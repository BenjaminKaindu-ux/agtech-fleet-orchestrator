"""Lightweight, offline memory for the tractor fleet agents.

* Storage is one local JSONL file (one record per line). No hosted services.
  Path: ``FLEET_MEMORY_PATH`` env var, else ``data/fleet_memory.jsonl`` next to
  this file. Every store can also be given an explicit path.
* Every model-written lesson is stored with ``status: "candidate"``. Nothing in
  this module promotes a record to a rule; promotion is a human decision.
* Records separate ``measured`` (copied from the simulator and the comparison
  registry by code) from ``interpretation`` (the lesson text the model wrote).
* Current simulator state is authoritative. Retrieved memories are returned as
  advisory context, and the planners still validate everything against the sim.
* An improvement claim is only accepted when the baseline and the coordinator
  run share the same scenario, event schedule and time horizon (checked by
  fingerprint in :class:`ComparisonRegistry`).

Tools are built with factories that take the *existing* simulator instance.
They never construct a simulator of their own.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from smolagents import CodeAgent, Tool

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_MEMORY_PATH = PROJECT_DIR / "data" / "fleet_memory.jsonl"
SCHEMA_VERSION = 1
MAX_RESULTS = 5
MAX_LESSON_CHARS = 300
MAX_STORED_EVENTS = 12

# Metrics copied into records (keeps lines short; all come from sim.metrics()).
METRIC_KEYS = (
    "completed_jobs", "on_time_jobs", "late_completed_jobs", "unfinished_jobs",
    "acres_completed", "completion_rate_pct", "travel_cells", "idle_tractor_ticks",
    "fuel_used_liters", "breakdowns", "dispatches", "replans",
)
# Event types worth remembering; routine movement/refuel chatter is skipped.
NOTABLE_EVENTS = {
    "breakdown", "repair", "job_interrupted", "route_blocked", "route_opened",
    "route_replanned", "tractor_blocked", "priority_changed", "fuel_empty",
    "deadline_missed", "job_completed", "plan_rejected", "refuel_requested",
}
# Tools the assessment agent must never hold.
FORBIDDEN_ASSESSMENT_TOOLS = {
    "commit_dispatch_plan", "report_tractor_breakdown", "repair_tractor",
    "report_blocked_cell", "reopen_cell", "change_job_priority",
    "request_refuel", "advance_simulation", "validate_dispatch_plan",
}
_IMPROVEMENT_CLAIM = re.compile(
    r"\b(better|worse|outperform\w*|underperform\w*|improv\w*|beat|beats|superior|"
    r"baseline|fcfs|compared (to|with)|vs\.?|versus)\b", re.IGNORECASE)


def resolve_memory_path(path: str | Path | None = None) -> Path:
    """Explicit path > FLEET_MEMORY_PATH env var > data/fleet_memory.jsonl."""
    if path:
        return Path(path).expanduser().resolve()
    env = os.environ.get("FLEET_MEMORY_PATH")
    return Path(env).expanduser().resolve() if env else DEFAULT_MEMORY_PATH


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm(s: Any) -> str:
    return str(s or "").strip().lower()


# --------------------------------------------------------------------------- #
# JSONL store
# --------------------------------------------------------------------------- #
class MemoryStore:
    """Append-only JSONL file. Malformed lines are skipped on read."""

    def __init__(self, path: str | Path | None = None):
        self.path = resolve_memory_path(path)
        self._lock = threading.Lock()

    def append(self, record: dict[str, Any]) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
        with self._lock, open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return record

    def all(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        out = []
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
        return out

    def upsert(self, key: str, build: "Callable[[dict[str, Any] | None], dict[str, Any] | None]") -> dict[str, Any] | None:
        """Create or update the record whose ``key`` matches (rewrites the file atomically).

        ``build`` receives the existing record (or None) and returns the new record,
        or None to leave the file unchanged. Used for measured insights that are
        refined as more runs are observed, instead of appending near-duplicates."""
        with self._lock:
            records = self.all()
            idx = next((i for i, r in enumerate(records) if r.get("key") == key), None)
            new = build(records[idx] if idx is not None else None)
            if new is None:
                return None
            new["key"] = key
            if idx is None:
                records.append(new)
            else:
                records[idx] = new
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                for r in records:
                    fh.write(json.dumps(r, separators=(",", ":"), ensure_ascii=False) + "\n")
            os.replace(tmp, self.path)
            return new

    def search(self, tractor_id: str | None = None, event_type: str | None = None,
               operation: str | None = None, scenario: str | None = None,
               limit: int = MAX_RESULTS) -> list[dict[str, Any]]:
        """Records matching at least one filter, most matches then newest first."""
        limit = max(1, min(int(limit), MAX_RESULTS))
        t, e, o, s = map(_norm, (tractor_id, event_type, operation, scenario))
        if not any((t, e, o, s)):
            return []
        scored = []
        for idx, rec in enumerate(self.all()):
            tags = rec.get("tags", {})
            score = 0
            if t and t in map(_norm, tags.get("tractors", [])):
                score += 1
            if e and e in map(_norm, tags.get("event_types", [])):
                score += 1
            if o and any(o in _norm(op) or _norm(op) in o for op in tags.get("operations", []) if op):
                score += 1
            if s and s in _norm(rec.get("scenario")):
                score += 1
            if score:
                scored.append((score, idx, rec))
        scored.sort(key=lambda r: (-r[0], -r[1]))
        return [r for _, _, r in scored[:limit]]


# --------------------------------------------------------------------------- #
# Comparison registry (decides whether an improvement claim is allowed)
# --------------------------------------------------------------------------- #
def _digest(obj: Any) -> str:
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def events_as_list(events: Any) -> list[dict[str, Any]]:
    """Normalise an EventSchedule, a {"events": [...]} dict, a list, or a path."""
    if events is None:
        return []
    if isinstance(events, (str, Path)):
        p = Path(events)
        if not p.is_file():
            return []
        events = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(events, dict):
        events = events.get("events", [])
    if hasattr(events, "pending") and hasattr(events, "applied"):  # EventSchedule
        events = list(events.applied) + list(events.pending)
    rows = [{k: v for k, v in dict(ev).items() if k not in {"applied_tick", "note"}}
            for ev in events]
    return sorted(rows, key=lambda ev: (ev.get("tick", 0), json.dumps(ev, sort_keys=True)))


@dataclass
class RunFingerprint:
    scenario_name: str
    scenario_hash: str
    events_hash: str
    horizon_ticks: int

    @classmethod
    def build(cls, scenario: dict[str, Any], events: Any, horizon_ticks: int) -> "RunFingerprint":
        return cls(str(scenario.get("name", "")), _digest(scenario),
                   _digest(events_as_list(events)), int(horizon_ticks))


@dataclass
class Comparison:
    comparison_id: str
    baseline_policy: str | None = None
    baseline_metrics: dict[str, Any] | None = None
    baseline_fp: RunFingerprint | None = None
    coordinator_policy: str | None = None
    coordinator_fp: RunFingerprint | None = None

    def check(self) -> tuple[bool, list[str]]:
        reasons = []
        if not self.baseline_fp:
            reasons.append("no baseline registered")
        if not self.coordinator_fp:
            reasons.append("no coordinator run registered")
        if self.baseline_fp and self.coordinator_fp:
            b, c = self.baseline_fp, self.coordinator_fp
            if b.scenario_hash != c.scenario_hash:
                reasons.append("scenario differs")
            if b.events_hash != c.events_hash:
                reasons.append("event schedule differs")
            if b.horizon_ticks != c.horizon_ticks:
                reasons.append(f"horizon differs ({b.horizon_ticks} vs {c.horizon_ticks} ticks)")
        return not reasons, reasons


class ComparisonRegistry:
    """In-process record of baseline vs coordinator runs, filled by code, not the LLM."""

    def __init__(self) -> None:
        self._items: dict[str, Comparison] = {}

    def get(self, comparison_id: str) -> Comparison | None:
        return self._items.get(comparison_id)

    def _item(self, comparison_id: str) -> Comparison:
        return self._items.setdefault(comparison_id, Comparison(comparison_id))

    def register_baseline(self, comparison_id: str, policy: str, metrics: dict[str, Any],
                          scenario: dict[str, Any], events: Any, horizon_ticks: int) -> Comparison:
        c = self._item(comparison_id)
        c.baseline_policy = policy
        c.baseline_metrics = {k: metrics.get(k) for k in METRIC_KEYS if k in metrics}
        c.baseline_fp = RunFingerprint.build(scenario, events, horizon_ticks)
        return c

    def register_baseline_report(self, comparison_id: str, report: dict[str, Any],
                                 scenario: dict[str, Any], events: Any,
                                 horizon_ticks: int) -> Comparison:
        """Convenience for the dict returned by ``coordinator.report(...)``."""
        return self.register_baseline(comparison_id, report.get("policy", "baseline"),
                                      report["metrics"], scenario, events, horizon_ticks)

    def register_coordinator_run(self, comparison_id: str, sim: Any, events: Any,
                                 horizon_ticks: int, policy: str = "coordinator") -> Comparison:
        c = self._item(comparison_id)
        c.coordinator_policy = policy
        c.coordinator_fp = RunFingerprint.build(sim.scenario, events, horizon_ticks)
        return c


# --------------------------------------------------------------------------- #
# Snapshot helpers (measured facts, read from the live simulator)
# --------------------------------------------------------------------------- #
def observed_metrics(sim: Any) -> dict[str, Any]:
    m = sim.metrics()
    return {k: m[k] for k in METRIC_KEYS if k in m}


def compact_events(sim: Any, limit: int = MAX_STORED_EVENTS) -> list[dict[str, Any]]:
    rows = []
    for ev in sim.event_log:
        if ev.get("type") not in NOTABLE_EVENTS:
            continue
        d = ev.get("details", {}) or {}
        row = {"tick": ev.get("tick"), "type": ev.get("type")}
        for k in ("tractor_id", "job_id", "position"):
            if d.get(k) is not None:
                row[k] = d[k]
        rows.append(row)
    return rows[-limit:]


def _derive_tags(sim: Any, events: list[dict[str, Any]], extra: dict[str, list[str]]) -> dict[str, list[str]]:
    tractors, types, ops = set(), set(), set()
    for ev in events:
        types.add(ev["type"])
        if ev.get("tractor_id"):
            tractors.add(str(ev["tractor_id"]))
        job = sim.jobs.get(ev.get("job_id")) if ev.get("job_id") else None
        if job is not None:
            ops.update({job.job_type, job.required_implement})
    # Explicit tags from the lesson author are more specific than everything
    # that appears in the recent event window, so they replace derived ones.
    if extra.get("tractors"):
        tractors = set(extra["tractors"])
    if extra.get("operations"):
        ops = set(extra["operations"])
    types.update(extra.get("event_types", []))
    clean = lambda s: sorted({x.strip() for x in s if x and x.strip()})  # noqa: E731
    return {"tractors": clean(tractors), "event_types": clean(types), "operations": clean(ops)}


def _split_csv(s: str | None) -> list[str]:
    return [p.strip() for p in (s or "").split(",") if p.strip()]


# --------------------------------------------------------------------------- #
# smolagents tools (each bound to the existing simulator instance)
# --------------------------------------------------------------------------- #
class RetrieveFleetMemoryTool(Tool):
    name = "retrieve_fleet_memory"
    description = (
        "Look up at most five past candidate lessons matching a tractor, event type, "
        "operation (job type or implement), or scenario. Results are advisory, unverified "
        "candidates: the current simulator state always wins, and every plan must still be "
        "estimated and validated against the simulator."
    )
    inputs = {
        "tractor_id": {"type": "string", "description": "Tractor ID such as T2.", "nullable": True},
        "event_type": {"type": "string", "description": "Simulator event type such as breakdown or route_blocked.", "nullable": True},
        "operation": {"type": "string", "description": "Job type or implement, e.g. mow, seed, sprayer.", "nullable": True},
        "scenario": {"type": "string", "description": "Scenario name (substring match).", "nullable": True},
    }
    output_type = "string"

    def __init__(self, sim: Any, store: MemoryStore):
        super().__init__()
        self.sim, self.store = sim, store

    def forward(self, tractor_id: str | None = None, event_type: str | None = None,
                operation: str | None = None, scenario: str | None = None) -> str:
        hits = self.store.search(tractor_id, event_type, operation, scenario, limit=MAX_RESULTS)
        return json.dumps({
            "notice": ("Candidate memories only; not rules. Current simulator state "
                       f"(tick {self.sim.current_tick}, version {self.sim.version}) is authoritative."),
            "count": len(hits),
            "records": [{
                "id": r.get("id"), "status": r.get("status"), "created_at": r.get("created_at"),
                "scenario": r.get("scenario"), "tags": r.get("tags"),
                "lesson": (r.get("interpretation") or {}).get("lesson"),
                "comparable": ((r.get("measured") or {}).get("comparison") or {}).get("comparable"),
                "deltas_vs_baseline": ((r.get("measured") or {}).get("comparison") or {}).get("deltas"),
            } for r in hits],
        })


class SaveCandidateMemoryTool(Tool):
    name = "save_candidate_memory"
    description = (
        "Save one short candidate lesson after a run. Code attaches the timestamp, scenario, "
        "observed simulator metrics, recent simulator events, and the registered baseline "
        "metrics for comparison_id. The record is stored as 'candidate' and is never a rule. "
        "Lessons that claim one strategy did better are rejected unless baseline and "
        "coordinator used the same scenario, events, and time horizon."
    )
    inputs = {
        "lesson": {"type": "string", "description": f"One or two sentences, max {MAX_LESSON_CHARS} chars. Interpretation only."},
        "comparison_id": {"type": "string", "description": "ID of the registered baseline-vs-coordinator comparison."},
        "claims_improvement": {"type": "boolean", "description": "True if the lesson says a strategy performed better.", "nullable": True},
        "tractor_ids": {"type": "string", "description": "Optional comma-separated extra tractor tags.", "nullable": True},
        "operations": {"type": "string", "description": "Optional comma-separated extra operation tags.", "nullable": True},
    }
    output_type = "string"

    def __init__(self, sim: Any, store: MemoryStore, registry: ComparisonRegistry,
                 author: str = "assessment_agent"):
        super().__init__()
        self.sim, self.store, self.registry, self.author = sim, store, registry, author

    def forward(self, lesson: str, comparison_id: str, claims_improvement: bool | None = None,
                tractor_ids: str | None = None, operations: str | None = None) -> str:
        lesson = " ".join(str(lesson or "").split())
        if not lesson:
            return json.dumps({"saved": False, "error": "lesson is empty"})
        if len(lesson) > MAX_LESSON_CHARS:
            return json.dumps({"saved": False, "error": f"lesson exceeds {MAX_LESSON_CHARS} characters"})

        comp = self.registry.get(comparison_id)
        comparable, reasons = comp.check() if comp else (False, [f"unknown comparison_id {comparison_id!r}"])
        claims = bool(claims_improvement) or bool(_IMPROVEMENT_CLAIM.search(lesson))
        if claims and not comparable:
            return json.dumps({"saved": False, "error": (
                "Lesson claims a performance difference but the runs are not comparable: "
                + "; ".join(reasons) + ". Rephrase as an observation without comparison.")})
        if comp and comp.coordinator_fp and comp.coordinator_fp.scenario_hash != _digest(self.sim.scenario):
            return json.dumps({"saved": False, "error": "bound simulator's scenario differs from the coordinator run registered for this comparison"})

        observed = observed_metrics(self.sim)
        baseline = comp.baseline_metrics if comp else None
        deltas = None
        if comparable and baseline:
            deltas = {k: round(observed[k] - baseline[k], 2) for k in observed
                      if isinstance(baseline.get(k), (int, float)) and isinstance(observed[k], (int, float))}
        events = compact_events(self.sim)
        record = {
            "id": f"mem-{uuid.uuid4().hex[:10]}",
            "schema": SCHEMA_VERSION,
            "status": "candidate",
            "created_at": _now(),
            "scenario": self.sim.scenario.get("name"),
            "comparison_id": comparison_id,
            "tags": _derive_tags(self.sim, events, {
                "tractors": _split_csv(tractor_ids), "operations": _split_csv(operations)}),
            "measured": {
                "sim_tick": self.sim.current_tick,
                "state_version": self.sim.version,
                "observed_metrics": observed,
                "baseline": {"policy": comp.baseline_policy, "metrics": baseline} if baseline else None,
                "comparison": {
                    "comparable": comparable,
                    "reasons": reasons,
                    "horizon_ticks": comp.coordinator_fp.horizon_ticks if comp and comp.coordinator_fp else None,
                    "deltas": deltas,  # observed - baseline; only when comparable
                },
                "recent_events": events,
            },
            "interpretation": {"lesson": lesson, "author": self.author, "claims_improvement": claims},
        }
        self.store.append(record)
        return json.dumps({"saved": True, "id": record["id"], "status": "candidate",
                           "comparable": comparable, "path": str(self.store.path)})


class GetRunMetricsTool(Tool):
    name = "get_run_metrics"
    description = "Read-only: current simulator metrics, tick, and state version."
    inputs: dict[str, Any] = {}
    output_type = "string"

    def __init__(self, sim: Any):
        super().__init__()
        self.sim = sim

    def forward(self) -> str:
        return json.dumps({"tick": self.sim.current_tick, "state_version": self.sim.version,
                           "metrics": self.sim.metrics()})


class GetRecentEventsTool(Tool):
    name = "get_recent_events"
    description = "Read-only: recent notable simulator events (breakdowns, blocked routes, deadline misses, ...)."
    inputs = {"limit": {"type": "integer", "description": "Max events, 1 to 40.", "nullable": True}}
    output_type = "string"

    def __init__(self, sim: Any):
        super().__init__()
        self.sim = sim

    def forward(self, limit: int | None = None) -> str:
        n = max(1, min(int(limit or 20), 40))
        return json.dumps({"tick": self.sim.current_tick, "events": compact_events(self.sim, n)})


class GetComparisonTool(Tool):
    name = "get_comparison"
    description = ("Read-only: baseline metrics, observed metrics, and whether the baseline and coordinator "
                   "runs are comparable (same scenario, events, horizon) for a comparison_id.")
    inputs = {"comparison_id": {"type": "string", "description": "Registered comparison ID."}}
    output_type = "string"

    def __init__(self, sim: Any, registry: ComparisonRegistry):
        super().__init__()
        self.sim, self.registry = sim, registry

    def forward(self, comparison_id: str) -> str:
        comp = self.registry.get(comparison_id)
        if not comp:
            return json.dumps({"found": False, "comparison_id": comparison_id})
        ok, reasons = comp.check()
        return json.dumps({
            "found": True, "comparison_id": comparison_id, "comparable": ok, "reasons": reasons,
            "baseline_policy": comp.baseline_policy, "baseline_metrics": comp.baseline_metrics,
            "coordinator_policy": comp.coordinator_policy, "observed_metrics": observed_metrics(self.sim),
            "horizon_ticks": {"baseline": comp.baseline_fp.horizon_ticks if comp.baseline_fp else None,
                              "coordinator": comp.coordinator_fp.horizon_ticks if comp.coordinator_fp else None},
        })


# --------------------------------------------------------------------------- #
# Factories
# --------------------------------------------------------------------------- #
def make_retrieval_tool(sim: Any, store: MemoryStore | None = None) -> RetrieveFleetMemoryTool:
    return RetrieveFleetMemoryTool(sim, store or MemoryStore())


def make_assessment_tools(sim: Any, store: MemoryStore, registry: ComparisonRegistry) -> list[Tool]:
    return [GetRunMetricsTool(sim), GetRecentEventsTool(sim), GetComparisonTool(sim, registry),
            RetrieveFleetMemoryTool(sim, store), SaveCandidateMemoryTool(sim, store, registry)]


ASSESSMENT_INSTRUCTIONS = (
    "You assess a finished fleet run. Read metrics, recent events, and the comparison for the "
    "given comparison_id, optionally check related past memories, then save at most two short "
    "candidate lessons with save_candidate_memory. Base lessons only on the numbers and events the "
    "tools return. Only say a strategy performed better if get_comparison reports comparable=true, "
    "and then set claims_improvement=true. Otherwise describe what happened without comparing. "
    "You cannot and must not control tractors, dispatch jobs, advance time, or change any agent's "
    "instructions; saved lessons are candidates for human review, not rules."
)


def build_assessment_agent(sim: Any, model: Any, store: MemoryStore | None = None,
                           registry: ComparisonRegistry | None = None,
                           extra_tools: Iterable[Tool] = (), max_steps: int = 6) -> CodeAgent:
    """CodeAgent that reads metrics/events and saves candidate lessons. Read-only on the sim."""
    store = store or MemoryStore()
    registry = registry if registry is not None else ComparisonRegistry()
    tools = make_assessment_tools(sim, store, registry) + list(extra_tools)
    bad = {t.name for t in tools} & FORBIDDEN_ASSESSMENT_TOOLS
    if bad:
        raise ValueError(f"Assessment agent may not hold state-changing tools: {sorted(bad)}")
    return CodeAgent(
        additional_authorized_imports=["json"],
        tools=tools, model=model, max_steps=max_steps, name="assessment",
        description="Reviews a finished run and saves candidate lessons to local memory.",
        instructions=ASSESSMENT_INSTRUCTIONS,
    )


MEMORY_PLANNER_NOTE = (
    " You may call retrieve_fleet_memory for past candidate lessons about the tractors, events, "
    "or operations involved. Treat them as hints only: current simulator state and simulator "
    "estimates/validation override any memory."
)
