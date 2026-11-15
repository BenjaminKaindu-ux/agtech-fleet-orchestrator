"""Deterministic core of the AI Farm Orchestrator.

Everything here is plain Python on top of the provided ``farm_sim`` simulator:

* ``load_simulator``      – finds ``tractor_fleet_sim/farm_sim.py`` and builds a sim
* ``optimized_plan``      – rollout-based planner (beats FCFS on travel/idle/jobs)
* ``plan_refuels``        – sends idle tractors to base when fuel blocks useful work
* ``explain_*``           – plain-language explanations of what changed and why
* ``EventSchedule``       – scripted breakdowns / blocked routes / priority changes
* ``run_episode``         – tick loop shared by the baselines and the LLM agent

The smolagents layer (``agent.py``) wraps these as tools, so the LLM always works
with validated numbers and the simulator remains the final safety gate.
"""

from __future__ import annotations

import copy
import itertools
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

PROJECT_DIR = Path(__file__).resolve().parent


# --------------------------------------------------------------------------- #
# Loading the simulator
# --------------------------------------------------------------------------- #
def find_sim_dir() -> Path:
    """Locate the folder holding farm_sim.py (env var, sibling folder, or local)."""
    candidates = [
        os.environ.get("TRACTOR_SIM_DIR"),
        PROJECT_DIR / "tractor_fleet_sim",
        PROJECT_DIR.parent / "tractor_fleet_sim",
    ]
    for cand in candidates:
        if cand and (Path(cand) / "farm_sim.py").is_file():
            return Path(cand).resolve()
    raise FileNotFoundError(
        "Could not find farm_sim.py. Extract tractor_fleet_sim.zip next to this "
        "project or set TRACTOR_SIM_DIR=/path/to/tractor_fleet_sim"
    )


SIM_DIR = find_sim_dir()
if str(SIM_DIR) not in sys.path:
    sys.path.insert(0, str(SIM_DIR))

import farm_sim  # noqa: E402
from farm_sim import (  # noqa: E402,F401
    FarmSimulator,
    first_come_first_served_plan,
    priority_nearest_plan,
)

DEFAULT_SCENARIO = PROJECT_DIR / "scenario.json"
DEFAULT_EVENTS = PROJECT_DIR / "events.json"


def load_simulator(scenario_path: str | Path | None = None) -> FarmSimulator:
    path = Path(scenario_path) if scenario_path else DEFAULT_SCENARIO
    if not path.is_file():
        path = SIM_DIR / "scenario.json"
    return FarmSimulator.from_file(path)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
OPEN_JOB = {"pending", "interrupted"}

JobGate = Callable[[FarmSimulator, Any], bool]


@dataclass(frozen=True)
class PlannerConfig:
    """Tunable planner settings. The defaults reproduce the original constants exactly.

    ``job_gate`` is an *environment* rule (weather, soil, job dependencies): it says
    whether a job may be dispatched right now. ``tick_hook`` replays the environment
    inside look-ahead rollouts so the optimizer can plan around a forecast. Both
    default to None, which is the original behaviour.
    """
    w_priority: float = 20.0
    w_urgency: float = 30.0
    w_acres: float = 0.5
    w_travel: float = 1.5
    horizon: int = 60
    max_candidates: int = 12
    job_gate: JobGate | None = None
    tick_hook: Callable[[FarmSimulator], None] | None = None

    def weights(self) -> dict[str, float]:
        return {"w_priority": self.w_priority, "w_urgency": self.w_urgency,
                "w_acres": self.w_acres, "w_travel": self.w_travel, "horizon": self.horizon}


DEFAULT_CONFIG = PlannerConfig()


def _cfg(cfg: PlannerConfig | None) -> PlannerConfig:
    return cfg or DEFAULT_CONFIG


def job_allowed(sim: FarmSimulator, job: Any, cfg: PlannerConfig | None = None) -> bool:
    gate = _cfg(cfg).job_gate
    return True if gate is None else bool(gate(sim, job))


def idle_tractors(sim: FarmSimulator) -> list[str]:
    return [t.id for t in sorted(sim.tractors.values(), key=lambda t: t.id)
            if t.status == "idle" and not t.job_id]


def open_jobs(sim: FarmSimulator, cfg: PlannerConfig | None = None) -> list[Any]:
    busy_fields = {j.field_id for j in sim.jobs.values()
                   if j.status in {"assigned", "in_progress"} and j.assigned_tractor_id}
    return [j for j in sorted(sim.jobs.values(), key=lambda j: j.created_order)
            if j.status in OPEN_JOB and not j.assigned_tractor_id and j.field_id not in busy_fields
            and job_allowed(sim, j, cfg)]


def pair_value(sim: FarmSimulator, job: Any, est: dict[str, Any],
               cfg: PlannerConfig | None = None) -> float:
    """Immediate value of sending a tractor to a job (higher is better)."""
    c = _cfg(cfg)
    slack = job.deadline_tick - est["estimated_completion_tick"]
    return (
        c.w_priority * (6 - job.priority)          # priority 1 is worth most
        + c.w_urgency / (1.0 + max(0, slack))      # urgent jobs first
        + c.w_acres * est["remaining_acres"]       # prefer productive work
        - c.w_travel * est["travel_ticks"]         # avoid long empty drives
    )


def feasible_options(sim: FarmSimulator, cfg: PlannerConfig | None = None) -> dict[str, list[tuple[float, str, dict]]]:
    """For each idle tractor, the feasible jobs it could start now, best first."""
    jobs = open_jobs(sim, cfg)
    out: dict[str, list[tuple[float, str, dict]]] = {}
    for tid in idle_tractors(sim):
        rows = []
        for job in jobs:
            est = sim.estimate_job(tid, job.id)
            if est["feasible"]:
                rows.append((pair_value(sim, job, est, cfg), job.id, est))
        rows.sort(key=lambda r: -r[0])
        out[tid] = rows
    return out


def candidate_matchings(sim: FarmSimulator, limit: int | None = None,
                        cfg: PlannerConfig | None = None) -> list[list[dict[str, str]]]:
    """Enumerate conflict-free tractor→job matchings, ranked by immediate value."""
    limit = _cfg(cfg).max_candidates if limit is None else limit
    opts = feasible_options(sim, cfg)
    tractors = list(opts)
    choices = [[None] + [(jid, v) for v, jid, _ in opts[t][:6]] for t in tractors]
    scored: list[tuple[float, list[dict[str, str]]]] = []
    for combo in itertools.product(*choices):
        jobs_used, fields_used, plan, total = set(), set(), [], 0.0
        ok = True
        for tid, pick in zip(tractors, combo):
            if pick is None:
                continue
            jid, val = pick
            fid = sim.jobs[jid].field_id
            if jid in jobs_used or fid in fields_used:
                ok = False
                break
            jobs_used.add(jid)
            fields_used.add(fid)
            plan.append({"tractor_id": tid, "job_id": jid})
            total += val
        if ok:
            scored.append((total, plan))
    scored.sort(key=lambda s: -s[0])
    seen, result = set(), []
    for _, plan in scored:
        key = tuple(sorted((a["tractor_id"], a["job_id"]) for a in plan))
        if key not in seen:
            seen.add(key)
            result.append(plan)
        if len(result) >= limit:
            break
    return result


def greedy_plan(sim: FarmSimulator, cfg: PlannerConfig | None = None) -> list[dict[str, str]]:
    plans = candidate_matchings(sim, limit=1, cfg=cfg)
    return plans[0] if plans else []


# --------------------------------------------------------------------------- #
# Fuel planning
# --------------------------------------------------------------------------- #
def refuel_candidates(sim: FarmSimulator, cfg: PlannerConfig | None = None) -> list[tuple[str, str]]:
    """Idle tractors that cannot do any open job *only* because of fuel.

    Returns (tractor_id, reason) pairs. A refuel is only worth it if, after
    driving to base and filling up, the tractor could still meet the deadline.
    """
    out = []
    opts = feasible_options(sim, cfg)
    for tid in idle_tractors(sim):
        if opts.get(tid):
            continue  # it has useful work right now
        t = sim.tractors[tid]
        if t.fuel_liters >= 0.9 * t.tank_liters:
            continue
        to_base = sim.find_path(t.position, sim.base)
        if to_base is None:
            continue
        refuel_ticks = int((t.tank_liters - t.fuel_liters) // t.refuel_liters_per_tick) + 1
        ready_tick = sim.current_tick + len(to_base) - 1 + refuel_ticks
        for job in open_jobs(sim, cfg):
            if job.required_implement not in t.implements:
                continue
            fpos = sim.fields[job.field_id]["position"]
            leg = sim.find_path(sim.base, fpos)
            if leg is None:
                continue
            rate = t.acres_per_hour[job.required_implement]
            work_ticks = -(-job.remaining_acres / rate // sim.tick_hours)
            need = (2 * (len(leg) - 1) * t.travel_liters_per_cell
                    + job.remaining_acres / rate * t.work_liters_per_hour + t.fuel_reserve_liters)
            eta = ready_tick + len(leg) - 1 + work_ticks
            if t.fuel_liters < need <= t.tank_liters and eta <= job.deadline_tick:
                out.append((tid, f"{tid} has {t.fuel_liters:.0f} L but {job.id} needs "
                                 f"{need:.0f} L incl. reserve; refuel now and it can still "
                                 f"finish by t{int(eta)} (deadline t{job.deadline_tick})"))
                break
    return out


def plan_refuels(sim: FarmSimulator, log: Callable[[str], None] | None = None,
                 cfg: PlannerConfig | None = None) -> list[dict[str, Any]]:
    done = []
    for tid, reason in refuel_candidates(sim, cfg):
        res = sim.request_refuel(tid, target_pct=100)
        if res.get("ok"):
            done.append({"tractor_id": tid, "reason": reason})
            if log:
                log(f"Refuel: {reason}.")
    return done


# --------------------------------------------------------------------------- #
# Rollout evaluation (look-ahead)
# --------------------------------------------------------------------------- #
def score_state(sim: FarmSimulator) -> float:
    """Single number for comparing futures: useful work minus waste."""
    score = 0.0
    for j in sim.jobs.values():
        if j.status == "completed":
            on_time = j.completed_tick is not None and j.completed_tick <= j.deadline_tick
            score += (100.0 if on_time else 40.0) + 15.0 * (6 - j.priority)
        else:
            score += 1.0 * j.completed_acres
    m = sim.counters
    return score - 0.6 * m["travel_cells"] - 0.3 * m["idle_tractor_ticks"]


def _quiet_copy(sim: FarmSimulator) -> FarmSimulator:
    clone = copy.deepcopy(sim)
    clone._log = lambda *a, **k: None  # type: ignore[method-assign]
    return clone


def rollout(sim: FarmSimulator, first_plan: list[dict[str, str]], horizon: int | None = None,
            cfg: PlannerConfig | None = None) -> float:
    """Commit ``first_plan`` on a copy, then follow the greedy policy to the horizon."""
    c = _cfg(cfg)
    horizon = c.horizon if horizon is None else horizon
    clone = _quiet_copy(sim)
    if first_plan and not clone.commit_plan(first_plan, clone.version).get("accepted"):
        return float("-inf")
    plan_refuels(clone, cfg=c)
    for _ in range(horizon):
        if all(j.status == "completed" for j in clone.jobs.values()):
            break
        clone.advance(1)
        if c.tick_hook:
            c.tick_hook(clone)
        if idle_tractors(clone):
            nxt = greedy_plan(clone, c)
            if nxt:
                clone.commit_plan(nxt, clone.version)
            plan_refuels(clone, cfg=c)
    return score_state(clone)


def optimized_plan(sim: FarmSimulator, horizon: int | None = None,
                   cfg: PlannerConfig | None = None) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """Pick the matching whose simulated future scores best.

    Returns (best_plan, ranked_alternatives) where each alternative has its score.
    """
    cands = candidate_matchings(sim, cfg=cfg)
    if not cands:
        return [], []
    ranked = sorted(({"plan": p, "score": round(rollout(sim, p, horizon, cfg), 1)} for p in cands),
                    key=lambda r: -r["score"])
    return ranked[0]["plan"], ranked


# --------------------------------------------------------------------------- #
# Explanations
# --------------------------------------------------------------------------- #
@dataclass
class Decision:
    tick: int
    trigger: str
    assignments: list[dict[str, Any]]
    refuels: list[dict[str, Any]]
    explanation: str
    source: str  # "llm", "optimizer", "fallback", "fcfs", ...
    alternatives: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def explain_assignment(sim: FarmSimulator, tractor_id: str, job_id: str,
                       est: dict[str, Any] | None = None) -> str:
    """Plain-language reason for one tractor→job pairing."""
    est = est or sim.estimate_job(tractor_id, job_id)
    job, t = sim.jobs[job_id], sim.tractors[tractor_id]
    field_name = sim.fields[job.field_id].get("name", job.field_id)
    others = [o.id for o in sim.tractors.values()
              if o.id != tractor_id and job.required_implement in o.implements]
    slack = job.deadline_tick - est.get("estimated_completion_tick", sim.current_tick)
    fuel_margin = est.get("fuel_available_liters", t.fuel_liters) - est.get("fuel_needed_liters", 0)
    bits = [
        f"{tractor_id} → {job_id} ({job.job_type}, {field_name}, P{job.priority})",
        f"{tractor_id} has the {job.required_implement}"
        + (f" (also: {', '.join(others)})" if others else " (the only one that does)"),
        f"{est.get('travel_cells', '?')} cells away, finishes ~t{est.get('estimated_completion_tick')}"
        f" vs deadline t{job.deadline_tick} ({slack} ticks spare)",
        f"fuel margin {fuel_margin:.0f} L after return-to-base reserve",
    ]
    if job.completed_acres > 0:
        bits.append(f"resumes interrupted work: {job.completed_acres:.1f} ac already done are kept, "
                    f"{job.remaining_acres:.1f} ac remain")
    return "; ".join(bits) + "."


def describe_trigger(events: list[dict[str, Any]]) -> str:
    """Summarise the simulator events that caused a replan."""
    interesting = {"breakdown", "job_interrupted", "route_blocked", "route_opened",
                   "priority_changed", "repair", "tractor_blocked", "job_completed",
                   "refuel_complete", "fuel_empty"}
    msgs = [e["message"] for e in events if e["type"] in interesting]
    return "; ".join(dict.fromkeys(msgs)) if msgs else "tractor(s) available"


def explain_plan(sim: FarmSimulator, plan: list[dict[str, str]], trigger: str,
                 estimates: list[dict[str, Any]] | None = None) -> str:
    by_pair = {(e["tractor_id"], e["job_id"]): e for e in (estimates or [])}
    lines = [f"[t{sim.current_tick}] Why replanning: {trigger}."]
    for a in plan:
        lines.append("  • " + explain_assignment(sim, a["tractor_id"], a["job_id"],
                                                 by_pair.get((a["tractor_id"], a["job_id"]))))
    waiting = [j.id for j in open_jobs(sim) if j.id not in {a["job_id"] for a in plan}]
    if waiting:
        lines.append(f"  • Still queued: {', '.join(waiting)} (no compatible tractor free, or a "
                     "better use of the free tractor was found).")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Event schedule
# --------------------------------------------------------------------------- #
class EventSchedule:
    """Scripted challenge events keyed by tick. Retries next tick if rejected
    (for example, blocking a cell a tractor currently stands on)."""

    def __init__(self, events: list[dict[str, Any]] | None = None):
        self.pending = sorted((dict(e) for e in (events or [])), key=lambda e: e["tick"])
        self.applied: list[dict[str, Any]] = []

    @classmethod
    def from_file(cls, path: str | Path | None) -> "EventSchedule":
        if not path or not Path(path).is_file():
            return cls([])
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return cls(data.get("events", data) if isinstance(data, dict) else data)

    def apply_due(self, sim: FarmSimulator) -> list[dict[str, Any]]:
        fired, keep = [], []
        for ev in self.pending:
            if ev["tick"] <= sim.current_tick:
                payload = {k: v for k, v in ev.items() if k not in {"tick", "name", "note"}}
                result = sim.event(ev["name"], **payload)
                if result.get("ok"):
                    fired.append({**ev, "applied_tick": sim.current_tick})
                else:
                    keep.append(ev)  # try again next tick
            else:
                keep.append(ev)
        self.pending = keep
        self.applied.extend(fired)
        return fired


# --------------------------------------------------------------------------- #
# Episode runner
# --------------------------------------------------------------------------- #
Planner = Callable[[FarmSimulator, str], "Decision | None"]


def state_signature(sim: FarmSimulator) -> tuple:
    """Changes in this tuple mean a new dispatch decision may be needed."""
    return (
        tuple(idle_tractors(sim)),
        tuple(j.id for j in open_jobs(sim)),
        tuple(sorted(sim.blocked_cells)),
        tuple((j.id, j.priority) for j in sim.jobs.values()),
    )


def workday_over(sim: FarmSimulator) -> bool:
    """True when every job is done or can no longer be finished on time and no
    tractor is still working. Idle time after this point is not counted for any
    policy, so the comparison only measures idle time while work was available."""
    if any(t.job_id for t in sim.tractors.values()):
        return False
    return all(j.status == "completed" or sim.current_tick >= j.deadline_tick
               for j in sim.jobs.values())


def run_episode(sim: FarmSimulator, planner: Planner, events: EventSchedule,
                ticks: int = 60, replan_every_tick: bool = False,
                on_tick: Callable[[FarmSimulator], None] | None = None,
                verbose: bool = True,
                signature: Callable[[FarmSimulator], tuple] | None = None) -> list[Decision]:
    """Advance ``ticks`` ticks, applying events and asking ``planner`` to dispatch.

    The planner is consulted when the dispatch-relevant state changes (an event,
    a tractor becoming free, a job completing) – not every tick – which keeps
    LLM calls low. Baselines use ``replan_every_tick=True`` like farm_sim.main.
    ``signature`` overrides what counts as a state change (e.g. include which jobs
    the weather currently allows); the default is ``state_signature``.
    """
    sig_fn = signature or state_signature
    decisions: list[Decision] = []
    last_sig, log_cursor = None, len(sim.event_log)
    for _ in range(ticks):
        fired = events.apply_due(sim)
        for ev in fired:
            if verbose:
                print(f"[t{sim.current_tick}] EVENT {ev['name']}: "
                      f"{ {k: v for k, v in ev.items() if k not in {'tick', 'name', 'applied_tick'}} }")
        sig = sig_fn(sim)
        if idle_tractors(sim) and open_jobs(sim) and (replan_every_tick or sig != last_sig or fired):
            trigger = describe_trigger(sim.event_log[log_cursor:])
            log_cursor = len(sim.event_log)
            decision = planner(sim, trigger)
            if decision and (decision.assignments or decision.refuels):
                decisions.append(decision)
                if verbose:
                    print(decision.explanation)
            last_sig = sig_fn(sim)
        if workday_over(sim) and not events.pending:
            break
        sim.advance(1)
        if on_tick:
            on_tick(sim)
    return decisions


# --------------------------------------------------------------------------- #
# Non-LLM planners (baselines and the optimizer used as fallback)
# --------------------------------------------------------------------------- #
def _commit(sim: FarmSimulator, plan: list[dict[str, str]], trigger: str, source: str,
            refuels: list[dict[str, Any]] | None = None,
            alternatives: list[dict[str, Any]] | None = None) -> Decision | None:
    est = []
    if plan:
        res = sim.commit_plan(plan, sim.version)
        if not res.get("accepted"):
            return None
        est = res.get("estimates", [])
    text = explain_plan(sim, plan, trigger, est)
    for r in refuels or []:
        text += f"\n  • Refuel: {r['reason']}."
    sim._log("plan_explanation", text.replace("\n", " "), {"source": source})
    return Decision(sim.current_tick, trigger, est or [], refuels or [], text, source, alternatives or [])


def fcfs_planner(sim: FarmSimulator, trigger: str) -> Decision | None:
    plan = first_come_first_served_plan(sim)
    return _commit(sim, plan, trigger, "fcfs") if plan else None


def priority_nearest_planner(sim: FarmSimulator, trigger: str) -> Decision | None:
    plan = priority_nearest_plan(sim)
    return _commit(sim, plan, trigger, "priority-nearest") if plan else None


def gated_baseline_plan(sim: FarmSimulator, gate: JobGate, order: str) -> list[dict[str, str]]:
    """FCFS / priority-nearest exactly as in farm_sim, but skipping jobs the
    environment forbids right now (a real operator would not spray in a gale)."""
    available = [t.id for t in sorted(sim.tractors.values(), key=lambda t: t.id)
                 if t.status == "idle" and not t.job_id]
    key = ((lambda j: j.created_order) if order == "fcfs"
           else (lambda j: (j.priority, j.deadline_tick, j.created_order)))
    candidates = sorted((j for j in sim.jobs.values() if j.status in OPEN_JOB
                         and not j.assigned_tractor_id and gate(sim, j)), key=key)
    plan: list[dict[str, str]] = []
    fields: set[str] = set()
    for job in candidates:
        if job.field_id in fields:
            continue
        if order == "fcfs":
            pick = next((tid for tid in available if sim.estimate_job(tid, job.id)["feasible"]), None)
        else:
            opts = []
            for tid in available:
                est = sim.estimate_job(tid, job.id)
                if est["feasible"]:
                    opts.append((est["travel_ticks"], est["fuel_needed_liters"], tid))
            pick = min(opts)[2] if opts else None
        if pick:
            plan.append({"tractor_id": pick, "job_id": job.id})
            available.remove(pick)
            fields.add(job.field_id)
    return plan


def make_baseline_planner(order: str, cfg: PlannerConfig | None = None) -> Planner:
    """``order`` is "fcfs" or "priority-nearest". Without a gate this is the farm_sim policy."""
    c = _cfg(cfg)
    if c.job_gate is None:
        return fcfs_planner if order == "fcfs" else priority_nearest_planner

    def planner(sim: FarmSimulator, trigger: str) -> Decision | None:
        plan = gated_baseline_plan(sim, c.job_gate, order)  # type: ignore[arg-type]
        return _commit(sim, plan, trigger, order) if plan else None
    return planner


def make_optimizer_planner(cfg: PlannerConfig | None = None, source: str = "optimizer") -> Planner:
    c = _cfg(cfg)

    def planner(sim: FarmSimulator, trigger: str) -> Decision | None:
        return optimizer_planner(sim, trigger, source=source, cfg=c)
    return planner


def optimizer_planner(sim: FarmSimulator, trigger: str, source: str = "optimizer",
                      cfg: PlannerConfig | None = None) -> Decision | None:
    plan, ranked = optimized_plan(sim, cfg=cfg)
    decision = _commit(sim, plan, trigger, source, alternatives=ranked[:3]) if plan else None
    refuels = plan_refuels(sim, cfg=cfg)
    if refuels:
        if decision:
            decision.refuels = refuels
            decision.explanation += "".join(f"\n  • Refuel: {r['reason']}." for r in refuels)
        else:
            decision = _commit(sim, [], trigger, source, refuels=refuels)
    return decision


def report(sim: FarmSimulator, decisions: list[Decision], events: EventSchedule,
           policy: str) -> dict[str, Any]:
    return {
        "policy": policy,
        "scenario": sim.scenario.get("name"),
        "final_tick": sim.current_tick,
        "metrics": sim.metrics(),
        "jobs": [sim._job_state(j) for j in sim.jobs.values()],
        "events_applied": events.applied,
        "decisions": [d.as_dict() for d in decisions],
        "event_log": sim.event_log,
    }
