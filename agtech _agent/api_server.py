"""HTTP service around the live fleet simulator, the Simulation Lab and the
learning memory. Standard library only (works offline). The Spring Boot backend
proxies to this.

    .venv/bin/python api_server.py            # http://127.0.0.1:8765

Live operations (ONE live FarmSimulator at a time):
    GET  /health
    GET  /state
    POST /reset    {"policy", "scenario_id", "seed"}
    POST /step     {"ticks": 1..100}
    POST /events   {"name": "breakdown", "tractor_id": "T2", ...}
    POST /estimate {"tractor_id","job_id"}
    POST /dispatch {"assignments":[...], "state_version": n, "commit": bool}
    POST /compare                        FCFS baseline replay (same scenario/events/horizon)
Simulation Lab & learning (background jobs, each run uses its own simulator):
    GET  /scenarios
    POST /lab/run      {"scenario_ids":[], "policies":[], "seeds":[]}
    GET  /lab/runs/<id>                   full run incl. per-tick timeline
    POST /learn/train  {"scenario_ids":[], "rounds": n}
    GET  /jobs, GET /jobs/<id>
    GET  /learning                        skills, facts, learning curve, insights
    GET  /learning/episodes?scenario_id=&policy=&q=&limit=
    GET  /learning/skills/<id>            SKILL.md
Candidate lessons:
    GET  /memory?tractor_id=&event_type=&operation=&scenario=    (<=5 records)
    GET  /memory/recent?limit=20
    POST /memory   {"lesson","comparison_id","claims_improvement","tractor_ids","operations"}
    POST /assess   {"comparison_id"}     LLM assessment agent (needs HF_TOKEN)
Farmer chat (read-only; proposes operator events for the farmer to confirm):
    POST /chat     {"message"}
AI crew (smolagents team from agent.py acting on the LIVE simulator; needs a HF token):
    POST /crew/run {"task"}   -> background job; its changes land in /state and the decision log
"""

from __future__ import annotations

import copy
import json
import os
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

import coordinator as c
import ai_crew
import farm_chat
import fleet_memory as fm
import learning
import scenario_library as sl
import sim_lab

POLICIES = sim_lab.POLICIES
OPERATOR_EVENTS = {"breakdown", "repair", "block_cell", "unblock_cell", "change_priority", "refuel"}
MAX_RUNS_KEPT = 300


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


class Jobs:
    """Tiny background-job registry with progress reporting."""

    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()

    def start(self, kind: str, label: str, fn: Callable[[Callable[[int, int, str], None]], Any]) -> dict[str, Any]:
        jid = f"{kind}-{uuid.uuid4().hex[:8]}"
        job = {"id": jid, "kind": kind, "label": label, "status": "running", "progress": 0, "total": 1,
               "message": "starting", "started_at": time.time(), "finished_at": None, "result": None, "error": None}
        with self.lock:
            self.items[jid] = job

        def progress(i: int, n: int, msg: str) -> None:
            job.update(progress=i, total=max(1, n), message=msg)

        def target() -> None:
            try:
                job["result"] = fn(progress)
                job["status"] = "done"
                job["message"] = "finished"
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
                job.update(status="error", error=f"{type(exc).__name__}: {exc}")
            finally:
                job["finished_at"] = time.time()

        threading.Thread(target=target, daemon=True, name=jid).start()
        return self.view(job)

    @staticmethod
    def view(job: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in job.items() if k != "result"} | {"has_result": job["result"] is not None}

    def get(self, jid: str) -> dict[str, Any]:
        job = self.items.get(jid)
        if not job:
            raise ApiError(404, f"no job {jid}")
        return {**self.view(job), "result": job["result"]}

    def running(self, kind: str) -> bool:
        return any(j["kind"] == kind and j["status"] == "running" for j in self.items.values())

    def list(self) -> list[dict[str, Any]]:
        return [self.view(j) for j in sorted(self.items.values(), key=lambda j: -j["started_at"])][:30]


class FleetService:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.assessing = False
        self.crew_running = False
        self.crew_log: list[dict[str, Any]] = []
        self.store = fm.MemoryStore()
        self.learning = learning.LearningMemory(lessons=self.store)
        self.registry = fm.ComparisonRegistry()
        self.jobs = Jobs()
        self.runs: dict[str, dict[str, Any]] = {}
        self.library = sl.load_library()
        self.last_comparison: dict[str, Any] | None = None
        self.reset("optimizer", os.environ.get("SCENARIO_ID", "cochise-field-day"), 0)

    # ------------------------------------------------------------ live sim
    def _skill_params(self, sid: str, policy: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        if policy != "agent":
            return None, None
        skill, why = self.learning.best_for(sid, self.library[sid].get("tags", []))
        return skill["params"], {"id": skill["id"], "name": skill["name"], "status": skill["status"],
                                 "why": why, "params": skill["params"]}

    def reset(self, policy: str = "optimizer", scenario_id: str | None = None, seed: int = 0) -> None:
        if policy not in POLICIES:
            raise ApiError(400, f"policy must be one of {list(POLICIES)}")
        sid = scenario_id or getattr(self, "scenario_id", "cochise-field-day")
        if sid not in self.library:
            raise ApiError(400, f"unknown scenario {sid}")
        if self.crew_running:
            raise ApiError(409, "the AI crew is working on the live farm; wait for it to finish")
        with self.lock:
            self.crew_log = []
            self.policy, self.scenario_id, self.seed = policy, sid, int(seed)
            self.spec = sim_lab.resolve(self.library[sid], self.seed)
            self.envr = sim_lab.Environment(self.spec)
            self.sim = self.envr.sim
            self.schedule = self.envr.schedule
            self.params, self.skill = self._skill_params(sid, policy)
            self.planner = self.envr.planner(policy, self.params, source="agent" if policy == "agent" else None)
            self.signature = self.envr.signature(self.params) if self.envr.env.enabled else None
            self.manual_events: list[dict[str, Any]] = []
            self.manual_dispatches = 0
            self.ticks_requested = 0
            self.decisions: list[c.Decision] = []
            self.timeline = [self.envr.sample()]
            self.recorded = False
            self.retrieve_tool = fm.make_retrieval_tool(self.sim, self.store)
            self.save_tool = fm.SaveCandidateMemoryTool(self.sim, self.store, self.registry, author="operator")
            self.last_comparison = None

    def _guard_mutation(self) -> None:
        if self.assessing:
            raise ApiError(409, "assessment agent is reading the simulator; try again shortly")
        if self.crew_running:
            raise ApiError(409, "the AI crew is working on the live farm; wait for it to finish")

    def all_events(self) -> list[dict[str, Any]]:
        return fm.events_as_list(list(self.spec["events"]) + self.manual_events)

    def done(self) -> bool:
        return (c.workday_over(self.sim) and not self.schedule.pending) or self.sim.current_tick >= self.spec["horizon"]

    def state(self) -> dict[str, Any]:
        with self.lock:
            s = self.sim.fleet_state()
            spec = self.spec
            s.update({
                "scenario_name": self.sim.scenario.get("name"),
                "scenario": {k: spec.get(k) for k in ("id", "title", "summary", "region", "tags", "horizon",
                                                      "seed", "realism", "sources", "machines")},
                "conditions": self.envr.env.snapshot(self.sim) if self.envr.env.enabled else {"enabled": False},
                "policy": self.policy,
                "skill": self.skill,
                "ticks_requested": self.ticks_requested,
                "done": self.done(),
                "assessing": self.assessing,
                "crew_running": self.crew_running,
                "crew_log": self.crew_log[-10:],
                "scheduled_events": {"applied": self.schedule.applied, "pending": self.schedule.pending},
                "manual_events": self.manual_events,
                "manual_dispatches": self.manual_dispatches,
                "decisions": [d.as_dict() for d in self.decisions[-30:]],
                "timeline": self.timeline,
                "memory_path": str(self.store.path),
                "learning_path": str(self.learning.root),
                "last_comparison": self.last_comparison,
            })
            return s

    def step(self, ticks: int) -> dict[str, Any]:
        if not 1 <= ticks <= 100:
            raise ApiError(400, "ticks must be from 1 to 100")
        with self.lock:
            self._guard_mutation()
            remaining = int(self.spec["horizon"]) - self.sim.current_tick
            ticks = max(0, min(ticks, remaining))
            before = self.sim.current_tick

            def on_tick(sim: Any) -> None:
                self.envr.env.after_tick(sim)
                self.timeline.append(self.envr.sample())

            new = c.run_episode(self.sim, self.planner, self.schedule, ticks=ticks,
                                replan_every_tick=self.policy in ("fcfs", "priority-nearest"),
                                on_tick=on_tick, verbose=False, signature=self.signature) if ticks else []
            self.decisions.extend(new)
            self.ticks_requested += ticks
            done = self.done()
            if done and not self.recorded and not self.manual_events and not self.manual_dispatches:
                # A clean live run is an episode like any lab run: remember it and reflect.
                self.recorded = True
                res = sim_lab.build_result(self.envr, self.policy, self.seed, self.params,
                                           (self.skill or {}).get("id"), self.decisions, [],
                                           horizon=self.spec["horizon"])
                self.learning.record_episode(res, role="live")
                self.learning.reflect(self.scenario_id)
            return {"ok": True, "advanced": self.sim.current_tick - before, "new_decisions": len(new),
                    "tick": self.sim.current_tick, "done": done}

    def operator_event(self, body: dict[str, Any]) -> dict[str, Any]:
        name = body.get("name")
        if name not in OPERATOR_EVENTS:
            raise ApiError(400, f"name must be one of {sorted(OPERATOR_EVENTS)}")
        payload = {k: v for k, v in body.items() if k != "name"}
        with self.lock:
            self._guard_mutation()
            try:
                result = self.sim.event(name, **payload)
            except (KeyError, ValueError, TypeError) as exc:
                raise ApiError(400, f"bad event payload: {exc}") from exc
            if result.get("ok"):
                self.manual_events.append({"tick": self.sim.current_tick, "name": name, **payload})
            return result

    def estimate(self, body: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            try:
                est = self.sim.estimate_job(str(body["tractor_id"]), str(body["job_id"]))
            except KeyError as exc:
                raise ApiError(400, f"unknown id: {exc}") from exc
            reason = self.envr.env.block_reason(self.sim, self.sim.jobs[str(body["job_id"])])
            if reason:
                est = {**est, "feasible": False,
                       "reasons": [*(est.get("reasons") or ([est["reason"]] if est.get("reason") else [])),
                                   f"field conditions: {reason}"]}
            return est

    def dispatch(self, body: dict[str, Any]) -> dict[str, Any]:
        assignments = body.get("assignments") or []
        version = body.get("state_version")
        with self.lock:
            blocked = [f"{a.get('job_id')}: {r}" for a in assignments
                       if a.get("job_id") in self.sim.jobs
                       and (r := self.envr.env.block_reason(self.sim, self.sim.jobs[a["job_id"]]))]
            if blocked:
                return {"valid": False, "accepted": False, "errors": [f"field conditions – {b}" for b in blocked]}
            if not body.get("commit"):
                return self.sim.validate_plan(assignments, expected_version=version)
            self._guard_mutation()
            res = self.sim.commit_plan(assignments, expected_version=version)
            if res.get("accepted"):
                self.manual_dispatches += 1
                text = c.explain_plan(self.sim, assignments, "operator dispatch", res.get("estimates"))
                self.decisions.append(c.Decision(self.sim.current_tick, "operator dispatch",
                                                 res.get("estimates", []), [], text, "operator"))
            return res

    def compare(self) -> dict[str, Any]:
        with self.lock:
            if self.ticks_requested == 0:
                raise ApiError(400, "run the simulation before comparing")
            events = self.all_events()
            horizon = self.ticks_requested
            base_spec = copy.deepcopy(self.spec)
            base_spec["events"] = events
            be = sim_lab.Environment(base_spec)
            base_dec = c.run_episode(be.sim, be.planner("fcfs"), be.schedule, ticks=horizon,
                                     replan_every_tick=True, on_tick=be.env.after_tick, verbose=False)
            base_rep = {**c.report(be.sim, base_dec, be.schedule, "fcfs")}
            base_rep["metrics"] = {**base_rep["metrics"], **be.env.metrics()}
            cid = f"cmp-{uuid.uuid4().hex[:8]}"
            policy = self.policy + ("+operator" if self.manual_dispatches else "")
            self.registry.register_baseline_report(cid, base_rep, be.sim.scenario, events, horizon)
            self.registry.register_coordinator_run(cid, self.sim, events, horizon, policy=policy)
            out = json.loads(fm.GetComparisonTool(self.sim, self.registry).forward(cid))
            out["baseline_score"] = sim_lab.lab_score(be.sim, be.env)
            out["observed_score"] = sim_lab.lab_score(self.sim, self.envr.env)
            if out["comparable"]:
                b, o = out["baseline_metrics"], out["observed_metrics"]
                out["deltas"] = {k: round(o[k] - b[k], 2) for k in o
                                 if isinstance(o[k], (int, float)) and isinstance(b.get(k), (int, float))}
            out["note"] = ("Measured on the same scenario, conditions, event schedule (scripted + operator) "
                           f"and {horizon}-tick horizon." if out["comparable"]
                           else "Not comparable: " + "; ".join(out["reasons"]))
            self.last_comparison = out
            return out

    # ---------------------------------------------------------------- lab
    def scenarios(self) -> dict[str, Any]:
        out = []
        for spec in self.library.values():
            sc = spec["scenario"]
            out.append({k: spec.get(k) for k in ("id", "title", "summary", "region", "tags", "horizon",
                                                 "realism", "sources", "machines")}
                       | {"tick_minutes": sc.get("tick_minutes", 15),
                          "fields": len(sc["fields"]), "tractors": len(sc["tractors"]), "jobs": len(sc["jobs"]),
                          "acres": sum(j["acres"] for j in sc["jobs"]),
                          "events": len(spec["events"]),
                          "weather": (spec.get("conditions") or {}).get("weather", [])})
        return {"scenarios": out, "policies": list(POLICIES)}

    def lab_run(self, body: dict[str, Any]) -> dict[str, Any]:
        sids = [s for s in body.get("scenario_ids") or [] if s in self.library]
        pols = [p for p in body.get("policies") or [] if p in POLICIES]
        seeds = [int(s) for s in (body.get("seeds") or [0])][:10]
        if not sids or not pols:
            raise ApiError(400, "choose at least one scenario and one policy")
        if len(sids) * len(pols) * len(seeds) > 120:
            raise ApiError(400, "too many runs (max 120 per batch)")
        combos = [(sid, pol, seed) for sid in sids for seed in seeds for pol in pols]

        def work(progress: Callable[[int, int, str], None]) -> dict[str, Any]:
            summaries = []
            for i, (sid, pol, seed) in enumerate(combos, 1):
                progress(i - 1, len(combos), f"{self.library[sid]['title']} · {pol} · seed {seed}")
                params, skill = self._skill_params(sid, pol)
                res = sim_lab.run_simulation(sid, pol, seed=seed, params=params,
                                             skill_id=(skill or {}).get("id"))
                run_id = f"run-{uuid.uuid4().hex[:8]}"
                res["run_id"], res["skill"] = run_id, skill
                self.runs[run_id] = res
                while len(self.runs) > MAX_RUNS_KEPT:
                    self.runs.pop(next(iter(self.runs)))
                self.learning.record_episode(res, role="lab")
                summaries.append({k: v for k, v in res.items() if k not in ("timeline", "decision_log", "jobs")})
            for sid in sids:
                self.learning.reflect(sid)
            progress(len(combos), len(combos), "done")
            return {"runs": summaries}

        return self.jobs.start("lab", f"{len(combos)} simulation runs", work)

    def lab_run_detail(self, run_id: str) -> dict[str, Any]:
        run = self.runs.get(run_id)
        if not run:
            raise ApiError(404, f"no run {run_id} (runs are kept in memory; re-run the batch)")
        spec = sim_lab.resolve(self.library[run["scenario_id"]], run["seed"])
        return {**run, "map": {"grid": spec["scenario"]["grid"], "base": spec["scenario"]["base"],
                               "fields": spec["scenario"]["fields"]}}

    def train(self, body: dict[str, Any]) -> dict[str, Any]:
        if self.jobs.running("train"):
            raise ApiError(409, "training is already running")
        sids = [s for s in body.get("scenario_ids") or [] if s in self.library] or \
               [s for s in self.library if s != "cochise-field-day"]
        rounds = max(1, min(int(body.get("rounds", 2)), 6))

        def work(progress: Callable[[int, int, str], None]) -> dict[str, Any]:
            return learning.train(self.learning, sids, rounds=rounds, progress=progress)

        return self.jobs.start("train", f"{rounds} round(s) × {len(sids)} scenario(s)", work)

    # -------------------------------------------------------------- memory
    def memory_search(self, q: dict[str, str]) -> dict[str, Any]:
        with self.lock:
            return json.loads(self.retrieve_tool.forward(q.get("tractor_id"), q.get("event_type"),
                                                         q.get("operation"), q.get("scenario")))

    def memory_recent(self, limit: int) -> dict[str, Any]:
        recs = self.store.all()[-max(1, min(limit, 50)):]
        return {"path": str(self.store.path), "count": len(recs), "records": list(reversed(recs))}

    def memory_save(self, body: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            return json.loads(self.save_tool.forward(
                body.get("lesson", ""), body.get("comparison_id", ""),
                body.get("claims_improvement"), body.get("tractor_ids"), body.get("operations")))

    def assess(self, body: dict[str, Any]) -> dict[str, Any]:
        if not farm_chat.hf_token():
            raise ApiError(503, "set HF_TOKEN (or run `hf auth login`) to run the LLM assessment agent")
        cid = body.get("comparison_id") or (self.last_comparison or {}).get("comparison_id")
        if not cid:
            raise ApiError(400, "run /compare first or pass comparison_id")
        with self.lock:
            if self.assessing:
                raise ApiError(409, "an assessment is already running")
            self.assessing = True
        try:
            from smolagents import InferenceClientModel
            model = InferenceClientModel(model_id=os.environ.get("FLEET_MODEL_ID", "Qwen/Qwen2.5-72B-Instruct"))
            agent = fm.build_assessment_agent(self.sim, model, self.store, self.registry)
            answer = agent.run(f"Assess the finished run for comparison_id {cid}.")
            return {"ok": True, "comparison_id": cid, "answer": str(answer)}
        finally:
            self.assessing = False

    def crew_run(self, body: dict[str, Any]) -> dict[str, Any]:
        if not farm_chat.hf_token():
            raise ApiError(503, "set HF_TOKEN (or run `hf auth login`) to run the AI crew")
        task = body.get("task")
        if task is not None and (not isinstance(task, str) or len(task) > 500):
            raise ApiError(400, "task must be a string of at most 500 characters")
        with self.lock:
            if self.crew_running or self.assessing:
                raise ApiError(409, "an agent run is already in progress")
            self.crew_running = True
            sim = self.sim

        def work(progress: Callable[[int, int, str], None]) -> dict[str, Any]:
            try:
                progress(0, 1, "AI crew is consulting the fleet, navigation, agronomy and safety agents…")
                res = ai_crew.run(sim, (task or "").strip() or None, env=self.envr.env)
                with self.lock:
                    if sim is self.sim:  # the farm wasn't reset meanwhile
                        changed = "; ".join(res["changes"]) or "no changes committed"
                        self.decisions.append(c.Decision(
                            tick=res["tick"], trigger="AI crew request", assignments=res["assignments"], refuels=[],
                            explanation=f"[t{res['tick']}] AI crew (LLM agents): {res['answer']}\nCommitted: {changed}",
                            source="crew"))
                        if res["changes"]:
                            self.manual_dispatches += 1  # an outside actor changed the run
                        self.crew_log.append({k: res[k] for k in ("tick", "task", "answer", "changes", "steps")})
                return res
            finally:
                self.crew_running = False

        return self.jobs.start("crew", "AI crew", work)

    def chat(self, body: dict[str, Any]) -> dict[str, Any]:
        msg = body.get("message")
        if not isinstance(msg, str):
            raise ApiError(400, "message must be a string")
        return farm_chat.answer(msg, self.state(), self.learning.summary())


# --------------------------------------------------------------------------- #
# HTTP plumbing
# --------------------------------------------------------------------------- #
SERVICE: FleetService | None = None


class Handler(BaseHTTPRequestHandler):
    server_version = "FleetSim/2.0"

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            data = json.loads(self.rfile.read(n))
        except json.JSONDecodeError as exc:
            raise ApiError(400, f"invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ApiError(400, "body must be a JSON object")
        return data

    def _dispatch(self, method: str) -> None:
        assert SERVICE is not None
        url = urlparse(self.path)
        path = url.path.rstrip("/") or "/"
        if path.startswith("/api/"):
            path = path[4:]
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        s = SERVICE
        try:
            routes = {
                ("GET", "/health"): lambda: {"ok": True, "tick": s.sim.current_tick},
                ("GET", "/state"): s.state,
                ("POST", "/reset"): lambda: (lambda b: (s.reset(b.get("policy", "optimizer"), b.get("scenario_id"),
                                                                int(b.get("seed", 0))), s.state())[1])(self._body()),
                ("POST", "/step"): lambda: s.step(int(self._body().get("ticks", 1))),
                ("POST", "/events"): lambda: s.operator_event(self._body()),
                ("POST", "/estimate"): lambda: s.estimate(self._body()),
                ("POST", "/dispatch"): lambda: s.dispatch(self._body()),
                ("POST", "/compare"): s.compare,
                ("GET", "/scenarios"): s.scenarios,
                ("POST", "/lab/run"): lambda: s.lab_run(self._body()),
                ("POST", "/learn/train"): lambda: s.train(self._body()),
                ("GET", "/jobs"): lambda: {"jobs": s.jobs.list()},
                ("GET", "/learning"): s.learning.summary,
                ("GET", "/learning/episodes"): lambda: {"episodes": s.learning.episodes(
                    int(q.get("limit", 50)), q.get("scenario_id"), q.get("policy"), q.get("q"))},
                ("GET", "/memory"): lambda: s.memory_search(q),
                ("GET", "/memory/recent"): lambda: s.memory_recent(int(q.get("limit", 20))),
                ("POST", "/memory"): lambda: s.memory_save(self._body()),
                ("POST", "/assess"): lambda: s.assess(self._body()),
                ("POST", "/chat"): lambda: s.chat(self._body()),
                ("POST", "/crew/run"): lambda: s.crew_run(self._body()),
            }
            fn = routes.get((method, path))
            if fn is None and method == "GET":
                for prefix, handler in (("/jobs/", s.jobs.get), ("/lab/runs/", s.lab_run_detail),
                                        ("/learning/skills/", lambda i: {"id": i, "markdown": s.learning.skill_md(i)})):
                    if path.startswith(prefix) and len(path) > len(prefix):
                        ident = unquote(path[len(prefix):])
                        fn = (lambda h=handler, i=ident: h(i))
                        break
            if fn is None:
                raise ApiError(404, f"no route {method} {path}")
            self._send(200, fn())
        except ApiError as exc:
            self._send(exc.status, {"error": exc.message})
        except (ValueError, TypeError) as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def log_message(self, fmt: str, *args: Any) -> None:
        if os.environ.get("SIM_API_QUIET") != "1":
            super().log_message(fmt, *args)


def main() -> None:
    global SERVICE
    SERVICE = FleetService()
    host = os.environ.get("SIM_API_HOST", "127.0.0.1")
    port = int(os.environ.get("SIM_API_PORT", "8765"))
    print(f"Fleet sim service on http://{host}:{port}  (lessons: {SERVICE.store.path}; "
          f"learning: {SERVICE.learning.root})")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
