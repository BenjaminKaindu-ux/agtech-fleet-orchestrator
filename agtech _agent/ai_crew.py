"""Run the smolagents crew from agent.py against the LIVE simulator.

agent.py builds its tools around a global `sim` and only creates one when none
is present, so we execute it with the server's live FarmSimulator injected.
No second simulator is created, and whatever the crew commits (dispatches,
priorities, refuels, breakdown reports) shows up immediately in /state.

The server owns the clock, so the crew's `advance_simulation` tool is removed.
"""

from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any

AGENT_FILE = Path(__file__).with_name("agent.py")
DEFAULT_TASK = ("Plan dispatch for the idle tractors right now. Consult your specialists, then commit only a "
                "safety-validated plan. Respect weather/soil holds. Summarize what you changed in 2-3 sentences.")


class ConditionAwareSim:
    """The live simulator, plus field-condition holds (wind, rain, wet soil, prerequisites) enforced at
    estimate/validate/commit time. farm_sim alone doesn't know the weather, so without this an LLM could
    dispatch a sprayer into 16 mph wind. Everything else passes straight through to the same object."""

    def __init__(self, sim: Any, env: Any = None) -> None:
        self._sim, self._env = sim, env

    def __getattr__(self, name: str) -> Any:
        return getattr(self._sim, name)

    def hold(self, job_id: str) -> str | None:
        env, job = self._env, self._sim.jobs.get(job_id)
        if env is None or not getattr(env, "enabled", False) or job is None or env.gate(self._sim, job):
            return None
        return env.block_reason(self._sim, job) or "waiting on a prerequisite job"

    def _hold_errors(self, assignments: Any) -> list[str]:
        out = []
        for a in assignments if isinstance(assignments, list) else []:
            reason = self.hold(str((a or {}).get("job_id")))
            if reason:
                out.append(f"{a.get('job_id')} is on hold: {reason}")
        return out

    def estimate_job(self, tractor_id: str, job_id: str) -> dict[str, Any]:
        est = self._sim.estimate_job(tractor_id, job_id)
        reason = self.hold(job_id)
        return {**est, "feasible": False, "reasons": [*est.get("reasons", []), f"on hold: {reason}"]} if reason else est

    def pending_jobs(self) -> list[dict[str, Any]]:
        return [{**j, "on_hold": self.hold(j["id"])} for j in self._sim.pending_jobs()]

    def validate_plan(self, assignments: Any, expected_version: int | None = None) -> dict[str, Any]:
        res = self._sim.validate_plan(assignments, expected_version=expected_version)
        holds = self._hold_errors(assignments)
        return {**res, "valid": False, "errors": [*res.get("errors", []), *holds]} if holds else res

    def commit_plan(self, assignments: Any, expected_version: int | None = None) -> dict[str, Any]:
        holds = self._hold_errors(assignments)
        if holds:
            return {"accepted": False, "valid": False, "errors": holds}
        return self._sim.commit_plan(assignments, expected_version=expected_version)


def build_crew(live_sim: Any, env: Any = None) -> dict[str, Any]:
    ns: dict[str, Any] = {"sim": ConditionAwareSim(live_sim, env), "__name__": "fleet_crew", "__file__": str(AGENT_FILE)}
    exec(compile(AGENT_FILE.read_text(), str(AGENT_FILE), "exec"), ns)  # noqa: S102 — our own project file
    crew = ns["final_agent"]
    crew.tools.pop("advance_simulation", None)  # system prompt is rebuilt from tools on each run
    return ns


def snapshot(sim: Any) -> dict[str, Any]:
    s = sim.fleet_state()
    return {"tractors": {t["id"]: {k: t[k] for k in ("status", "job_id", "fuel_pct", "position")} for t in s["tractors"]},
            "jobs": {j["id"]: {k: j[k] for k in ("priority", "assigned_tractor_id", "status")} for j in s["jobs"]},
            "blocked": sorted(map(tuple, s.get("grid", {}).get("blocked_cells", []) or [])),
            "events": len(s.get("recent_events", []))}


def diff(sim: Any, before: dict[str, Any], after: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Assignments the crew made + human-readable list of every change."""
    assignments, changes = [], []
    for tid, a in after["tractors"].items():
        b = before["tractors"].get(tid, {})
        if a["job_id"] and a["job_id"] != b.get("job_id"):
            try:
                est = sim.estimate_job(tid, a["job_id"])
            except Exception:  # noqa: BLE001
                est = {}
            assignments.append({"tractor_id": tid, "job_id": a["job_id"], "feasible": True, "reasons": [],
                                **{k: est.get(k) for k in ("route", "travel_cells", "work_hours",
                                                           "estimated_completion_tick", "fuel_needed_liters")}})
            changes.append(f"{tid} → {a['job_id']}")
        elif a["status"] != b.get("status") and a["status"] in ("broken", "to_refuel", "refueling"):
            changes.append(f"{tid} now {a['status']}")
        elif b.get("status") == "broken" and a["status"] != "broken":
            changes.append(f"{tid} repaired")
    for jid, a in after["jobs"].items():
        if a["priority"] != before["jobs"].get(jid, {}).get("priority"):
            changes.append(f"{jid} priority {before['jobs'][jid]['priority']} → {a['priority']}")
    for cell in set(after["blocked"]) ^ set(before["blocked"]):
        changes.append(f"cell {cell} {'blocked' if cell in after['blocked'] else 'reopened'}")
    return assignments, changes


def brief(sim: Any, task: str | None, env: Any = None) -> str:
    """Hand the crew the live facts up front so specialists don't have to dig."""
    s = sim.fleet_state()
    holds = {}
    if env is not None and getattr(env, "enabled", False):
        holds = env.snapshot(sim).get("job_blocks") or {}
    guard = ConditionAwareSim(sim, env)
    idle_t = [t for t in s["tractors"] if t["status"] == "idle" and not t["job_id"]]
    open_j = [j for j in s["jobs"] if j["status"] in ("pending", "interrupted") and not j["assigned_tractor_id"]]
    pairs = [f"{t['id']}→{j['id']}" for t in idle_t for j in open_j
             if j["required_implement"] in t["implements"] and not guard.hold(j["id"])]
    idle = [f"{t['id']} ({'/'.join(t['implements'])}, fuel {t['fuel_pct']:.0f}%)"
            for t in s["tractors"] if t["status"] == "idle" and not t["job_id"]]
    open_jobs = [f"{j['id']} {j['job_type']} on {j['field_id']} needs {j['required_implement']}, "
                 f"priority {j['priority']}, due t{j['deadline_tick']}" + (f" [ON HOLD: {holds[j['id']]}]" if j["id"] in holds else "")
                 for j in s["jobs"] if j["status"] in ("pending", "interrupted") and not j["assigned_tractor_id"]]
    return (f"{task or DEFAULT_TASK}\n\nLIVE FACTS at tick {s['tick']} (state_version {s['version']}):\n"
            f"Idle tractors: {', '.join(idle) or 'none'}\nOpen jobs:\n- " + "\n- ".join(open_jobs or ["none"]) +
            f"\nAllowed right now (implement matches, not on hold): {', '.join(pairs) or 'NONE'}" +
            ("\nNothing can be dispatched right now — do not consult specialists; give final_answer immediately "
             "explaining why (holds / no idle machine) and when that may change." if not pairs else "") +
            "\n\nRULES: a tractor can only take a job whose required implement it carries. Never dispatch a job "
            "marked ON HOLD. Specialists answer in plain text — read their answers, do NOT json.loads them. "
            "To commit, call commit_dispatch_plan(assignments_json='[{\"tractor_id\":\"T1\",\"job_id\":\"J2\"}]', "
            f"state_version={s['version']}). Keep it quick: ask each specialist at most once, then commit and give "
            "your final_answer.")


def run(live_sim: Any, task: str | None, env: Any = None, max_steps: int = 6, deadline_s: float = 180) -> dict[str, Any]:
    ns = build_crew(live_sim, env)
    crew = ns["final_agent"]
    agents = [crew, *crew.managed_agents.values()]
    for a in agents[1:]:
        a.max_steps = 4
    timer = threading.Timer(deadline_s, lambda: [a.interrupt() for a in agents])
    before = copy.deepcopy(snapshot(live_sim))
    timer.start()
    try:
        answer = crew.run(brief(live_sim, task, env), max_steps=max_steps)
    except Exception as exc:  # noqa: BLE001 — interrupted or model failure; keep whatever was committed
        answer = (f"The crew ran out of time ({deadline_s:.0f} s) before finishing; anything listed below was "
                  "committed, nothing else was changed." if "interrupt" in str(exc).lower()
                  else f"The crew stopped with an error ({type(exc).__name__}: {str(exc)[:160]}).")
    finally:
        timer.cancel()
    after = snapshot(live_sim)
    assignments, changes = diff(live_sim, before, after)
    return {"answer": str(answer), "assignments": assignments, "changes": changes,
            "tick": live_sim.current_tick, "task": task or DEFAULT_TASK,
            "steps": len(getattr(crew.memory, "steps", []))}


def as_json(result: dict[str, Any]) -> str:
    return json.dumps(result, default=str)
