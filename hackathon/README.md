# AI Farm Orchestrator: full stack

A React dashboard and a Spring Boot API on top of the Python tractor fleet simulator,
coordinator, and smolagents agents in `../agtech _agent`.

```
React + Vite (:5173)  ──/api──▶  Spring Boot (:8080)  ──HTTP──▶  Python sim service (:8765)
  dashboard                        API gateway, CORS               ONE live FarmSimulator
                                   no state of its own             coordinator (optimizer/FCFS)
                                                                   fleet_memory (JSONL)
```

**The simulator is the source of truth.** The Java layer forwards requests and keeps no copy of
tractors, jobs, or metrics. Every dispatch and event is validated by `farm_sim` itself.

## Prerequisites

| Tier | Needs |
|------|-------|
| Simulator service | Python 3.11+, `../agtech _agent/.venv` (`uv sync`), and `tractor_fleet_sim/` next to it (or `TRACTOR_SIM_DIR`) |
| Backend | Java 17+ (the bundled `./mvnw` downloads Maven and dependencies on first run) |
| Frontend | Node 20+ |

## Run

One command:

```bash
./start.sh            # open http://localhost:5173
```

Or run each tier in its own terminal:

```bash
cd "../agtech _agent" && .venv/bin/python api_server.py      # :8765
cd backend && ./mvnw spring-boot:run                          # :8080
cd frontend && npm install && npm run dev                     # :5173
```

### Configuration

| Variable | Where | Default |
|----------|-------|---------|
| `SIM_API_PORT` | sim service | `8765` |
| `SCENARIO_PATH`, `EVENTS_PATH` | sim service | `agtech _agent/scenario.json`, `events.json` |
| `FLEET_MEMORY_PATH` | sim service | `agtech _agent/data/fleet_memory.jsonl` |
| `HF_TOKEN`, `FLEET_MODEL_ID` | sim service | none; needed only for the AI assessment button |
| `SIM_SERVICE_URL` | backend | `http://127.0.0.1:8765` |
| `PORT`, `CORS_ORIGINS` | backend | `8080`, the Vite dev and preview origins |
| `BACKEND_URL` | Vite proxy | `http://localhost:8080` |

## Using the dashboard

- **Step / Run workday** advances simulated time. The scripted events in `events.json` fire on
  schedule, and the chosen policy (`optimizer`, `fcfs`, `priority-nearest`) dispatches.
  **Reset** starts a fresh run with a new policy.
- **Field map:** click an open cell to block it (operator `block_cell`). Click a hatched cell to
  reopen it. Dashed lines show the tractors' current routes.
- **Fleet:** break down, repair, or refuel a tractor. **Jobs:** change a job's priority.
  Every operator event is logged and replayed in the baseline (see below).
- **Manual dispatch:** *Estimate* and *Validate* first, then *Commit*. The simulator rejects
  stale state versions and infeasible plans.
- **Agent reasoning log:** the coordinator's plain-language reasons for each dispatch.
- **Fleet memory:**
  1. *Run comparison* replays the FCFS baseline on a fresh simulator with the **same scenario,
     the same scripted + operator events, and the same tick horizon**, then reports measured
     deltas.
  2. *Save candidate*: lessons are capped at 300 characters and always stored as
     `candidate`. A lesson that says a strategy did *better* is rejected unless the comparison
     is comparable.
  3. *Retrieve* returns at most 5 records by tractor, event type, operation, or scenario.
  4. *Ask AI assessment agent* runs the read-only smolagents assessment agent (needs `HF_TOKEN`).
     While it runs, actions that change state return `409`.

## API (Spring Boot, `/api`)

| Method | Path | Body / query |
|--------|------|--------------|
| GET | `/health` | backend + simulator status |
| GET | `/state` | full fleet state, decisions, schedule, last comparison |
| POST | `/reset` | `{"policy": "optimizer"}` |
| POST | `/step` | `{"ticks": 1..100}` |
| POST | `/events` | `{"name": "breakdown", "tractor_id": "T2", "repair_after_ticks": 6}` (also `repair`, `block_cell`, `unblock_cell`, `change_priority`, `refuel`) |
| POST | `/estimate` | `{"tractor_id": "T1", "job_id": "J2"}` |
| POST | `/dispatch` | `{"assignments": [{"tractor_id": "T1", "job_id": "J2"}], "state_version": 12, "commit": false}` |
| POST | `/compare` | none |
| GET | `/memory` | `?tractor_id=&event_type=&operation=&scenario=` (max 5) |
| GET | `/memory/recent` | `?limit=20` |
| POST | `/memory` | `{"lesson", "comparison_id", "claims_improvement", "tractor_ids", "operations"}` |
| POST | `/assess` | `{"comparison_id"}` |

Simulator errors keep their status codes (400, 409, 503). If the simulator service is down,
the API returns `502`.

## Tests

```bash
cd backend && ./mvnw test        # controller tests against a fake simulator + context load
cd frontend && npm run lint && npm run build
```

## Layout

```
backend/    Spring Boot 4 gateway (farm.api.FarmController, farm.sim.SimServiceClient)
frontend/   React 19 + Vite dashboard (src/App.tsx, src/api.ts)
configuration/, integrations/, prediction-models/, results/   placeholders from the original repo
```
