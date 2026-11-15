"""Small, deterministic fleet simulator for the Run the Fleet hack-day.

The simulation models fleet-level jobs, not low-level steering. One tick is a
configurable number of minutes. Tractors move one grid cell per tick, consume
fuel, work a field job at an implement-specific rate, and can break down or
encounter blocked cells. The public methods are designed to be wrapped as
Smolagents tools.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
from math import ceil
from pathlib import Path
from typing import Any, Iterable
import copy
import json

Position = tuple[int, int]


@dataclass
class Tractor:
    id: str
    position: Position
    implements: list[str]
    fuel_liters: float
    tank_liters: float
    fuel_reserve_liters: float
    acres_per_hour: dict[str, float]
    travel_liters_per_cell: float
    work_liters_per_hour: float
    refuel_liters_per_tick: float
    status: str = "idle"
    job_id: str | None = None
    route: list[Position] = field(default_factory=list)
    target_type: str | None = None
    refuel_target_liters: float | None = None
    repair_at_tick: int | None = None
    waiting_ticks: int = 0

    @property
    def fuel_pct(self) -> float:
        return 100.0 * self.fuel_liters / self.tank_liters if self.tank_liters else 0.0


@dataclass
class FieldJob:
    id: str
    field_id: str
    job_type: str
    required_implement: str
    acres: float
    priority: int
    deadline_tick: int
    created_order: int
    status: str = "pending"
    completed_acres: float = 0.0
    assigned_tractor_id: str | None = None
    deadline_warning_logged: bool = False
    completed_tick: int | None = None

    @property
    def remaining_acres(self) -> float:
        return max(0.0, self.acres - self.completed_acres)

    @property
    def progress_pct(self) -> float:
        return min(100.0, 100.0 * self.completed_acres / self.acres) if self.acres else 100.0


class FarmSimulator:
    """Stateful grid-world simulator with atomic dispatch validation."""

    def __init__(self, scenario: dict[str, Any]):
        self.scenario = copy.deepcopy(scenario)
        grid = self.scenario.get("grid", {})
        self.width = int(grid.get("width", 12))
        self.height = int(grid.get("height", 8))
        self.blocked_cells: set[Position] = {tuple(p) for p in grid.get("blocked_cells", [])}
        self.base: Position = tuple(self.scenario.get("base", [0, 0]))  # type: ignore[assignment]
        self.tick_minutes = int(self.scenario.get("tick_minutes", 15))
        self.tick_hours = self.tick_minutes / 60.0
        self.current_tick = int(self.scenario.get("start_tick", 0))
        self.version = 0
        self.fields: dict[str, dict[str, Any]] = {}
        for item in self.scenario.get("fields", []):
            row = copy.deepcopy(item)
            row["position"] = tuple(row["position"])
            self.fields[row["id"]] = row
        self.tractors: dict[str, Tractor] = {}
        for item in self.scenario.get("tractors", []):
            row = dict(item)
            row["position"] = tuple(row["position"])
            self.tractors[row["id"]] = Tractor(**row)
        self.jobs: dict[str, FieldJob] = {
            item["id"]: FieldJob(**item) for item in self.scenario.get("jobs", [])
        }
        self.event_log: list[dict[str, Any]] = []
        self.counters = {
            "travel_cells": 0,
            "fuel_used_liters": 0.0,
            "idle_tractor_ticks": 0,
            "work_tractor_ticks": 0,
            "breakdowns": 0,
            "dispatches": 0,
            "replans": 0,
        }
        self._validate_scenario()
        self._log("simulation_started", "Farm simulation initialized", {
            "tractors": len(self.tractors), "fields": len(self.fields), "jobs": len(self.jobs)
        })

    @classmethod
    def from_file(cls, path: str | Path) -> "FarmSimulator":
        with open(path, encoding="utf-8") as handle:
            return cls(json.load(handle))

    def _validate_scenario(self) -> None:
        if self.width < 2 or self.height < 2:
            raise ValueError("Grid must be at least 2 by 2")
        if self.tick_minutes <= 0:
            raise ValueError("tick_minutes must be positive")
        if not self._inside(self.base) or self.base in self.blocked_cells:
            raise ValueError("Base must be inside the grid and not blocked")
        if not self.tractors or not self.fields or not self.jobs:
            raise ValueError("Scenario must include tractors, fields, and jobs")
        for field_data in self.fields.values():
            pos = field_data["position"]
            if not self._inside(pos) or pos in self.blocked_cells:
                raise ValueError(f"Field {field_data['id']} has an invalid position")
        for tractor in self.tractors.values():
            if not self._inside(tractor.position) or tractor.position in self.blocked_cells:
                raise ValueError(f"Tractor {tractor.id} has an invalid position")
            if tractor.tank_liters <= 0 or not 0 <= tractor.fuel_liters <= tractor.tank_liters:
                raise ValueError(f"Tractor {tractor.id} has invalid fuel values")
            if tractor.fuel_reserve_liters < 0 or tractor.travel_liters_per_cell < 0 or tractor.work_liters_per_hour < 0:
                raise ValueError(f"Tractor {tractor.id} has a negative fuel parameter")
            if tractor.refuel_liters_per_tick <= 0 or any(float(rate) <= 0 for rate in tractor.acres_per_hour.values()):
                raise ValueError(f"Tractor {tractor.id} needs positive work rates and refuel rate")
        for job in self.jobs.values():
            if job.field_id not in self.fields:
                raise ValueError(f"Job {job.id} references unknown field {job.field_id}")
            if job.acres <= 0 or job.deadline_tick < 0:
                raise ValueError(f"Job {job.id} has invalid acres or deadline")
            if not 1 <= job.priority <= 5:
                raise ValueError(f"Job {job.id} priority must be between 1 and 5")

    def _inside(self, p: Position) -> bool:
        return 0 <= p[0] < self.width and 0 <= p[1] < self.height

    def _log(self, event_type: str, message: str, details: dict[str, Any] | None = None) -> None:
        self.event_log.append({
            "tick": self.current_tick,
            "time_minutes": self.current_tick * self.tick_minutes,
            "type": event_type,
            "message": message,
            "details": details or {},
        })
        self.event_log = self.event_log[-500:]

    @staticmethod
    def _pos_json(p: Position) -> list[int]:
        return [p[0], p[1]]

    def find_path(self, start: Position, goal: Position) -> list[Position] | None:
        """Return shortest 4-neighbor path, including start and goal; None if blocked."""
        if not self._inside(start) or not self._inside(goal):
            return None
        if start in self.blocked_cells or goal in self.blocked_cells:
            return None
        if start == goal:
            return [start]
        queue: deque[Position] = deque([start])
        parent: dict[Position, Position | None] = {start: None}
        # Fixed order makes routing and demo results reproducible.
        neighbors = ((1, 0), (0, 1), (-1, 0), (0, -1))
        while queue:
            x, y = queue.popleft()
            for dx, dy in neighbors:
                nxt = (x + dx, y + dy)
                if not self._inside(nxt) or nxt in self.blocked_cells or nxt in parent:
                    continue
                parent[nxt] = (x, y)
                if nxt == goal:
                    path = [goal]
                    node = goal
                    while parent[node] is not None:
                        node = parent[node]  # type: ignore[assignment]
                        path.append(node)
                    path.reverse()
                    return path
                queue.append(nxt)
        return None

    def _job(self, job_id: str) -> FieldJob:
        if job_id not in self.jobs:
            raise KeyError(f"Unknown job: {job_id}")
        return self.jobs[job_id]

    def _tractor(self, tractor_id: str) -> Tractor:
        if tractor_id not in self.tractors:
            raise KeyError(f"Unknown tractor: {tractor_id}")
        return self.tractors[tractor_id]

    def estimate_job(self, tractor_id: str, job_id: str) -> dict[str, Any]:
        tractor, job = self._tractor(tractor_id), self._job(job_id)
        field_data = self.fields[job.field_id]
        path = self.find_path(tractor.position, field_data["position"])
        if path is None:
            return {"feasible": False, "reason": "No open route to the field"}
        if job.required_implement not in tractor.implements:
            return {"feasible": False, "reason": f"Requires {job.required_implement}"}
        rate = float(tractor.acres_per_hour.get(job.required_implement, 0.0))
        if rate <= 0:
            return {"feasible": False, "reason": "Tractor has no work rate for this implement"}
        work_hours = job.remaining_acres / rate
        to_base = self.find_path(field_data["position"], self.base)
        if to_base is None:
            return {"feasible": False, "reason": "No return route from field to base"}
        travel_cells = max(0, len(path) - 1) + max(0, len(to_base) - 1)
        fuel_to_finish = (
            travel_cells * tractor.travel_liters_per_cell
            + work_hours * tractor.work_liters_per_hour
            + tractor.fuel_reserve_liters
        )
        travel_ticks = max(0, len(path) - 1)
        work_ticks = ceil(work_hours / self.tick_hours) if work_hours else 0
        eta_tick = self.current_tick + travel_ticks + work_ticks
        problems = []
        if tractor.status != "idle" or tractor.job_id:
            problems.append(f"Tractor is {tractor.status}")
        if job.status not in {"pending", "interrupted"} or job.assigned_tractor_id:
            problems.append(f"Job is {job.status}")
        if tractor.fuel_liters + 1e-9 < fuel_to_finish:
            problems.append(f"Needs {fuel_to_finish:.1f} L including reserve; has {tractor.fuel_liters:.1f} L")
        if eta_tick > job.deadline_tick:
            problems.append(f"Estimated completion tick {eta_tick} misses deadline tick {job.deadline_tick}")
        return {
            "feasible": not problems,
            "reasons": problems,
            "route": [self._pos_json(p) for p in path],
            "travel_cells": max(0, len(path) - 1),
            "travel_ticks": travel_ticks,
            "work_hours": round(work_hours, 2),
            "work_ticks": work_ticks,
            "estimated_completion_tick": eta_tick,
            "fuel_needed_liters": round(fuel_to_finish, 2),
            "fuel_available_liters": round(tractor.fuel_liters, 2),
            "remaining_acres": round(job.remaining_acres, 2),
        }

    def validate_plan(self, assignments: list[dict[str, str]], expected_version: int | None = None) -> dict[str, Any]:
        """Validate an entire proposed plan without changing state."""
        errors: list[str] = []
        if expected_version is not None and expected_version != self.version:
            errors.append(f"Stale state version: expected {expected_version}, current version is {self.version}")
        if not isinstance(assignments, list) or not assignments:
            errors.append("Plan must contain at least one assignment")
            return {"valid": False, "errors": errors, "state_version": self.version}
        seen_jobs: set[str] = set()
        seen_tractors: set[str] = set()
        # Do not send a second tractor to a field that already has active work.
        seen_fields: set[str] = {
            j.field_id for j in self.jobs.values()
            if j.status in {"assigned", "in_progress"} and j.assigned_tractor_id
        }
        estimates: list[dict[str, Any]] = []
        for i, item in enumerate(assignments):
            if not isinstance(item, dict) or not item.get("tractor_id") or not item.get("job_id"):
                errors.append(f"Assignment {i} must contain tractor_id and job_id")
                continue
            tractor_id, job_id = str(item["tractor_id"]), str(item["job_id"])
            if tractor_id not in self.tractors:
                errors.append(f"Assignment {i}: unknown tractor {tractor_id}")
                continue
            if job_id not in self.jobs:
                errors.append(f"Assignment {i}: unknown job {job_id}")
                continue
            job = self.jobs[job_id]
            if job_id in seen_jobs:
                errors.append(f"Job {job_id} appears more than once")
            if tractor_id in seen_tractors:
                errors.append(f"Tractor {tractor_id} has more than one new job")
            if job.field_id in seen_fields:
                errors.append(f"Field {job.field_id} already has active work or another assignment in this plan")
            seen_jobs.add(job_id)
            seen_tractors.add(tractor_id)
            seen_fields.add(job.field_id)
            estimate = self.estimate_job(tractor_id, job_id)
            estimates.append({"tractor_id": tractor_id, "job_id": job_id, **estimate})
            for reason in estimate.get("reasons", []):
                errors.append(f"{tractor_id} → {job_id}: {reason}")
        return {
            "valid": not errors,
            "errors": errors,
            "estimates": estimates,
            "state_version": self.version,
        }

    def commit_plan(self, assignments: list[dict[str, str]], expected_version: int | None = None) -> dict[str, Any]:
        """Atomically apply a plan only if every assignment is currently feasible."""
        validation = self.validate_plan(assignments, expected_version)
        if not validation["valid"]:
            self._log("plan_rejected", "Dispatch plan rejected", {"errors": validation["errors"]})
            return {"accepted": False, **validation}
        for item in assignments:
            tractor = self.tractors[item["tractor_id"]]
            job = self.jobs[item["job_id"]]
            target = self.fields[job.field_id]["position"]
            path = self.find_path(tractor.position, target)
            assert path is not None  # validation already checked reachability
            tractor.job_id = job.id
            tractor.route = path
            tractor.target_type = "job"
            tractor.status = "working" if len(path) == 1 else "traveling"
            job.status = "in_progress" if len(path) == 1 else "assigned"
            job.assigned_tractor_id = tractor.id
        self.counters["dispatches"] += 1
        self.version += 1
        self._log("plan_accepted", "Fleet plan accepted", {"assignments": assignments})
        return {"accepted": True, "assignments": assignments, "state_version": self.version,
                "estimates": validation["estimates"]}

    def pending_jobs(self) -> list[dict[str, Any]]:
        rows = []
        for job in sorted(self.jobs.values(), key=lambda j: j.created_order):
            if job.status not in {"pending", "interrupted"}:
                continue
            rows.append({
                "id": job.id, "field_id": job.field_id, "job_type": job.job_type,
                "required_implement": job.required_implement, "acres": job.acres,
                "completed_acres": round(job.completed_acres, 2),
                "remaining_acres": round(job.remaining_acres, 2),
                "progress_pct": round(job.progress_pct, 1), "priority": job.priority,
                "deadline_tick": job.deadline_tick, "status": job.status,
                "previous_tractor_id": job.assigned_tractor_id,
            })
        return rows

    def fleet_state(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "tick": self.current_tick,
            "time_minutes": self.current_tick * self.tick_minutes,
            "tick_minutes": self.tick_minutes,
            "grid": {"width": self.width, "height": self.height,
                     "blocked_cells": [self._pos_json(p) for p in sorted(self.blocked_cells)],
                     "base": self._pos_json(self.base)},
            "tractors": [self._tractor_state(t) for t in sorted(self.tractors.values(), key=lambda x: x.id)],
            "fields": [{**v, "position": self._pos_json(v["position"])}
                       for v in sorted(self.fields.values(), key=lambda x: x["id"])],
            "jobs": [self._job_state(j) for j in sorted(self.jobs.values(), key=lambda x: x.created_order)],
            "pending_jobs": self.pending_jobs(),
            "metrics": self.metrics(),
            "recent_events": self.event_log[-40:],
        }

    def _tractor_state(self, t: Tractor) -> dict[str, Any]:
        job = self.jobs.get(t.job_id) if t.job_id else None
        return {
            "id": t.id, "status": t.status, "position": self._pos_json(t.position),
            "implements": t.implements, "fuel_liters": round(t.fuel_liters, 2),
            "fuel_pct": round(t.fuel_pct, 1), "tank_liters": t.tank_liters,
            "fuel_reserve_liters": t.fuel_reserve_liters, "job_id": t.job_id,
            "job_progress_pct": round(job.progress_pct, 1) if job else 0,
            "route": [self._pos_json(p) for p in t.route],
            "waiting_ticks": t.waiting_ticks,
            "repair_at_tick": t.repair_at_tick,
        }

    @staticmethod
    def _job_state(j: FieldJob) -> dict[str, Any]:
        return {
            "id": j.id, "field_id": j.field_id, "job_type": j.job_type,
            "required_implement": j.required_implement, "acres": j.acres,
            "completed_acres": round(j.completed_acres, 2),
            "remaining_acres": round(j.remaining_acres, 2),
            "progress_pct": round(j.progress_pct, 1), "priority": j.priority,
            "deadline_tick": j.deadline_tick, "status": j.status,
            "assigned_tractor_id": j.assigned_tractor_id,
            "completed_tick": j.completed_tick,
        }

    def _interrupt_job(self, tractor: Tractor, why: str) -> None:
        if tractor.job_id and tractor.job_id in self.jobs:
            job = self.jobs[tractor.job_id]
            if job.status != "completed":
                job.status = "interrupted"
                job.assigned_tractor_id = None
                self._log("job_interrupted", f"{job.id} interrupted; {job.remaining_acres:.1f} acres remain", {
                    "job_id": job.id, "tractor_id": tractor.id,
                    "completed_acres": round(job.completed_acres, 2), "reason": why,
                })
        tractor.job_id = None
        tractor.target_type = None
        tractor.route = []

    def breakdown(self, tractor_id: str, repair_after_ticks: int | None = None) -> dict[str, Any]:
        tractor = self._tractor(tractor_id)
        if tractor.status == "broken":
            return {"ok": False, "reason": "Tractor is already broken"}
        if repair_after_ticks is not None and int(repair_after_ticks) < 0:
            return {"ok": False, "reason": "repair_after_ticks cannot be negative"}
        self._interrupt_job(tractor, "breakdown")
        tractor.status = "broken"
        tractor.repair_at_tick = self.current_tick + repair_after_ticks if repair_after_ticks is not None else None
        self.counters["breakdowns"] += 1
        self.counters["replans"] += 1
        self.version += 1
        self._log("breakdown", f"{tractor.id} broke down", {"tractor_id": tractor.id,
            "repair_at_tick": tractor.repair_at_tick})
        return {"ok": True, "state_version": self.version, "tractor": self._tractor_state(tractor)}

    def repair(self, tractor_id: str, add_fuel_liters: float = 0.0) -> dict[str, Any]:
        tractor = self._tractor(tractor_id)
        if tractor.status != "broken" and tractor.status != "stranded":
            return {"ok": False, "reason": f"Tractor is {tractor.status}, not broken or stranded"}
        tractor.status = "idle"
        tractor.repair_at_tick = None
        tractor.fuel_liters = min(tractor.tank_liters, tractor.fuel_liters + max(0.0, add_fuel_liters))
        self.version += 1
        self._log("repair", f"{tractor.id} repaired", {"tractor_id": tractor.id,
            "fuel_added_liters": round(add_fuel_liters, 2)})
        return {"ok": True, "state_version": self.version, "tractor": self._tractor_state(tractor)}

    def block_cell(self, position: Position) -> dict[str, Any]:
        p = tuple(position)
        if not self._inside(p):
            return {"ok": False, "reason": "Cell is outside the grid"}
        if p == self.base or any(f["position"] == p for f in self.fields.values()):
            return {"ok": False, "reason": "Cannot block the base or a field entrance"}
        if any(t.position == p for t in self.tractors.values()):
            return {"ok": False, "reason": "Cannot block a cell occupied by a tractor"}
        self.blocked_cells.add(p)
        self.counters["replans"] += 1
        self.version += 1
        self._log("route_blocked", f"Route cell {p} blocked", {"position": self._pos_json(p)})
        for tractor in self.tractors.values():
            if tractor.status != "traveling" or not tractor.job_id:
                continue
            job = self.jobs[tractor.job_id]
            path = self.find_path(tractor.position, self.fields[job.field_id]["position"])
            if path is None:
                tractor.status = "blocked"
                tractor.route = []
                self._log("tractor_blocked", f"{tractor.id} has no route to {job.field_id}", {
                    "tractor_id": tractor.id, "job_id": job.id,
                })
            else:
                tractor.route = path
                self._log("route_replanned", f"{tractor.id} received an alternate route", {
                    "tractor_id": tractor.id, "job_id": job.id,
                    "remaining_route": [self._pos_json(v) for v in path],
                })
        return {"ok": True, "state_version": self.version}

    def unblock_cell(self, position: Position) -> dict[str, Any]:
        p = tuple(position)
        if p not in self.blocked_cells:
            return {"ok": False, "reason": "Cell was not blocked"}
        self.blocked_cells.remove(p)
        self.version += 1
        self._log("route_opened", f"Route cell {p} reopened", {"position": self._pos_json(p)})
        for tractor in self.tractors.values():
            if tractor.status == "blocked" and tractor.job_id:
                job = self.jobs[tractor.job_id]
                path = self.find_path(tractor.position, self.fields[job.field_id]["position"])
                if path:
                    tractor.route = path
                    tractor.status = "traveling"
                    self._log("route_replanned", f"{tractor.id} resumed toward {job.field_id}", {
                        "tractor_id": tractor.id, "job_id": job.id,
                    })
        return {"ok": True, "state_version": self.version}

    def change_priority(self, job_id: str, priority: int) -> dict[str, Any]:
        job = self._job(job_id)
        if not 1 <= int(priority) <= 5:
            return {"ok": False, "reason": "Priority must be 1 (highest) through 5 (lowest)"}
        old = job.priority
        job.priority = int(priority)
        self.version += 1
        self.counters["replans"] += 1
        self._log("priority_changed", f"{job_id} priority changed {old} → {job.priority}", {
            "job_id": job_id, "old_priority": old, "new_priority": job.priority,
        })
        return {"ok": True, "state_version": self.version}

    def request_refuel(self, tractor_id: str, target_pct: float = 85.0) -> dict[str, Any]:
        tractor = self._tractor(tractor_id)
        if tractor.status not in {"idle", "working", "traveling"}:
            return {"ok": False, "reason": f"Cannot send tractor to refuel while {tractor.status}"}
        if tractor.job_id:
            self._interrupt_job(tractor, "tractor sent to refuel")
        path = self.find_path(tractor.position, self.base)
        if path is None:
            return {"ok": False, "reason": "No route to base"}
        fuel_to_base = max(0, len(path) - 1) * tractor.travel_liters_per_cell
        if tractor.fuel_liters < fuel_to_base:
            return {"ok": False, "reason": "Not enough fuel to reach base; needs service recovery"}
        tractor.route = path
        tractor.target_type = "refuel"
        tractor.refuel_target_liters = tractor.tank_liters * min(100.0, max(1.0, target_pct)) / 100.0
        tractor.status = "refueling" if len(path) == 1 else "to_refuel"
        self.version += 1
        self._log("refuel_requested", f"{tractor.id} heading to base to refuel", {
            "tractor_id": tractor.id, "target_pct": target_pct,
        })
        return {"ok": True, "state_version": self.version}

    def _consume_fuel(self, tractor: Tractor, liters: float) -> bool:
        amount = max(0.0, liters)
        if tractor.fuel_liters + 1e-9 < amount:
            return False
        tractor.fuel_liters = max(0.0, tractor.fuel_liters - amount)
        self.counters["fuel_used_liters"] += amount
        return True

    def _replan_active_route(self, tractor: Tractor) -> bool:
        if tractor.target_type == "job" and tractor.job_id:
            goal = self.fields[self.jobs[tractor.job_id].field_id]["position"]
        elif tractor.target_type == "refuel":
            goal = self.base
        else:
            return False
        path = self.find_path(tractor.position, goal)
        if path is None:
            tractor.route = []
            tractor.status = "blocked"
            return False
        tractor.route = path
        tractor.status = "working" if tractor.target_type == "job" and len(path) == 1 else (
            "refueling" if tractor.target_type == "refuel" and len(path) == 1 else
            "traveling" if tractor.target_type == "job" else "to_refuel"
        )
        return True

    def advance(self, steps: int = 1) -> dict[str, Any]:
        """Advance the discrete simulation. Returns the latest snapshot."""
        for _ in range(max(0, int(steps))):
            self._advance_one_tick()
        return self.fleet_state()

    def _advance_one_tick(self) -> None:
        # Scheduled repairs happen at the beginning of the tick.
        for tractor in self.tractors.values():
            if tractor.status == "broken" and tractor.repair_at_tick is not None and self.current_tick >= tractor.repair_at_tick:
                tractor.status = "idle"
                tractor.repair_at_tick = None
                self._log("repair", f"{tractor.id} automatically repaired", {"tractor_id": tractor.id})

        # A broken or stranded unit still physically occupies its cell.
        occupied: dict[Position, str] = {t.position: t.id for t in self.tractors.values()}
        for tractor in sorted(self.tractors.values(), key=lambda t: t.id):
            if tractor.status in {"traveling", "to_refuel", "blocked"}:
                self._step_travel(tractor, occupied)
            elif tractor.status == "working":
                self._step_work(tractor)
            elif tractor.status == "refueling":
                self._step_refuel(tractor)
            elif tractor.status == "idle":
                self.counters["idle_tractor_ticks"] += 1

        self.current_tick += 1
        for job in self.jobs.values():
            if job.status in {"pending", "interrupted", "assigned", "in_progress"} and not job.deadline_warning_logged and self.current_tick > job.deadline_tick:
                job.deadline_warning_logged = True
                self._log("deadline_missed", f"{job.id} passed its deadline with work remaining", {
                    "job_id": job.id, "remaining_acres": round(job.remaining_acres, 2),
                })
        self.version += 1

    def _step_travel(self, tractor: Tractor, occupied: dict[Position, str]) -> None:
        if tractor.status == "blocked":
            if not self._replan_active_route(tractor):
                return
            if tractor.status == "blocked":
                return
        if len(tractor.route) <= 1:
            self._arrive(tractor)
            return
        # Route may have become invalid due to a newly blocked cell.
        if any(p in self.blocked_cells for p in tractor.route[1:]):
            if not self._replan_active_route(tractor):
                self._log("tractor_blocked", f"{tractor.id} cannot find an open route", {"tractor_id": tractor.id})
                return
            if len(tractor.route) <= 1:
                self._arrive(tractor)
                return
        nxt = tractor.route[1]
        owner = occupied.get(nxt)
        if owner is not None and owner != tractor.id:
            tractor.waiting_ticks += 1
            if tractor.waiting_ticks == 1:
                self._log("traffic_wait", f"{tractor.id} waiting for {owner} to clear a cell", {
                    "tractor_id": tractor.id, "blocking_tractor_id": owner,
                })
            return
        if not self._consume_fuel(tractor, tractor.travel_liters_per_cell):
            self._interrupt_job(tractor, "fuel exhausted while traveling")
            tractor.status = "stranded"
            self._log("fuel_empty", f"{tractor.id} ran out of fuel", {"tractor_id": tractor.id})
            return
        occupied.pop(tractor.position, None)
        tractor.position = nxt
        occupied[nxt] = tractor.id
        self.counters["travel_cells"] += 1
        tractor.waiting_ticks = 0
        tractor.route.pop(0)
        if len(tractor.route) <= 1:
            self._arrive(tractor)

    def _arrive(self, tractor: Tractor) -> None:
        tractor.route = []
        if tractor.target_type == "refuel":
            tractor.status = "refueling"
            self._log("arrived_base", f"{tractor.id} arrived at base", {"tractor_id": tractor.id})
        elif tractor.target_type == "job" and tractor.job_id:
            job = self.jobs[tractor.job_id]
            tractor.status = "working"
            job.status = "in_progress"
            self._log("arrived_field", f"{tractor.id} arrived at {job.field_id} for {job.id}", {
                "tractor_id": tractor.id, "job_id": job.id,
            })

    def _step_work(self, tractor: Tractor) -> None:
        if not tractor.job_id or tractor.job_id not in self.jobs:
            tractor.status = "idle"
            tractor.target_type = None
            return
        job = self.jobs[tractor.job_id]
        rate = float(tractor.acres_per_hour.get(job.required_implement, 0.0))
        if rate <= 0:
            self._interrupt_job(tractor, "implement/work-rate unavailable")
            tractor.status = "idle"
            self._log("job_interrupted", f"{job.id} stopped: implement unavailable", {"job_id": job.id})
            return
        possible_acres = min(job.remaining_acres, rate * self.tick_hours)
        fuel_needed = (possible_acres / rate) * tractor.work_liters_per_hour
        if not self._consume_fuel(tractor, fuel_needed):
            affordable_hours = tractor.fuel_liters / tractor.work_liters_per_hour if tractor.work_liters_per_hour else 0
            affordable_acres = min(possible_acres, affordable_hours * rate)
            actual_fuel = affordable_hours * tractor.work_liters_per_hour
            if affordable_acres > 0:
                self._consume_fuel(tractor, actual_fuel)
                job.completed_acres = min(job.acres, job.completed_acres + affordable_acres)
            self._interrupt_job(tractor, "fuel exhausted during field work")
            tractor.status = "stranded"
            self._log("fuel_empty", f"{tractor.id} ran out of fuel during {job.id}", {
                "tractor_id": tractor.id, "job_id": job.id,
                "completed_acres": round(job.completed_acres, 2),
            })
            return
        job.completed_acres = min(job.acres, job.completed_acres + possible_acres)
        self.counters["work_tractor_ticks"] += 1
        if job.remaining_acres <= 1e-6:
            job.completed_acres = job.acres
            job.status = "completed"
            job.assigned_tractor_id = tractor.id
            job.completed_tick = self.current_tick + 1
            self._log("job_completed", f"{tractor.id} completed {job.id} at {job.field_id}", {
                "tractor_id": tractor.id, "job_id": job.id, "acres": job.acres,
                "on_time": job.completed_tick <= job.deadline_tick,
            })
            tractor.job_id = None
            tractor.target_type = None
            tractor.status = "idle"

    def _step_refuel(self, tractor: Tractor) -> None:
        target = tractor.refuel_target_liters or tractor.tank_liters * 0.85
        if tractor.position != self.base:
            tractor.status = "to_refuel"
            self._replan_active_route(tractor)
            return
        before = tractor.fuel_liters
        tractor.fuel_liters = min(target, tractor.tank_liters, tractor.fuel_liters + tractor.refuel_liters_per_tick)
        added = tractor.fuel_liters - before
        if tractor.fuel_liters >= target - 1e-6:
            tractor.status = "idle"
            tractor.target_type = None
            tractor.refuel_target_liters = None
            tractor.route = []
            self.version += 1
            self._log("refuel_complete", f"{tractor.id} refueled", {
                "tractor_id": tractor.id, "fuel_liters": round(tractor.fuel_liters, 1),
            })
        elif added > 0:
            self._log("refueling", f"{tractor.id} refueling at base", {
                "tractor_id": tractor.id, "fuel_liters": round(tractor.fuel_liters, 1),
            })

    def metrics(self) -> dict[str, Any]:
        completed = [j for j in self.jobs.values() if j.status == "completed"]
        return {
            **self.counters,
            "fuel_used_liters": round(self.counters["fuel_used_liters"], 2),
            "completed_jobs": len(completed),
            "on_time_jobs": sum(j.completed_tick is not None and j.completed_tick <= j.deadline_tick for j in completed),
            "late_completed_jobs": sum(j.completed_tick is not None and j.completed_tick > j.deadline_tick for j in completed),
            "unfinished_jobs": sum(j.status != "completed" for j in self.jobs.values()),
            "acres_completed": round(sum(j.completed_acres for j in self.jobs.values()), 2),
            "total_job_acres": round(sum(j.acres for j in self.jobs.values()), 2),
            "completion_rate_pct": round(100 * len(completed) / len(self.jobs), 1) if self.jobs else 0,
        }

    def event(self, name: str, **payload: Any) -> dict[str, Any]:
        """Apply a named challenge event, useful for a dashboard or agent tool."""
        if name == "breakdown":
            return self.breakdown(str(payload["tractor_id"]), payload.get("repair_after_ticks"))
        if name == "repair":
            return self.repair(str(payload["tractor_id"]), float(payload.get("add_fuel_liters", 0)))
        if name == "block_cell":
            return self.block_cell(tuple(payload["position"]))
        if name == "unblock_cell":
            return self.unblock_cell(tuple(payload["position"]))
        if name == "change_priority":
            return self.change_priority(str(payload["job_id"]), int(payload["priority"]))
        if name == "refuel":
            return self.request_refuel(str(payload["tractor_id"]), float(payload.get("target_pct", 85)))
        raise ValueError(f"Unknown event: {name}")


def first_come_first_served_plan(sim: FarmSimulator) -> list[dict[str, str]]:
    """Reference baseline: oldest jobs first, then first feasible idle tractor."""
    available = [t.id for t in sorted(sim.tractors.values(), key=lambda t: t.id) if t.status == "idle" and not t.job_id]
    candidates = sorted(
        (j for j in sim.jobs.values() if j.status in {"pending", "interrupted"} and not j.assigned_tractor_id),
        key=lambda j: j.created_order,
    )
    result: list[dict[str, str]] = []
    reserved_fields: set[str] = set()
    for job in candidates:
        if job.field_id in reserved_fields:
            continue
        for tractor_id in list(available):
            if sim.estimate_job(tractor_id, job.id)["feasible"]:
                result.append({"tractor_id": tractor_id, "job_id": job.id})
                available.remove(tractor_id)
                reserved_fields.add(job.field_id)
                break
    return result


def priority_nearest_plan(sim: FarmSimulator) -> list[dict[str, str]]:
    """Deterministic demo policy, useful for exercising the UI without an LLM."""
    available = [t.id for t in sorted(sim.tractors.values(), key=lambda t: t.id) if t.status == "idle" and not t.job_id]
    candidates = sorted(
        (j for j in sim.jobs.values() if j.status in {"pending", "interrupted"} and not j.assigned_tractor_id),
        key=lambda j: (j.priority, j.deadline_tick, j.created_order),
    )
    assignments: list[dict[str, str]] = []
    fields_taken: set[str] = set()
    for job in candidates:
        if job.field_id in fields_taken:
            continue
        options = []
        for tractor_id in available:
            estimate = sim.estimate_job(tractor_id, job.id)
            if estimate["feasible"]:
                options.append((estimate["travel_ticks"], estimate["fuel_needed_liters"], tractor_id))
        if options:
            _, _, tractor_id = min(options)
            assignments.append({"tractor_id": tractor_id, "job_id": job.id})
            available.remove(tractor_id)
            fields_taken.add(job.field_id)
    return assignments


def default_scenario() -> dict[str, Any]:
    """A 12×8, three-tractor, six-field scenario with mixed equipment and deadlines."""
    return {
        "name": "Cochise Valley Mixed Field Day",
        "tick_minutes": 15,
        "base": [0, 0],
        "grid": {"width": 12, "height": 8, "blocked_cells": [[5, 1], [5, 2], [5, 3], [8, 5]]},
        "fields": [
            {"id": "F1", "name": "North Lettuce", "position": [3, 1], "acres": 12},
            {"id": "F2", "name": "East Alfalfa", "position": [9, 1], "acres": 18},
            {"id": "F3", "name": "South Corn", "position": [2, 5], "acres": 16},
            {"id": "F4", "name": "West Cotton", "position": [6, 6], "acres": 14},
            {"id": "F5", "name": "River Barley", "position": [10, 6], "acres": 20},
            {"id": "F6", "name": "Trial Plot", "position": [8, 3], "acres": 8},
        ],
        "tractors": [
            {"id": "T1", "position": [0, 0], "implements": ["sprayer", "mower"],
             "fuel_liters": 72, "tank_liters": 100, "fuel_reserve_liters": 12,
             "acres_per_hour": {"sprayer": 8.0, "mower": 5.0}, "travel_liters_per_cell": 0.55,
             "work_liters_per_hour": 7.5, "refuel_liters_per_tick": 30.0},
            {"id": "T2", "position": [1, 0], "implements": ["mower", "seeder"],
             "fuel_liters": 48, "tank_liters": 80, "fuel_reserve_liters": 10,
             "acres_per_hour": {"mower": 6.5, "seeder": 4.0}, "travel_liters_per_cell": 0.42,
             "work_liters_per_hour": 5.8, "refuel_liters_per_tick": 24.0},
            {"id": "T3", "position": [0, 1], "implements": ["sprayer", "seeder"],
             "fuel_liters": 60, "tank_liters": 90, "fuel_reserve_liters": 11,
             "acres_per_hour": {"sprayer": 7.0, "seeder": 5.2}, "travel_liters_per_cell": 0.48,
             "work_liters_per_hour": 6.4, "refuel_liters_per_tick": 27.0},
        ],
        "jobs": [
            {"id": "J1", "field_id": "F1", "job_type": "spot spray", "required_implement": "sprayer",
             "acres": 8, "priority": 1, "deadline_tick": 20, "created_order": 1},
            {"id": "J2", "field_id": "F2", "job_type": "mow", "required_implement": "mower",
             "acres": 14, "priority": 2, "deadline_tick": 34, "created_order": 2},
            {"id": "J3", "field_id": "F3", "job_type": "seed", "required_implement": "seeder",
             "acres": 10, "priority": 1, "deadline_tick": 30, "created_order": 3},
            {"id": "J4", "field_id": "F4", "job_type": "mow", "required_implement": "mower",
             "acres": 11, "priority": 3, "deadline_tick": 42, "created_order": 4},
            {"id": "J5", "field_id": "F5", "job_type": "spot spray", "required_implement": "sprayer",
             "acres": 16, "priority": 2, "deadline_tick": 46, "created_order": 5},
            {"id": "J6", "field_id": "F6", "job_type": "seed", "required_implement": "seeder",
             "acres": 6, "priority": 1, "deadline_tick": 24, "created_order": 6},
            {"id": "J7", "field_id": "F1", "job_type": "mow", "required_implement": "mower",
             "acres": 5, "priority": 4, "deadline_tick": 54, "created_order": 7},
            {"id": "J8", "field_id": "F3", "job_type": "spot spray", "required_implement": "sprayer",
             "acres": 7, "priority": 3, "deadline_tick": 50, "created_order": 8},
        ],
    }


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Run the autonomous tractor fleet simulator")
    parser.add_argument("--scenario", default=None, help="Path to a scenario JSON file")
    parser.add_argument("--ticks", type=int, default=16, help="Number of simulation ticks to advance")
    parser.add_argument("--policy", choices=("fcfs", "priority-nearest"), default="priority-nearest")
    args = parser.parse_args()
    sim = FarmSimulator.from_file(args.scenario) if args.scenario else FarmSimulator(default_scenario())
    policy = first_come_first_served_plan if args.policy == "fcfs" else priority_nearest_plan
    for _ in range(args.ticks):
        proposal = policy(sim)
        if proposal:
            sim.commit_plan(proposal, sim.version)
        sim.advance()
    print(json.dumps({"metrics": sim.metrics(), "state": sim.fleet_state(), "event_log": sim.event_log}, indent=2))


if __name__ == "__main__":
    main()
