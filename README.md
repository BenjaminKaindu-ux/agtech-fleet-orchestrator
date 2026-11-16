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

## Farmer chat ("Ask the farm")

Click **💬 Ask the farm** (bottom-right, on every tab) or open `http://localhost:5173/?chat=1`.
Farmers ask in plain words — *"How are we doing?"*, *"What is the sprayer doing?"*, *"Can I spray today?"*,
*"What's late?"*, *"Why did the agent pick P1?"*, *"What's up with F2?"*, *"What have you learned?"* —
and get answers built from the **live simulator state** (authoritative) plus the learning memory.

Farmers can also report what's happening: *"S2 broke down"*, *"rush the F2 planting"*, *"refuel T1"*,
*"road at (3,4) is flooded"*. The chat never changes the farm by itself — it proposes the matching
operator event as a button, and only applies it (then the agent replans) when the farmer confirms.

- Works offline: `agtech _agent/farm_chat.py` is a grounded rule-based answerer (`POST /chat {"message"}`).
- Optional: with `HF_TOKEN` set, the grounded answer is rephrased by an LLM (`FARM_CHAT_MODEL`,
  default `Qwen/Qwen2.5-72B-Instruct`; disable with `FARM_CHAT_LLM=0`). Any LLM failure falls back to rules.

## AI crew on the live farm

In **Live operations → AI crew (LLM agents)**, press **Ask AI crew to plan** (optionally with an instruction).
This runs the smolagents team from `agtech _agent/agent.py` (fleet dispatch, navigation, safety, agronomy,
coordinator) **on the live simulator itself** — `ai_crew.py` injects the server's `sim`, so no copy is made.
Whatever the crew commits (dispatches, priorities, refuels, breakdowns) appears immediately on the map,
fleet list and reasoning log (badge `crew`); the planner keeps running around it afterwards.

- `POST /crew/run {"task"}` starts a background job (poll `/jobs/<id>`); typically 1–3 minutes, hard stop at 3.
- The clock is paused while the crew works (stepping returns 409) so its plan matches the state it read.
- The crew is briefed with live facts, including weather/soil holds, and does not get `advance_simulation`.
- Needs a Hugging Face token: `HF_TOKEN`, or a cached `hf auth login`.
