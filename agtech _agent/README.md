# AgTech agent: tractor fleet coordinator with local memory

Smolagents agents and a deterministic coordinator on top of the `farm_sim` tractor fleet
simulator, plus a small offline memory of candidate lessons.

| File | Purpose |
|------|---------|
| `coordinator.py` | Loads the simulator, runs the optimizer, FCFS, and priority-nearest planners, applies scripted events, and explains plans |
| `agent.py` | Notebook-style smolagents setup: tools bound to the global `sim`, plus the specialist, coordinator, and assessment agents |
| `fleet_memory.py` | JSONL memory store, comparison registry, memory tools, and the assessment-agent builder |
| `api_server.py` | Standard-library HTTP service around one live simulator, used by the dashboard in `../hackathon` |
| `scenario.json`, `events.json` | Demo scenario and the scripted event schedule |

## Setup

```bash
uv sync                                # or: python -m venv .venv && .venv/bin/pip install -e .
# The simulator must be next to this project: ../tractor_fleet_sim/farm_sim.py
# (or set TRACTOR_SIM_DIR=/path/to/tractor_fleet_sim)
```

The memory system adds **no new dependencies, no database, and no external service**.
`agent.py` still calls `huggingface_hub.login()` and uses a hosted model, like before.
Memory storage and retrieval work fully offline.

## Persistence

| Setting | Value |
|---------|-------|
| Default file | `data/fleet_memory.jsonl` (relative to this project; created on first save) |
| Override | `FLEET_MEMORY_PATH=/some/where.jsonl`, or `MemoryStore("/some/where.jsonl")` |
| Format | one JSON object per line, append-only; malformed lines are skipped on read |

Back up or reset memory by copying or deleting that file. Nothing else stores memory.

### Record shape

```json
{
  "id": "mem-fe07e06384", "schema": 1, "status": "candidate",
  "created_at": "2026-11-15T22:31:06+00:00",
  "scenario": "Cochise Valley Mixed Field Day", "comparison_id": "cmp-1",
  "tags": {"tractors": ["T2"], "event_types": ["breakdown", "job_interrupted"], "operations": ["mow", "mower"]},
  "measured": {
    "sim_tick": 46, "state_version": 56,
    "observed_metrics": {"completed_jobs": 7, "travel_cells": 43, "idle_tractor_ticks": 29, "...": "..."},
    "baseline": {"policy": "fcfs", "metrics": {"completed_jobs": 7, "travel_cells": 61, "...": "..."}},
    "comparison": {"comparable": true, "reasons": [], "horizon_ticks": 60,
                   "deltas": {"travel_cells": -18, "idle_tractor_ticks": 5, "...": "..."}},
    "recent_events": [{"tick": 5, "type": "breakdown", "tractor_id": "T2"}, "..."]
  },
  "interpretation": {"lesson": "…", "author": "assessment_agent", "claims_improvement": true}
}
```

- **`measured`** is filled in by code from the live simulator and the comparison registry:
  metrics, deltas, up to 12 notable events. The model cannot write these numbers.
- **`interpretation`** is the only model-written part: a lesson of at most 300 characters.
- **`status` is always `candidate`.** Nothing promotes a record to a rule. Promotion is a
  human decision made outside this code.

## Rules the memory enforces

1. **The current simulator is authoritative.** Every retrieval result starts with a notice
   giving the live tick and state version. Planners must still estimate and validate against
   the simulator.
2. **Improvement claims require a fair comparison.** `ComparisonRegistry` fingerprints each
   run's scenario (hash), event schedule (hash), and tick horizon. A lesson is rejected if it
   sets `claims_improvement=true` or uses comparative words (better, worse, improved, beat,
   baseline, FCFS, vs, …) while the baseline and coordinator runs differ in any of the three,
   or if either run is missing. Deltas are stored only when the runs are comparable.
3. **Concise records.** A lesson is capped at 300 characters, metrics are limited to a fixed set
   of keys, and at most 12 notable events are kept (routine movement and refuelling are dropped).
4. **Retrieval returns at most 5 records.** A record matches if it shares the tractor, event
   type, operation (job type or implement, substring match), or scenario. Results are ranked
   by number of matching filters, then by recency.

## How the planners use memory

`agent.py` builds one store and one retrieval tool bound to the **existing** `sim`. No second
simulator is created:

```python
memory_store = fleet_memory.MemoryStore()
comparisons  = fleet_memory.ComparisonRegistry()
retrieve_fleet_memory = fleet_memory.make_retrieval_tool(sim, memory_store)
```

`retrieve_fleet_memory(tractor_id=None, event_type=None, operation=None, scenario=None)` is
given to `fleet_dispatch`, `navigation`, `agronomy`, and the final coordinator. Each agent's
instructions end with `MEMORY_PLANNER_NOTE`, which says memories are hints only and the
simulator's state, estimates, and validation override them. `safety` deliberately does not
get it, because it only validates.

## How the assessment agent uses memory

`assessment_agent = fleet_memory.build_assessment_agent(sim, model, store=memory_store, registry=comparisons)`

| Tool | Access |
|------|--------|
| `get_run_metrics` | read: live metrics, tick, version |
| `get_recent_events` | read: notable simulator events |
| `get_comparison` | read: baseline vs observed, and whether they are comparable |
| `retrieve_fleet_memory` | read: past candidates |
| `save_candidate_memory` | append one candidate lesson |

It has no tools to commit plans, report breakdowns, repair, block cells, change priorities,
refuel, or advance time. `build_assessment_agent` raises `ValueError` if any of those is
passed in. It is not a managed agent of the coordinator, and nothing in it can edit another
agent's instructions.

A typical post-run flow (the baseline is its own episode, so it gets its own simulator; the
live `sim` is the coordinator run):

```python
import coordinator as c

H, EV = 60, c.DEFAULT_EVENTS
base_sim, base_ev = c.load_simulator(), c.EventSchedule.from_file(EV)
base = c.report(base_sim, c.run_episode(base_sim, c.fcfs_planner, base_ev, ticks=H,
                                        replan_every_tick=True, verbose=False), base_ev, "fcfs")

# ... run the coordinator on the existing `sim` for H ticks with the same EV ...

comparisons.register_baseline_report("cmp-1", base, base_sim.scenario, EV, H)
comparisons.register_coordinator_run("cmp-1", sim, EV, H)
assessment_agent.run("Assess the finished run for comparison_id cmp-1.")
```

## HTTP service and dashboard

```bash
.venv/bin/python api_server.py      # http://127.0.0.1:8765, see the docstring for endpoints
```

The full stack (Spring Boot API + React dashboard) is in `../hackathon`. See its README.

## Known issue (not changed)

`main.py` is the PyCharm template and starts with a stray `nt`, so running it raises
`NameError`. It isn't used by anything here.
