# AgTech Fleet Orchestrator

AI tractor-fleet coordinator with a learning memory, realistic field-condition simulations, and a full-stack dashboard.

```
React + Vite (:5173) → Spring Boot (:8080) → Python simulator service (:8765)
```

| Folder | What it is |
|---|---|
| `agtech _agent/` | Python: coordinator/optimizer, smolagents agents, field conditions, scenario library, Simulation Lab, Hermes-style learning memory, HTTP service |
| `hackathon/` | Spring Boot API gateway (`backend/`) and React dashboard (`frontend/`) — tabs: Live operations, Simulation lab, Agent memory |
| `tractor_fleet_sim/` | The HackArizona “Run the Fleet” tractor simulator the stack runs on |

## Run locally

```bash
cd "agtech _agent" && uv sync            # Python deps (or python -m venv .venv && pip install -e .)
cd ../hackathon && ./start.sh            # starts all three tiers
# open http://localhost:5173
```

**Watch the agents in real time:** on the Live operations tab press **▶ Watch in real time** (pace 1–15 min per scenario, optional loop through all scenarios and fast-forward of quiet nights).
Kiosk link that starts playing immediately: `http://localhost:5173/?tab=live&play=1&pace=5`

Needs Python 3.11+, Java 17+, Node 20+. Agent memory is written to `agtech _agent/data/` (git-ignored).
See `hackathon/README.md` and `agtech _agent/README.md` for details.
