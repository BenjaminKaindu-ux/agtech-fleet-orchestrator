"""Learning memory: the fleet agent keeps what it learns and improves with practice.

The design borrows the *closed learning loop* of Nous Research's Hermes Agent
(persistent memory, skills created from experience and refined in use, searchable
session history, periodic "nudges" to write down what was learned), adapted so that
every improvement is measured in the simulator rather than asserted by a model:

  Hermes idea                     Here
  ─────────────────────────────── ───────────────────────────────────────────────
  session history + recall        episodes.jsonl  – every run, searchable
  MEMORY.md (persistent facts)    facts.json      – running stats per scenario,
                                                    machine and field condition
  skills (SKILL.md), refined      skills.json + skills/<id>/SKILL.md – planner
    when used                       strategies created as variants of the current
                                    champion and promoted only on measured wins
  memory nudge after a task       reflect()       – updates facts and writes
                                                    measured insights to the
                                                    candidate-lesson store

Rules that keep it honest:
  * A skill is promoted (``proven``) only if it beats its parent on the *same*
    scenario, event schedule, conditions and horizon (paired seeds) **and** on
    held-out seeds it was not selected on. Otherwise it is ``rejected``.
  * Skills only change planner weights / look-ahead. Every dispatch is still
    validated by the simulator; field-condition rules still apply.
  * Insights written by reflection are measured facts with status ``candidate``;
    model-written lessons are never promoted to rules by this code.

Storage: ``AGENT_MEMORY_DIR`` or ``data/agent_memory/`` (all local files).
"""

from __future__ import annotations

import json
import math
import os
import random
import statistics
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import fleet_memory as fm
import sim_lab

HERE = Path(__file__).resolve().parent
DEFAULT_ROOT = HERE / "data" / "agent_memory"
DEFAULT_SKILL_ID = "optimizer-default"
PARAM_BOUNDS = {"w_priority": (2.0, 60.0), "w_urgency": (2.0, 120.0), "w_acres": (0.05, 3.0),
                "w_travel": (0.0, 6.0)}
HORIZONS = (24, 36, 48, 60, 72, 96)
MIN_GAIN = 1.0  # points; a challenger must beat its parent by more than noise


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _mean(xs: Iterable[float]) -> float:
    xs = list(xs)
    return round(statistics.fmean(xs), 2) if xs else 0.0


class LearningMemory:
    def __init__(self, root: str | Path | None = None, lessons: fm.MemoryStore | None = None):
        self.root = Path(root or os.environ.get("AGENT_MEMORY_DIR") or DEFAULT_ROOT).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lessons = lessons or fm.MemoryStore()
        self._lock = threading.RLock()
        self.episodes_path = self.root / "episodes.jsonl"
        self.facts_path = self.root / "facts.json"
        self.skills_path = self.root / "skills.json"
        self.curve_path = self.root / "curve.jsonl"
        self.skills: dict[str, dict[str, Any]] = self._load(self.skills_path, {})
        self.facts: dict[str, Any] = self._load(self.facts_path, {"scenarios": {}, "machines": {}, "conditions": {}})
        if DEFAULT_SKILL_ID not in self.skills:
            self.skills[DEFAULT_SKILL_ID] = self._new_skill(
                DEFAULT_SKILL_ID, "Default rollout optimizer", dict(sim_lab.DEFAULT_PARAMS),
                parent=None, scenario_id=None, contexts=["*"], status="default",
                description="The coordinator's hand-set weights. Baseline every learned skill is measured against.")
            self._save_skills()

    # ------------------------------------------------------------- storage
    @staticmethod
    def _load(path: Path, default: Any) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default

    def _write_json(self, path: Path, data: Any) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
        os.replace(tmp, path)

    def _append(self, path: Path, row: dict[str, Any]) -> None:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, separators=(",", ":"), default=str) + "\n")

    def _read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        if not path.is_file():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def _save_skills(self) -> None:
        self._write_json(self.skills_path, self.skills)

    # ------------------------------------------------------------ episodes
    def record_episode(self, result: dict[str, Any], role: str = "lab") -> dict[str, Any]:
        m = result["metrics"]
        ep = {
            "id": f"ep-{uuid.uuid4().hex[:10]}", "at": _now(), "role": role,
            "scenario_id": result["scenario_id"], "title": result["title"], "tags": result.get("tags", []),
            "seed": result["seed"], "policy": result["policy"], "skill_id": result.get("skill_id"),
            "params": result.get("params"), "horizon": result["horizon"],
            "fingerprint": result["fingerprint"], "score": result["score"],
            "metrics": {k: m.get(k) for k in ("completed_jobs", "on_time_jobs", "unfinished_jobs", "acres_completed",
                                              "total_job_acres", "travel_cells", "idle_tractor_ticks",
                                              "fuel_used_liters", "breakdowns", "weather_halts",
                                              "rain_damaged_acres", "yard_moves")},
            "jobs_total": len(result.get("jobs", [])),
        }
        with self._lock:
            self._append(self.episodes_path, ep)
            self._update_facts(result, ep)
        return ep

    def episodes(self, limit: int = 50, scenario_id: str | None = None, policy: str | None = None,
                 query: str | None = None) -> list[dict[str, Any]]:
        rows = self._read_jsonl(self.episodes_path)
        if scenario_id:
            rows = [r for r in rows if r["scenario_id"] == scenario_id]
        if policy:
            rows = [r for r in rows if r["policy"] == policy]
        if query:
            q = query.lower()
            rows = [r for r in rows if q in json.dumps(r).lower()]
        return list(reversed(rows[-max(1, min(limit, 500)):]))

    # --------------------------------------------------------------- facts
    @staticmethod
    def _running(stat: dict[str, Any], key: str, value: float) -> None:
        n = stat.get("n", 0)
        stat[key] = round((stat.get(key, 0.0) * n + value) / (n + 1), 2) if n else round(value, 2)

    def _update_facts(self, result: dict[str, Any], ep: dict[str, Any]) -> None:
        sid, pol = result["scenario_id"], result["policy"]
        f = self.facts
        sc = f["scenarios"].setdefault(sid, {"title": result["title"], "runs": 0, "by_policy": {}})
        sc["runs"] += 1
        bp = sc["by_policy"].setdefault(pol, {"n": 0})
        for key, val in (("mean_score", ep["score"]), ("mean_jobs", ep["metrics"]["completed_jobs"]),
                         ("mean_acres", ep["metrics"]["acres_completed"]),
                         ("mean_idle", ep["metrics"]["idle_tractor_ticks"])):
            self._running(bp, key, float(val))
        bp["best_score"] = max(bp.get("best_score", -1e9), ep["score"])
        bp["n"] += 1
        sc["updated_at"] = ep["at"]
        for tid, t in result.get("tractors", {}).items():
            mk = f["machines"].setdefault(f"{sid}:{tid}", {"scenario_id": sid, "tractor_id": tid,
                                                           "implements": t["implements"], "n": 0,
                                                           "breakdowns": 0, "halts": 0})
            self._running(mk, "mean_acres", t["acres_completed"])
            self._running(mk, "mean_jobs", t["jobs_completed"])
            mk["breakdowns"] += t["breakdowns"]
            mk["halts"] += t["halts"]
            mk["n"] += 1
        cond = f["conditions"].setdefault(sid, {"n": 0, "halt_reasons": {}})
        for h in result.get("halts", []):
            reason = h["reason"].split(" until")[0]
            reason = "wind" if reason.startswith("wind") else reason
            cond["halt_reasons"][reason] = cond["halt_reasons"].get(reason, 0) + 1
        self._running(cond, "mean_halts", float(ep["metrics"]["weather_halts"] or 0))
        self._running(cond, "mean_rain_damage", float(ep["metrics"]["rain_damaged_acres"] or 0))
        cond["n"] += 1
        f["updated_at"] = ep["at"]
        self._write_json(self.facts_path, f)

    # -------------------------------------------------------------- skills
    def _new_skill(self, sid: str, name: str, params: dict[str, Any], parent: str | None,
                   scenario_id: str | None, contexts: list[str], status: str = "candidate",
                   description: str = "") -> dict[str, Any]:
        parent_v = self.skills.get(parent, {}).get("version", 0) if parent else 0
        return {"id": sid, "name": name, "description": description, "params": params,
                "parent": parent, "version": parent_v + 1, "scenario_id": scenario_id,
                "contexts": contexts, "status": status, "created_at": _now(), "updated_at": _now(),
                "evidence": {}, "history": []}

    def champion(self, scenario_id: str) -> dict[str, Any]:
        """Current best proven skill *for this scenario*, else the default."""
        proven = [s for s in self.skills.values() if s["status"] == "proven" and s.get("scenario_id") == scenario_id]
        if not proven:
            return self.skills[DEFAULT_SKILL_ID]
        return max(proven, key=lambda s: (s["evidence"].get(scenario_id, {}).get("vs_default", 0.0), s["version"]))

    def best_for(self, scenario_id: str, tags: list[str]) -> tuple[dict[str, Any], str]:
        """Skill the ``agent`` policy should use, and why."""
        champ = self.champion(scenario_id)
        if champ["id"] != DEFAULT_SKILL_ID:
            ev = champ["evidence"].get(scenario_id, {})
            return champ, f"proven on this scenario: {ev.get('vs_default', 0):+.1f} pts vs default on held-out seeds"
        # transfer: a proven skill from a scenario sharing ≥2 condition tags
        best, best_overlap = None, 1
        for s in self.skills.values():
            if s["status"] != "proven":
                continue
            overlap = len(set(s["contexts"]) & set(tags))
            if overlap > best_overlap:
                best, best_overlap = s, overlap
        if best:
            return best, f"transferred from {best['scenario_id']} (shares {best_overlap} condition tags); not yet proven here"
        return self.skills[DEFAULT_SKILL_ID], "no proven skill yet; using default weights"

    def write_skill_md(self, skill: dict[str, Any]) -> Path:
        d = self.root / "skills" / skill["id"]
        d.mkdir(parents=True, exist_ok=True)
        p = skill["params"]
        ev_rows = "\n".join(
            f"| {sid} | {e.get('train_n', 0)} | {e.get('train_delta', 0):+.1f} | {e.get('holdout_n', 0)} | "
            f"{e.get('holdout_delta', 0):+.1f} | {e.get('vs_default', 0):+.1f} |"
            for sid, e in skill["evidence"].items()) or "| – | 0 | – | 0 | – | – |"
        hist = "\n".join(f"- {h['at']}: {h['event']}" for h in skill["history"][-10:]) or "- created"
        text = f"""---
name: {skill['id']}
description: {skill['description'] or skill['name']}
metadata:
  status: {skill['status']}
  version: {skill['version']}
  parent: {skill['parent'] or 'none'}
  scenario: {skill['scenario_id'] or 'any'}
  contexts: [{', '.join(skill['contexts'])}]
---

# {skill['name']}

**Status:** `{skill['status']}`. Planner weights only; every dispatch is still validated by
the simulator and field-condition rules still apply.

## When to use
Scenario `{skill['scenario_id'] or 'any'}` and conditions tagged: {', '.join(skill['contexts'])}.

## Parameters
| priority weight | urgency weight | acres weight | travel penalty | look-ahead (ticks) |
|---|---|---|---|---|
| {p['w_priority']} | {p['w_urgency']} | {p['w_acres']} | {p['w_travel']} | {p['horizon']} |

## Evidence (paired runs: same scenario, events, conditions, horizon)
| scenario | train runs | Δ vs parent | held-out runs | Δ vs parent | Δ vs default |
|---|---|---|---|---|---|
{ev_rows}

## History
{hist}
"""
        path = d / "SKILL.md"
        path.write_text(text, encoding="utf-8")
        return path

    def skill_md(self, skill_id: str) -> str:
        p = self.root / "skills" / skill_id / "SKILL.md"
        if not p.is_file() and skill_id in self.skills:
            self.write_skill_md(self.skills[skill_id])
        return p.read_text(encoding="utf-8") if p.is_file() else ""

    # ---------------------------------------------------------- reflection
    def reflect(self, scenario_id: str) -> list[dict[str, Any]]:
        """Memory nudge: turn accumulated measurements for a scenario into concise,
        measured insights (status ``candidate``) that planners can retrieve."""
        sc = self.facts["scenarios"].get(scenario_id)
        cond = self.facts["conditions"].get(scenario_id, {})
        if not sc:
            return []
        title = sc["title"]
        written = []

        def upsert(kind: str, lesson: str, tags: dict[str, list[str]], measured: dict[str, Any],
                   comparable: bool = False) -> None:
            def build(old: dict[str, Any] | None) -> dict[str, Any]:
                return {
                    "id": (old or {}).get("id") or f"ins-{uuid.uuid4().hex[:10]}", "schema": 1,
                    "status": "candidate", "created_at": (old or {}).get("created_at") or _now(),
                    "updated_at": _now(), "scenario": title, "scenario_id": scenario_id,
                    "comparison_id": None, "tags": tags,
                    "measured": {**measured, "comparison": {"comparable": comparable, "reasons": [],
                                                            "deltas": measured.get("deltas")}},
                    "interpretation": {"lesson": lesson[:fm.MAX_LESSON_CHARS], "author": "reflection",
                                       "claims_improvement": comparable},
                    "observations": (old or {}).get("observations", 0) + 1,
                }
            written.append(self.lessons.upsert(f"insight:{scenario_id}:{kind}", build))

        reasons = cond.get("halt_reasons", {})
        if reasons:
            top = max(reasons, key=reasons.get)
            ops = sorted({imp for k, m in self.facts["machines"].items() if k.startswith(scenario_id + ":")
                          for imp in m["implements"]})
            upsert("halts", f"{title}: field conditions stopped work {cond.get('mean_halts', 0):.1f}× per run on "
                            f"average; most common cause: {top}.",
                   {"tractors": [], "event_types": ["condition_halt"], "operations": ops},
                   {"runs": cond.get("n", 0), "mean_halts": cond.get("mean_halts"), "halt_reasons": reasons})
        if cond.get("mean_rain_damage", 0) > 0:
            upsert("rain_damage", f"{title}: on average {cond['mean_rain_damage']:.0f} ac of cut hay were still "
                                  f"unbaled when rain arrived.",
                   {"tractors": [], "event_types": ["rain"], "operations": ["mower-conditioner", "baler"]},
                   {"runs": cond.get("n", 0), "mean_rain_damaged_acres": cond["mean_rain_damage"]})
        # Paired comparison across seeds (same scenario/events/horizon by construction).
        paired = self.paired_scores(scenario_id)
        if paired and "fcfs" in paired:
            fc_scores = paired.pop("fcfs")
            for pol, scores in paired.items():
                common = sorted(set(scores) & set(fc_scores))
                if len(common) >= 2:
                    deltas = [scores[s] - fc_scores[s] for s in common]
                    d = _mean(deltas)
                    word = "outperformed" if d > 0 else "underperformed"
                    upsert(f"vs_fcfs:{pol}", f"{title}: {pol} {word} FCFS by {abs(d):.0f} pts on average over "
                                             f"{len(common)} paired seeds (same scenario, events, horizon).",
                           {"tractors": [], "event_types": [], "operations": []},
                           {"paired_seeds": common, "deltas": {"score": d},
                            "wins": sum(x > 0 for x in deltas)}, comparable=True)
        for key, mstat in self.facts["machines"].items():
            if key.startswith(scenario_id + ":") and mstat["n"] >= 3 and mstat["breakdowns"] / mstat["n"] >= 0.5:
                upsert(f"reliability:{mstat['tractor_id']}",
                       f"{title}: {mstat['tractor_id']} had {mstat['breakdowns']} breakdowns across {mstat['n']} runs; "
                       f"plan spare capacity for its {', '.join(mstat['implements'])} work.",
                       {"tractors": [mstat["tractor_id"]], "event_types": ["breakdown"], "operations": mstat["implements"]},
                       {"runs": mstat["n"], "breakdowns": mstat["breakdowns"]})
        return [w for w in written if w]

    def paired_scores(self, scenario_id: str) -> dict[str, dict[int, float]]:
        """Latest score per (policy, seed). Agent/optimizer runs only count if they used
        the policy's own params (so training challengers don't pollute the table)."""
        out: dict[str, dict[int, float]] = {}
        for ep in self._read_jsonl(self.episodes_path):
            if ep["scenario_id"] != scenario_id or ep["role"] == "training":
                continue
            out.setdefault(ep["policy"], {})[ep["seed"]] = ep["score"]
        return out

    # ------------------------------------------------------------ training
    def add_curve_point(self, point: dict[str, Any]) -> None:
        with self._lock:
            self._append(self.curve_path, {"at": _now(), **point})

    def curve(self) -> list[dict[str, Any]]:
        return self._read_jsonl(self.curve_path)

    def summary(self) -> dict[str, Any]:
        skills = sorted(self.skills.values(), key=lambda s: (s["status"] != "proven", s["created_at"]))
        counts: dict[str, int] = {}
        for s in self.skills.values():
            counts[s["status"]] = counts.get(s["status"], 0) + 1
        return {"root": str(self.root), "skills": skills, "skill_counts": counts, "facts": self.facts,
                "curve": self.curve(), "episode_count": len(self._read_jsonl(self.episodes_path)),
                "insights": [r for r in self.lessons.all() if str(r.get("key", "")).startswith("insight:")][-40:]}


# --------------------------------------------------------------------------- #
# Self-improvement loop
# --------------------------------------------------------------------------- #
def mutate(params: dict[str, Any], rng: random.Random, scale: float = 0.45,
           horizon_p: float = 0.35) -> dict[str, Any]:
    """Log-normal perturbation of 1–3 weights; sometimes step the look-ahead."""
    out = dict(params)
    keys = rng.sample(list(PARAM_BOUNDS), k=rng.randint(1, 3))
    for k in keys:
        lo, hi = PARAM_BOUNDS[k]
        val = max(float(out[k]), 0.05) * math.exp(rng.gauss(0, scale))
        out[k] = round(min(hi, max(lo, val)), 3)
    if rng.random() < horizon_p:
        i = HORIZONS.index(min(HORIZONS, key=lambda h: abs(h - int(out["horizon"]))))
        out["horizon"] = HORIZONS[max(0, min(len(HORIZONS) - 1, i + rng.choice((-1, 1, 2))))]
    return out


def explore(rng: random.Random) -> dict[str, Any]:
    """Random restart anywhere in the bounds (log-uniform weights)."""
    out = {k: round(math.exp(rng.uniform(math.log(max(lo, 0.05)), math.log(hi))), 3)
           for k, (lo, hi) in PARAM_BOUNDS.items()}
    out["horizon"] = rng.choice(HORIZONS)
    return out


def challengers(params: dict[str, Any], rng: random.Random) -> list[tuple[str, dict[str, Any]]]:
    return [("refine", mutate(params, rng)),
            ("bold step", mutate(params, rng, scale=0.9, horizon_p=0.7)),
            ("explore", explore(rng))]


def _run_headless(args: tuple) -> dict[str, Any]:
    sid, seed, policy, params, skill_id = args
    return sim_lab.run_simulation(sid, policy, seed=seed, params=params, skill_id=skill_id,
                                  record_timeline=False)


class Evaluator:
    """Deterministic runs are cached by (scenario, seed, policy, params) and executed
    across CPU cores (each run builds its own simulator, so they are independent)."""

    def __init__(self, memory: LearningMemory, workers: int | None = None):
        self.memory = memory
        self.cache: dict[str, dict[str, Any]] = {}
        self.workers = max(1, workers if workers is not None else min(8, (os.cpu_count() or 2) - 1))
        self._pool = None

    def _executor(self):
        if self._pool is None and self.workers > 1:
            import concurrent.futures as cf
            import multiprocessing as mp
            self._pool = cf.ProcessPoolExecutor(self.workers, mp_context=mp.get_context("spawn"))
        return self._pool

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown()
            self._pool = None

    def scores(self, sid: str, seeds: Iterable[int], policy: str, params: dict[str, Any] | None,
               skill_id: str | None, role: str) -> dict[int, float]:
        seeds = list(seeds)
        keys = {s: json.dumps([sid, s, policy, params], sort_keys=True) for s in seeds}
        todo = [s for s in seeds if keys[s] not in self.cache]
        jobs = [(sid, s, policy, params, skill_id) for s in todo]
        pool = self._executor() if len(jobs) > 1 else None
        results = list(pool.map(_run_headless, jobs)) if pool else [_run_headless(j) for j in jobs]
        for s, res in zip(todo, results):
            self.memory.record_episode(res, role=role)
            self.cache[keys[s]] = res
        return {s: self.cache[keys[s]]["score"] for s in seeds}


def train(memory: LearningMemory, scenario_ids: list[str], rounds: int = 2,
          train_seeds: tuple[int, ...] = (1, 2, 3, 4, 5), holdout_seeds: tuple[int, ...] = (101, 102, 103),
          progress: Callable[[int, int, str], None] | None = None, rng_seed: int | None = None,
          evaluator: Evaluator | None = None) -> dict[str, Any]:
    """Champion/challenger self-play on the simulator.

    For each scenario and round: mutate the current champion into a challenger, run
    both on the same training seeds (paired), and if the challenger wins, confirm on
    held-out seeds before promoting it. Records a learning-curve point each round."""
    lib = sim_lab.sl.load_library()
    ev = evaluator or Evaluator(memory)
    rng = random.Random(rng_seed if rng_seed is not None else uuid.uuid4().int)
    eval_seeds = tuple(train_seeds) + tuple(holdout_seeds)
    own_evaluator = evaluator is None
    steps_total, step = rounds * len(scenario_ids), 0
    log: list[dict[str, Any]] = []
    for r in range(1, rounds + 1):
        for sid in scenario_ids:
            step += 1
            spec = lib[sid]
            tags = spec.get("tags", [])
            if progress:
                progress(step, steps_total, f"round {r}: {spec['title']}")
            champ = memory.champion(sid)
            default = memory.skills[DEFAULT_SKILL_ID]
            ref_default = ev.scores(sid, eval_seeds, "optimizer", None, DEFAULT_SKILL_ID, "reference")
            ref_fcfs = ev.scores(sid, eval_seeds, "fcfs", None, None, "reference")
            champ_train = ev.scores(sid, train_seeds, "agent", champ["params"], champ["id"], "training")
            # Try several variants on the same paired training seeds; keep the best.
            tried = []
            for kind, params in challengers(champ["params"], rng):
                sc = ev.scores(sid, train_seeds, "agent", params, None, "training")
                tried.append((_mean(sc[s] - champ_train[s] for s in train_seeds), kind, params, sc))
            tried.sort(key=lambda t: -t[0])
            _, kind, cand_params, cand_train = tried[0]
            cid = f"{sid}-v{champ['version'] + 1}-{uuid.uuid4().hex[:4]}"
            cand = memory._new_skill(cid, f"{spec['title']} planner v{champ['version'] + 1}", cand_params,
                                     parent=champ["id"], scenario_id=sid, contexts=tags,
                                     description=f"Planner weights ({kind}) derived from {champ['id']} for {spec['title']}.")
            deltas = [cand_train[s] - champ_train[s] for s in train_seeds]
            wins = sum(d > 0 for d in deltas)
            ev_row = {"train_n": len(deltas), "train_delta": _mean(deltas), "wins": wins}
            outcome = "rejected"
            if _mean(deltas) > MIN_GAIN and wins > len(deltas) / 2:
                champ_hold = ev.scores(sid, holdout_seeds, "agent", champ["params"], champ["id"], "training")
                cand_hold = ev.scores(sid, holdout_seeds, "agent", cand_params, cid, "training")
                hold = [cand_hold[s] - champ_hold[s] for s in holdout_seeds]
                ev_row.update(holdout_n=len(hold), holdout_delta=_mean(hold))
                if _mean(hold) > 0 and min(hold) > -40:
                    outcome = "proven"
                    cand_all = {**cand_train, **cand_hold}
                    ev_row["vs_default"] = _mean(cand_all[s] - ref_default[s] for s in eval_seeds)
                else:
                    outcome = "rejected (did not hold up on held-out seeds)"
            cand["evidence"][sid] = ev_row
            cand["status"] = "proven" if outcome == "proven" else "rejected"
            cand["history"].append({"at": _now(), "event": f"round {r}: {outcome}; train Δ {ev_row['train_delta']:+.1f} "
                                                         f"({wins}/{len(deltas)} wins)"
                                                         + (f", held-out Δ {ev_row['holdout_delta']:+.1f}"
                                                            if 'holdout_delta' in ev_row else "")})
            with memory._lock:
                memory.skills[cid] = cand
                if outcome == "proven" and champ["id"] != DEFAULT_SKILL_ID:
                    champ["history"].append({"at": _now(), "event": f"superseded by {cid}"})
                    champ["updated_at"] = _now()
                    memory.write_skill_md(champ)
                memory._save_skills()
            memory.write_skill_md(cand)
            new_champ = memory.champion(sid)
            champ_eval = ev.scores(sid, eval_seeds, "agent", new_champ["params"], new_champ["id"], "training")
            point = {"round": r, "scenario_id": sid, "champion": new_champ["id"],
                     "agent": _mean(champ_eval.values()), "optimizer": _mean(ref_default.values()),
                     "fcfs": _mean(ref_fcfs.values()), "candidate": cid, "outcome": outcome,
                     "seeds": list(eval_seeds)}
            memory.add_curve_point(point)
            memory.reflect(sid)
            log.append(point)
    if own_evaluator:
        ev.close()
    return {"rounds": rounds, "scenarios": scenario_ids, "log": log}
