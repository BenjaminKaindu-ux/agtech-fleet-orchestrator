# Run the Fleet: Tractor Fleet Simulator

A self-contained Python simulation for the HackArizona “Run the Fleet” challenge. It models the fleet coordination layer: job assignment, travel, field work, traffic, fuel, deadlines, breakdown recovery, and route disruptions. It does not try to simulate GPS steering or tractor hardware.

The simulator and dashboard use only Python's standard library. No internet connection or third-party package is required to run them.

## Start the interactive dashboard

From this folder, run:

```bash
python dashboard.py
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). The dashboard displays a live grid map, tractor status and fuel, job progress, key performance metrics, and an event log. Use the controls to advance time, dispatch jobs, trigger a breakdown, repair a tractor, block or reopen a route cell, change priority, or reset the scenario.

The **Demo dispatch** button uses a deterministic priority/deadline/nearest-feasible heuristic. **FCFS baseline** assigns oldest feasible jobs first. These are demonstration policies, not LLM decisions.

## Run a headless scenario

```bash
python farm_sim.py --ticks 40 --policy priority-nearest
```

Use the FCFS baseline instead:

```bash
python farm_sim.py --ticks 40 --policy fcfs
```

Load a different scenario:

```bash
python farm_sim.py --scenario scenario.json --ticks 40 --policy fcfs
```

The command prints a JSON report containing the final fleet state, performance metrics, and event log. Save it to a file for the hackathon demo/submission:

```bash
python farm_sim.py --ticks 40 --policy priority-nearest > event-log.json
```

## Simulation model

- **Map and travel:** rectangular grid; tractors travel one four-directional cell per tick along a shortest path. Blocked cells force detours. Tractors wait rather than occupy the same cell.
- **Time:** the scenario defaults to 15 minutes per tick. One tractor can make one movement step or perform one interval of work in a tick.
- **Fields and jobs:** a job has a field, operation, implement requirement, acres, priority (1 highest through 5 lowest), and deadline tick. Jobs on the same field are serialized.
- **Tractors:** each has a position, compatible implements, implement-specific work rate, fuel tank and starting fuel, reserve requirement, travel/work burn, and refueling rate.
- **Dispatch safety:** a full dispatch plan is checked before any assignment is applied. Checks cover tractor/job availability, implement, route, fuel including return-to-base reserve, estimated deadline, duplicate assignments, field conflicts, and stale state versions.
- **Breakdown recovery:** a failed tractor releases its unfinished job. Completed acres remain recorded; the remaining acres are eligible for reassignment. Repairs may be scheduled or triggered from the dashboard.
- **Route events:** blocked cells trigger alternate route calculation or a blocked state if there is no path. Reopening a path allows stopped tractors to resume.
- **Fuel:** tractors consume fuel during travel and field work. They can be sent back to base to refuel. If they run out, work remains accounted for and the tractor is stranded for recovery.
- **Metrics:** completed/on-time/late jobs, completed acres, travel cells, fuel used, idle tractor-ticks, breakdowns, dispatches, and replans.

## Scenario file

Edit `scenario.json` to change the farm. It defines grid size and obstacles, the base, fields, tractors, jobs, and tick length. Positions are `[x, y]`; deadlines and scheduled repairs are measured in simulation ticks. The default demo includes three tractors, six fields, eight jobs, different implement capabilities, an obstacle detour, varied deadlines, and shared-field jobs.

## Use from Python

```python
from farm_sim import FarmSimulator, priority_nearest_plan

sim = FarmSimulator.from_file("scenario.json")
proposal = priority_nearest_plan(sim)
result = sim.commit_plan(proposal, expected_version=sim.version)
sim.advance(8)
print(sim.metrics())
print(sim.fleet_state())
```

Other main operations include `sim.pending_jobs()`, `sim.estimate_job(tractor_id, job_id)`, `sim.validate_plan(assignments, expected_version)`, `sim.breakdown(tractor_id, repair_after_ticks=5)`, `sim.block_cell((x, y))`, `sim.change_priority(job_id, priority)`, and `sim.request_refuel(tractor_id)`.

## Comparing a coordinator with the baseline

Use the same scenario and tick horizon for both policies. The headless JSON output includes identical metrics for `fcfs` and `priority-nearest`. To compare an LLM coordinator later, have it propose and commit plans through the `FarmSimulator` methods, then compare its final `metrics()` with FCFS under the same scenario and event schedule. Report the scenario, event schedule, horizon, completed jobs/acres, travel cells, fuel, and idle tractor-ticks together; one run is a demo, not a statistically reliable performance result.

## Scope and limitations

This is an intentionally detailed hack-day **fleet** simulator, not a validated agricultural operations model. It abstracts fields as entrances and work as acres-per-hour; it does not simulate field geometry, turning, weather, crop growth, soil, implement setup time, or low-level vehicle safety. Grid distance is a simple proxy for travel distance, and the fuel/work rates are illustrative values in the scenario. Treat it as a coordination demo, not a real tractor control system.
