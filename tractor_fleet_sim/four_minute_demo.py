"""Timed four-minute Run the Fleet demonstration.

Run from this directory with ``python four_minute_demo.py``. The default run
lasts four wall-clock minutes (20 simulated 15-minute ticks at 12 seconds per
tick). Use ``--fast`` to replay the same event sequence immediately.

Environmental values in this demo are explicit, illustrative simulation
assumptions. They are not agronomic calibration data or equipment guidance.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from farm_sim import FarmSimulator, priority_nearest_plan


HERE = Path(__file__).resolve().parent
DEFAULT_SCENARIO = HERE / "scenario.json"
DEMO_TICKS = 20
SECONDS_PER_TICK = 12


class DemoFarmSimulator(FarmSimulator):
    """FarmSimulator with demo-only weather and soil work constraints."""

    def __init__(self, scenario: dict[str, Any]):
        super().__init__(scenario)
        self.weather: dict[str, Any] = {
            "condition": "clear",
            "wind_mph": 3.0,
            "rain": "none",
        }
        self.soil_by_field = {
            field_id: {"condition": "workable", "rate_factor": 1.0, "fuel_factor": 1.0}
            for field_id in self.fields
        }
        self._held_job_tick: set[tuple[str, int, str]] = set()
        self.environment_metrics = {
            "weather_hold_ticks": 0,
            "wet_soil_hold_ticks": 0,
            "soil_slowdown_ticks": 0,
        }

    def set_weather(self, *, condition: str, wind_mph: float, rain: str) -> None:
        self.weather = {"condition": condition, "wind_mph": float(wind_mph), "rain": rain}
        self._log("weather_changed", f"Weather: {condition}; wind {wind_mph:g} mph; rain {rain}", self.weather)

    def set_soil(self, field_id: str, *, condition: str, rate_factor: float, fuel_factor: float) -> None:
        if field_id not in self.fields:
            raise KeyError(f"Unknown field: {field_id}")
        if not 0 < rate_factor <= 1 or fuel_factor < 1:
            raise ValueError("Soil factors must use 0 < rate_factor <= 1 and fuel_factor >= 1")
        self.soil_by_field[field_id] = {
            "condition": condition,
            "rate_factor": float(rate_factor),
            "fuel_factor": float(fuel_factor),
        }
        self._log("soil_changed", f"{field_id} soil condition: {condition}", {
            "field_id": field_id, **self.soil_by_field[field_id],
        })

    def _step_work(self, tractor) -> None:
        if not tractor.job_id or tractor.job_id not in self.jobs:
            return super()._step_work(tractor)

        job = self.jobs[tractor.job_id]
        field_id = job.field_id
        soil = self.soil_by_field[field_id]

        # For this demo, saturated soil pauses field traffic to represent
        # trafficability/compaction risk. A real system needs field-specific
        # measurements and operator-defined thresholds.
        if soil["condition"] == "saturated":
            self.environment_metrics["wet_soil_hold_ticks"] += 1
            self._log_hold_once(job.id, "wet_soil", "Work held on saturated soil to avoid trafficability and compaction risk", {
                "field_id": field_id,
            })
            return

        # Wind-related spray drift can move material off target. The 5 mph
        # threshold is a conservative demo setting, not a universal rule;
        # product labels and local conditions govern real applications.
        if job.required_implement == "sprayer" and self.weather["wind_mph"] > 5.0:
            self.environment_metrics["weather_hold_ticks"] += 1
            self._log_hold_once(job.id, "wind", "Spraying paused because demo wind limit was exceeded", {
                "job_id": job.id,
                "wind_mph": self.weather["wind_mph"],
                "demo_limit_mph": 5.0,
            })
            return

        rate_factor = soil["rate_factor"]
        fuel_factor = soil["fuel_factor"]
        if rate_factor < 1.0 or fuel_factor > 1.0:
            self.environment_metrics["soil_slowdown_ticks"] += 1
            implement = job.required_implement
            original_rate = tractor.acres_per_hour[implement]
            original_fuel_rate = tractor.work_liters_per_hour
            try:
                tractor.acres_per_hour[implement] = original_rate * rate_factor
                tractor.work_liters_per_hour = original_fuel_rate * fuel_factor
                super()._step_work(tractor)
            finally:
                tractor.acres_per_hour[implement] = original_rate
                tractor.work_liters_per_hour = original_fuel_rate
            return

        super()._step_work(tractor)

    def _log_hold_once(self, job_id: str, cause: str, message: str, details: dict[str, Any]) -> None:
        key = (job_id, self.current_tick, cause)
        if key not in self._held_job_tick:
            self._held_job_tick.add(key)
            self._log("operation_held", message, details)

    def metrics(self) -> dict[str, Any]:
        return {**super().metrics(), **self.environment_metrics}


def _dispatch_pending(sim: DemoFarmSimulator) -> list[dict[str, str]]:
    """Use the simulator's deterministic demo policy for this standalone run."""
    plan = priority_nearest_plan(sim)
    if plan:
        result = sim.commit_plan(plan, expected_version=sim.version)
        if result.get("accepted"):
            print("  Coordinator: " + ", ".join(
                f"{item['tractor_id']} → {item['job_id']}" for item in plan
            ), flush=True)
        else:
            print("  Dispatch rejected: " + "; ".join(result.get("errors", [])), flush=True)
    return plan


def _event_schedule(sim: DemoFarmSimulator, tick: int) -> None:
    """Apply reproducible incidents at the start of the named simulation tick."""
    if tick == 0:
        print("  Scenario: dispatching the initial compatible jobs.", flush=True)
        _dispatch_pending(sim)
    elif tick == 3:
        sim.set_weather(condition="gusty", wind_mph=9.0, rain="none")
        print("  EVENT: wind rises; sprayer jobs wait to reduce drift risk.", flush=True)
    elif tick == 5:
        sim.set_weather(condition="rain", wind_mph=4.0, rain="heavy")
        sim.set_soil("F3", condition="saturated", rate_factor=1.0, fuel_factor=1.0)
        print("  EVENT: heavy rain saturates F3; field work there pauses.", flush=True)
    elif tick == 7:
        sim.set_soil("F4", condition="compacted", rate_factor=0.70, fuel_factor=1.20)
        print("  EVENT: F4 has compacted soil; demo work rate falls and fuel per hour rises.", flush=True)
    elif tick == 9:
        result = sim.breakdown("T2", repair_after_ticks=3)
        print(f"  EVENT: T2 breakdown; unfinished work retained for reassignment ({result.get('ok')}).", flush=True)
        _dispatch_pending(sim)
    elif tick == 12:
        result = sim.block_cell((4, 5))
        print(f"  EVENT: route cell (4, 5) blocked; active routes are recalculated ({result.get('ok')}).", flush=True)
    elif tick == 14:
        sim.set_weather(condition="clearing", wind_mph=3.0, rain="stopped")
        sim.set_soil("F3", condition="workable", rate_factor=1.0, fuel_factor=1.0)
        print("  EVENT: wind drops and F3 dries enough for the demo to resume work.", flush=True)
        sim.unblock_cell((4, 5))
        _dispatch_pending(sim)
    elif tick == 17:
        print("  Coordinator: checking newly available tractors and remaining jobs.", flush=True)
        _dispatch_pending(sim)


def run_demo(*, scenario_path: Path = DEFAULT_SCENARIO, seconds_per_tick: float = SECONDS_PER_TICK,
             fast: bool = False, output_path: Path | None = None) -> dict[str, Any]:
    scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
    sim = DemoFarmSimulator(scenario)
    print("RUN THE FLEET — FOUR-MINUTE FIELD DAY", flush=True)
    print(f"{len(sim.tractors)} tractors · {len(sim.fields)} fields · {len(sim.jobs)} jobs · {DEMO_TICKS} ticks", flush=True)
    print("Environmental effects are illustrative demo settings, not calibrated agronomy.", flush=True)
    print("Each tick represents 15 simulated minutes.", flush=True)

    started = time.monotonic()
    for tick in range(DEMO_TICKS):
        _event_schedule(sim, tick)
        if tick % 2 == 0:
            metrics = sim.metrics()
            print(
                f"[{tick * seconds_per_tick:>5.0f}s | farm {sim.current_tick * sim.tick_minutes:>3} min] "
                f"completed {metrics['completed_jobs']}/{len(sim.jobs)} jobs, "
                f"{metrics['acres_completed']:.1f}/{metrics['total_job_acres']:.1f} acres; "
                f"pending {len(sim.pending_jobs())}",
                flush=True,
            )
        if not fast and seconds_per_tick > 0:
            time.sleep(seconds_per_tick)
        sim.advance(1)

    elapsed = time.monotonic() - started
    report = {
        "demo": "Run the Fleet four-minute field day",
        "wall_clock_seconds": round(elapsed, 2),
        "simulated_minutes": sim.current_tick * sim.tick_minutes,
        "environment": {
            "weather": sim.weather,
            "soil_by_field": sim.soil_by_field,
            "assumptions": {
                "spraying_paused_above_wind_mph": 5.0,
                "F4_compacted_soil_rate_factor": 0.70,
                "F4_compacted_soil_fuel_factor": 1.20,
                "F3_saturated_soil": "field work paused until the demo clears the condition",
            },
        },
        "metrics": sim.metrics(),
        "tractors": sim.fleet_state()["tractors"],
        "jobs": sim.fleet_state()["jobs"],
        "event_log": sim.event_log,
        "source_notes": [
            "UMN Extension: wet soils are particularly susceptible to compaction; staying off wet soil can reduce risk.",
            "Penn State Extension: wind can carry spray droplets off target; product labels and conditions matter.",
        ],
    }
    print("\nDEMO COMPLETE", flush=True)
    print(json.dumps(report["metrics"], indent=2), flush=True)
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Full event log saved to: {output_path}", flush=True)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a four-minute tractor fleet demo.")
    parser.add_argument("--scenario", type=Path, default=DEFAULT_SCENARIO, help="scenario JSON (default: scenario.json beside this file)")
    parser.add_argument("--seconds-per-tick", type=float, default=SECONDS_PER_TICK,
                        help="wall-clock seconds per simulation tick; default 12 for a four-minute run")
    parser.add_argument("--fast", action="store_true", help="replay the same 20-tick demo without waiting")
    parser.add_argument("--output", type=Path, help="optional path for the full JSON event log")
    args = parser.parse_args()
    try:
        run_demo(scenario_path=args.scenario, seconds_per_tick=args.seconds_per_tick,
                 fast=args.fast, output_path=args.output)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"Could not run demo: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
