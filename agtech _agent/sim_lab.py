"""Simulation Lab: run any scenario from the library with any policy, record a
per-tick timeline for the dashboard, and score the run.

Every run uses a fresh ``farm_sim.FarmSimulator`` built from the scenario (the
tractor_fleet_sim engine), the scripted events, and the agronomic conditions in
``field_conditions.py``. All policies obey the same field conditions: weather and
soil rules are part of the environment, not of the strategy.

Policies:
    fcfs              farm_sim first-come-first-served (condition-aware)
    priority-nearest  farm_sim priority/nearest heuristic (condition-aware)
    optimizer         coordinator rollout optimizer, default weights
    agent             optimizer using the best *proven* skill from learning memory
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

import coordinator as c
import field_conditions as fc
import scenario_library as sl

POLICIES = ("fcfs", "priority-nearest", "optimizer", "agent")
PARAM_KEYS = ("w_priority", "w_urgency", "w_acres", "w_travel", "horizon")
DEFAULT_PARAMS = c.DEFAULT_CONFIG.weights()


def _digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def fingerprint(spec: dict[str, Any]) -> dict[str, Any]:
    """Same fingerprint ⇒ same scenario, events, conditions and horizon ⇒ comparable runs."""
    return {"scenario_id": spec["id"], "seed": spec.get("seed", 0),
            "scenario_hash": _digest(spec["scenario"]),
            "events_hash": _digest({"events": spec["events"], "conditions": spec.get("conditions")}),
            "horizon": int(spec["horizon"])}


def lab_score(sim: Any, env: fc.FieldEnvironment) -> float:
    """coordinator.score_state (on-time completions, priority, partial acres, minus
    travel and idle) minus one point per rain-damaged acre of hay."""
    return round(c.score_state(sim) - env.metrics()["rain_damaged_acres"], 1)


class Environment:
    """A fresh simulator + field conditions for one scenario variant."""

    def __init__(self, spec: dict[str, Any]):
        self.spec = spec
        self.sim = c.FarmSimulator(spec["scenario"])
        self.env = fc.FieldEnvironment(spec.get("conditions") or {}, self.sim.tick_minutes)
        self.schedule = c.EventSchedule(spec["events"])

    def config(self, params: dict[str, Any] | None = None) -> c.PlannerConfig:
        p = {**DEFAULT_PARAMS, **(params or {})}
        kw = {k: (int(p[k]) if k == "horizon" else float(p[k])) for k in PARAM_KEYS}
        if self.env.enabled:
            kw.update(job_gate=self.env.gate, tick_hook=self.env.rollout_hook)
        return c.PlannerConfig(**kw)

    def planner(self, policy: str, params: dict[str, Any] | None = None, source: str | None = None) -> c.Planner:
        cfg = self.config(params)
        if policy in ("fcfs", "priority-nearest"):
            return c.make_baseline_planner(policy, cfg)
        return c.make_optimizer_planner(cfg, source=source or policy)

    def signature(self, params: dict[str, Any] | None = None):
        cfg = self.config(params)

        def sig(sim: Any) -> tuple:
            return c.state_signature(sim) + (tuple(j.id for j in c.open_jobs(sim, cfg)),)
        return sig

    def sample(self) -> dict[str, Any]:
        sim, m = self.sim, self.sim.metrics()
        w = self.env.weather_at(sim.current_tick)
        return {
            "tick": sim.current_tick,
            "hour": round(self.env.hour_at(sim.current_tick), 2) if self.env.enabled else None,
            "acres": m["acres_completed"], "jobs": m["completed_jobs"], "on_time": m["on_time_jobs"],
            "idle": m["idle_tractor_ticks"], "travel": m["travel_cells"], "fuel": m["fuel_used_liters"],
            "wind": w["wind_mph"] if self.env.enabled else None,
            "rain": w["rain_mm_hr"] if self.env.enabled else None,
            "daylight": self.env.is_daylight(sim.current_tick) if self.env.enabled else True,
            "tractors": {t.id: [t.status, t.job_id] for t in sim.tractors.values()},
        }


def resolve(spec_or_id: str | dict[str, Any], seed: int = 0) -> dict[str, Any]:
    spec = spec_or_id if isinstance(spec_or_id, dict) else sl.load_library()[spec_or_id]
    return sl.make_variant(spec, seed)


def run_simulation(spec_or_id: str | dict[str, Any], policy: str = "optimizer", seed: int = 0,
                   params: dict[str, Any] | None = None, skill_id: str | None = None,
                   record_timeline: bool = True) -> dict[str, Any]:
    """Run one episode to the scenario horizon. Deterministic for a given input."""
    if policy not in POLICIES:
        raise ValueError(f"policy must be one of {POLICIES}")
    spec = resolve(spec_or_id, seed)
    e = Environment(spec)
    if policy == "optimizer":
        params = None
    planner = e.planner(policy, params, source="agent" if policy == "agent" else None)
    timeline = [e.sample()] if record_timeline else []

    def on_tick(sim: Any) -> None:
        e.env.after_tick(sim)
        if record_timeline:
            timeline.append(e.sample())

    t0 = time.time()
    decisions = c.run_episode(e.sim, planner, e.schedule, ticks=int(spec["horizon"]),
                              replan_every_tick=policy in ("fcfs", "priority-nearest"),
                              on_tick=on_tick, verbose=False,
                              signature=e.signature(params) if e.env.enabled else None)
    return build_result(e, policy, seed, params, skill_id, decisions, timeline, time.time() - t0)


def build_result(e: Environment, policy: str, seed: int, params: dict[str, Any] | None,
                 skill_id: str | None, decisions: list[Any], timeline: list[dict[str, Any]],
                 duration_s: float = 0.0, horizon: int | None = None) -> dict[str, Any]:
    """Uniform run summary used by lab runs, training and the live dashboard."""
    spec, sim = e.spec, e.sim
    tractors = {}
    for t in sim.tractors.values():
        done = [ev for ev in sim.event_log if ev["type"] == "job_completed" and ev["details"].get("tractor_id") == t.id]
        tractors[t.id] = {"implements": t.implements, "fuel_pct": round(t.fuel_pct, 1),
                          "jobs_completed": len(done),
                          "acres_completed": round(sum(ev["details"].get("acres", 0) for ev in done), 1),
                          "breakdowns": sum(1 for ev in sim.event_log if ev["type"] == "breakdown"
                                            and ev["details"].get("tractor_id") == t.id),
                          "halts": sum(1 for h in e.env.halt_log if h["tractor_id"] == t.id)}
    fp = fingerprint(spec)
    if horizon is not None:
        fp["horizon"] = int(horizon)
    return {
        "scenario_id": spec["id"], "title": spec.get("base_title", spec["title"]),
        "variant_title": spec["title"], "tags": spec.get("tags", []),
        "seed": seed, "policy": policy, "skill_id": skill_id,
        "params": {**DEFAULT_PARAMS, **(params or {})} if policy in ("optimizer", "agent") else None,
        "horizon": fp["horizon"], "tick_minutes": sim.tick_minutes,
        "final_tick": sim.current_tick,
        "fingerprint": fp,
        "metrics": {**sim.metrics(), **e.env.metrics()},
        "score": lab_score(sim, e.env),
        "jobs": [sim._job_state(j) for j in sim.jobs.values()],
        "tractors": tractors,
        "halts": e.env.halt_log[-40:],
        "events_applied": e.schedule.applied,
        "decisions": len(decisions),
        "decision_log": [{"tick": d.tick, "source": d.source, "trigger": d.trigger,
                          "explanation": d.explanation} for d in decisions[-25:]],
        "timeline": timeline,
        "duration_s": round(duration_s, 2),
    }
