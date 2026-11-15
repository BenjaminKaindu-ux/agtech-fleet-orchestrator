"""Agronomic field conditions layered on top of farm_sim (which models no weather).

The environment decides, at any tick, whether an operation may run:

* **Spraying** only in 3–10 mph wind (near-calm air risks temperature inversions,
  stronger wind causes drift), in daylight, and not while it rains.
* **Soil traffic** (planting, tillage, harvest, hay equipment) waits after a soaking
  rain until the soil has dried: extension guidance puts the compaction-risk window
  at roughly 24–72 h depending on texture, so the default drying times are
  sand 12 h, loam 24 h, clay 48 h per soaking rain.
* **Time-of-day windows** per implement (e.g. combines wait for dew to burn off).
* **Job dependencies** with a minimum wait (hay must cure after mowing before it is
  raked, planting follows tillage, ...).
* **Rain damage on cut hay**: acres mown but not yet baled when rain falls are counted
  as damaged (Wisconsin trials measured ~22 % dry-matter loss from 1 in of rain).

Everything here is derived from the scenario's weather timeline and the simulator's
own job state, so the same rules can be replayed inside optimizer rollouts (letting the
planner react to the *forecast*) without touching the simulator's code.

When conditions turn unsafe mid-job, the active tractor is stopped and its job is
released with completed acres kept: the same path the simulator uses for breakdowns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_DRYING_HOURS = {"sand": 12.0, "loam": 24.0, "clay": 48.0}
DEFAULT_RULES: dict[str, Any] = {
    # implement -> rule. Missing implements have no restrictions.
    "sprayer": {"min_wind_mph": 3.0, "max_wind_mph": 10.0, "daylight_only": True, "no_rain": True},
}


@dataclass
class _RainSpell:
    start: int
    end: int          # first dry tick after the spell
    total_mm: float


@dataclass
class FieldEnvironment:
    conditions: dict[str, Any]
    tick_minutes: int
    halts: int = 0
    yard_moves: int = 0
    halt_log: list[dict[str, Any]] = field(default_factory=list)
    rain_damage: dict[str, float] = field(default_factory=dict)  # field_id -> damaged acres
    _spell_damage: dict[tuple[str, int], float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        c = self.conditions or {}
        self.start_hour = float(c.get("start_hour", 6.0))
        self.sunrise = float(c.get("sunrise_hour", 6.0))
        self.sunset = float(c.get("sunset_hour", 20.5))
        self.weather = sorted(c.get("weather", []), key=lambda w: w["tick"])
        self.rules: dict[str, Any] = {**DEFAULT_RULES, **c.get("rules", {})}
        self.soil_traffic = set(c.get("soil_traffic_implements", []))
        self.field_soil: dict[str, str] = c.get("field_soil", {})
        self.drying = {**DEFAULT_DRYING_HOURS, **c.get("drying_hours", {})}
        self.soak_mm = float(c.get("soaking_rain_mm", 12.0))
        self.rain_stop_mm_hr = float(c.get("rain_stops_work_mm_hr", 1.0))
        self.deps: dict[str, dict[str, Any]] = c.get("dependencies", {})
        self.hay_chains: list[dict[str, str]] = c.get("hay_chains", [])
        self.ticks_per_hour = 60.0 / self.tick_minutes
        self.spells = self._rain_spells()

    @property
    def enabled(self) -> bool:
        return bool(self.conditions)

    # ---------------------------------------------------------------- weather
    def weather_at(self, tick: int) -> dict[str, float]:
        cur = {"wind_mph": 6.0, "rain_mm_hr": 0.0}
        for w in self.weather:
            if w["tick"] > tick:
                break
            cur = {"wind_mph": float(w.get("wind_mph", cur["wind_mph"])),
                   "rain_mm_hr": float(w.get("rain_mm_hr", 0.0))}
        return cur

    def hour_at(self, tick: int) -> float:
        return (self.start_hour + tick / self.ticks_per_hour) % 24.0

    def is_daylight(self, tick: int) -> bool:
        return self.sunrise <= self.hour_at(tick) < self.sunset

    def _rain_spells(self) -> list[_RainSpell]:
        if not self.weather:
            return []
        last = max(w["tick"] for w in self.weather) + 1
        spells, cur = [], None
        for t in range(0, last + 1):
            r = self.weather_at(t)["rain_mm_hr"]
            if r > 0 and cur is None:
                cur = _RainSpell(t, t, 0.0)
            if r > 0 and cur is not None:
                cur.total_mm += r / self.ticks_per_hour
            if r <= 0 and cur is not None:
                cur.end = t
                spells.append(cur)
                cur = None
        if cur is not None:
            cur.end = last + 1
            spells.append(cur)
        return spells

    def soil_dry_at_tick(self, field_id: str, tick: int) -> int | None:
        """Tick at which this field's soil is workable again, or None if not wet."""
        soil = self.field_soil.get(field_id, "loam")
        wet_until = None
        for sp in self.spells:
            if sp.start > tick or sp.total_mm < self.soak_mm:
                continue
            # a downpour of at least twice the soaking amount takes 50% longer to dry
            hours = self.drying.get(soil, 24.0) * (1.5 if sp.total_mm >= 2 * self.soak_mm else 1.0)
            until = sp.end + int(round(hours * self.ticks_per_hour))
            if tick < until:
                wet_until = max(wet_until or 0, until)
        return wet_until

    # ------------------------------------------------------------------ rules
    def block_reason(self, sim: Any, job: Any, tick: int | None = None) -> str | None:
        """Why this job may not run at ``tick`` (default: now); None if it may."""
        if not self.enabled:
            return None
        tick = sim.current_tick if tick is None else tick
        imp = job.required_implement
        w = self.weather_at(tick)
        rule = self.rules.get(imp, {})
        hour = self.hour_at(tick)
        if rule.get("daylight_only") and not self.is_daylight(tick):
            return "night: daylight-only operation"
        if "hours" in rule:
            lo, hi = rule["hours"]
            if not lo <= hour < hi:
                return f"outside {lo:g}:00–{hi:g}:00 window"
        if rule.get("no_rain") and w["rain_mm_hr"] > 0:
            return "raining"
        if w["rain_mm_hr"] >= self.rain_stop_mm_hr and imp in self.soil_traffic:
            return "raining"
        if "max_wind_mph" in rule and w["wind_mph"] > rule["max_wind_mph"]:
            return f"wind {w['wind_mph']:.0f} mph > {rule['max_wind_mph']:g} mph (drift)"
        if "min_wind_mph" in rule and w["wind_mph"] < rule["min_wind_mph"]:
            return f"wind {w['wind_mph']:.0f} mph < {rule['min_wind_mph']:g} mph (inversion risk)"
        if imp in self.soil_traffic:
            until = self.soil_dry_at_tick(job.field_id, tick)
            if until is not None:
                soil = self.field_soil.get(job.field_id, "loam")
                return f"{soil} soil too wet until t{until}"
        dep = self.deps.get(job.id)
        if dep:
            before = sim.jobs.get(dep["after"])
            if before is None or before.status != "completed" or before.completed_tick is None:
                return f"waits for {dep['after']}"
            ready = before.completed_tick + int(round(float(dep.get("min_hours", 0)) * self.ticks_per_hour))
            if tick < ready:
                return f"{dep.get('why', 'waiting')} until t{ready}"
        return None

    def gate(self, sim: Any, job: Any) -> bool:
        return self.block_reason(sim, job) is None

    # ------------------------------------------------------------- per tick
    def _halt_unsafe(self, sim: Any, record: bool) -> None:
        for t in sorted(sim.tractors.values(), key=lambda x: x.id):
            # Only machines actually working stop; a tractor still driving arrives,
            # is stopped on arrival if conditions are still unsafe, and waits there.
            if not t.job_id or t.status != "working":
                continue
            job = sim.jobs[t.job_id]
            reason = self.block_reason(sim, job)
            if reason is None:
                continue
            # Same release path the simulator uses for breakdowns: acres are kept.
            sim._interrupt_job(t, f"conditions: {reason}")
            t.status = "idle"
            sim.version += 1
            sim._log("condition_halt", f"{t.id} stopped {job.id}: {reason}",
                     {"tractor_id": t.id, "job_id": job.id, "reason": reason})
            if record:
                self.halts += 1
                self.halt_log.append({"tick": sim.current_tick, "tractor_id": t.id,
                                      "job_id": job.id, "reason": reason})

    def _clear_parked_blockers(self, sim: Any, record: bool) -> None:
        """Crew logistics: an idle machine parked in another machine's way (typically
        on a field entrance after finishing) pulls aside one cell, or, if there is no
        free cell beside it, drives back to the yard. farm_sim never moves idle
        tractors, so without this a mower left on a field would block the rake and
        baler for that field forever. A pull-aside is charged one cell of travel/fuel."""
        fields = {tuple(f["position"]) for f in sim.fields.values()}
        for t in sorted(sim.tractors.values(), key=lambda x: x.id):
            if t.status not in {"traveling", "to_refuel"} or t.waiting_ticks < 2 or len(t.route) < 2:
                continue
            occupied = {x.position: x for x in sim.tractors.values()}
            blocker = occupied.get(tuple(t.route[1]))
            if blocker is None or blocker.id == t.id or blocker.status != "idle" or blocker.job_id:
                continue
            route = {tuple(p) for p in t.route} | {t.position}
            x, y = blocker.position
            spots = [(x + dx, y + dy) for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))]
            spots = [p for p in spots if sim._inside(p) and p not in sim.blocked_cells and p not in occupied
                     and p not in route and p != sim.base]
            spots.sort(key=lambda p: (p in fields, p))  # prefer not to park on another field
            moved = False
            if spots and blocker.fuel_liters >= blocker.travel_liters_per_cell:
                blocker.position = spots[0]
                blocker.fuel_liters -= blocker.travel_liters_per_cell
                sim.counters["travel_cells"] += 1
                sim.counters["fuel_used_liters"] += blocker.travel_liters_per_cell
                sim.version += 1
                moved = True
            else:
                path = sim.find_path(blocker.position, sim.base)
                if path and t.position not in {tuple(p) for p in path}:
                    moved = bool(sim.request_refuel(blocker.id, target_pct=100).get("ok"))
            if moved:
                sim._log("yard_move", f"{blocker.id} pulled aside to let {t.id} through",
                         {"tractor_id": blocker.id, "for_tractor_id": t.id})
                if record:
                    self.yard_moves += 1

    def _track_rain_damage(self, sim: Any) -> None:
        tick = sim.current_tick
        if self.weather_at(tick)["rain_mm_hr"] <= 0:
            return
        spell = next((i for i, sp in enumerate(self.spells) if sp.start <= tick < sp.end), -1)
        for chain in self.hay_chains:
            mow, bale = sim.jobs.get(chain["mow"]), sim.jobs.get(chain["bale"])
            if not mow or not bale:
                continue
            exposed = max(0.0, mow.completed_acres - bale.completed_acres)
            key = (chain["field"], spell)
            if exposed > self._spell_damage.get(key, 0.0):
                self._spell_damage[key] = exposed
                self.rain_damage[chain["field"]] = sum(v for (f, _), v in self._spell_damage.items()
                                                       if f == chain["field"])

    def after_tick(self, sim: Any) -> None:
        """Recorded environment step for the real run (pass to run_episode on_tick)."""
        if not self.enabled:
            return
        self._track_rain_damage(sim)
        self._halt_unsafe(sim, record=True)
        self._clear_parked_blockers(sim, record=True)

    def rollout_hook(self, sim: Any) -> None:
        """Unrecorded step for optimizer look-ahead clones."""
        if self.enabled:
            self._halt_unsafe(sim, record=False)
            self._clear_parked_blockers(sim, record=False)

    # ---------------------------------------------------------------- views
    def metrics(self) -> dict[str, Any]:
        return {"weather_halts": self.halts, "yard_moves": self.yard_moves,
                "rain_damaged_acres": round(sum(self.rain_damage.values()), 2)}

    def snapshot(self, sim: Any, forecast_ticks: int = 16) -> dict[str, Any]:
        tick = sim.current_tick
        w = self.weather_at(tick)
        implements = sorted({j.required_implement for j in sim.jobs.values()})
        blocked: dict[str, str] = {}
        for imp in implements:
            sample = next((j for j in sim.jobs.values() if j.required_implement == imp
                           and j.status != "completed"), None)
            if sample is not None:
                r = self.block_reason(sim, sample)
                if r and not r.startswith(("waits for", "cure", "settle")) and "until t" not in r:
                    blocked[imp] = r
        soils = {}
        for fid in sim.fields:
            until = self.soil_dry_at_tick(fid, tick) if self.enabled else None
            soils[fid] = {"soil": self.field_soil.get(fid, "loam"), "wet_until": until}
        hour = self.hour_at(tick)
        return {
            "enabled": self.enabled,
            "hour": round(hour, 2),
            "clock": f"{int(hour):02d}:{int(round((hour % 1) * 60)) % 60:02d}",
            "day": int((self.start_hour + tick / self.ticks_per_hour) // 24) + 1,
            "daylight": self.is_daylight(tick),
            "wind_mph": w["wind_mph"],
            "rain_mm_hr": w["rain_mm_hr"],
            "blocked_implements": blocked,
            "soils": soils,
            "job_blocks": {j.id: r for j in sim.jobs.values() if j.status != "completed"
                           and (r := self.block_reason(sim, j))},
            "forecast": [{"tick": tick + i, **self.weather_at(tick + i), "daylight": self.is_daylight(tick + i)}
                         for i in range(0, forecast_ticks)],
            **self.metrics(),
        }
