from smolagents import CodeAgent, InferenceClientModel, tool, Tool, TransformersModel
from huggingface_hub import get_token, login
if not get_token():  # Colab / first run: prompt; locally a cached `hf auth login` token is reused
    login()

import json
import sys
from pathlib import Path
from smolagents import tool

if "sim" not in globals():
    _here = Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd()
    farm_file = next((f for root in (Path("/content"), _here.parent / "tractor_fleet_sim", _here)
                      if root.exists() for f in root.rglob("farm_sim.py")), None)
    if farm_file is None:
        raise FileNotFoundError(
            "Upload and extract the simulator ZIP so farm_sim.py is in /content."
        )

    project_dir = farm_file.parent
    sys.path.insert(0, str(project_dir))
    from farm_sim import FarmSimulator
    sim = FarmSimulator.from_file(str(project_dir / "scenario.json"))

# Local JSONL memory, bound to the *existing* `sim` (no second simulator).
# Path: FLEET_MEMORY_PATH env var, else data/fleet_memory.jsonl.
try:
    import fleet_memory
except ImportError:
    _mem_file = next(Path.cwd().rglob("fleet_memory.py"), None) or next(Path("/content").rglob("fleet_memory.py"), None)
    if _mem_file is None:
        raise
    sys.path.insert(0, str(_mem_file.parent))
    import fleet_memory

memory_store = fleet_memory.MemoryStore()
comparisons = fleet_memory.ComparisonRegistry()
retrieve_fleet_memory = fleet_memory.make_retrieval_tool(sim, memory_store)

model = InferenceClientModel(
    model_id="Qwen/Qwen2.5-72B-Instruct"
)

# Read-only tools

@tool
def get_fleet_state() -> str:
    """Read tractor status, jobs, map, metrics, and the current state version."""
    state = sim.fleet_state()
    state["state_version"] = state["version"]
    return json.dumps(state)

@tool
def get_pending_jobs() -> str:
    """List unassigned or interrupted jobs with remaining acres and deadlines."""
    return json.dumps({
        "state_version": sim.version,
        "jobs": sim.pending_jobs(),
    })

@tool
def estimate_job(tractor_id: str, job_id: str) -> str:
    """Check whether a tractor can do a job.

    Args:
        tractor_id: Tractor ID, such as T1.
        job_id: Pending job ID, such as J2.
    """
    return json.dumps(sim.estimate_job(tractor_id, job_id))

@tool
def validate_dispatch_plan(assignments_json: str, state_version: int) -> str:
    """Validate a proposed plan without changing simulator state.

    Args:
        assignments_json: JSON array, e.g. [{"tractor_id":"T1","job_id":"J2"}].
        state_version: Version from get_fleet_state.
    """
    try:
        assignments = json.loads(assignments_json)
    except json.JSONDecodeError as exc:
        return json.dumps({"valid": False, "errors": [str(exc)]})

    return json.dumps(
        sim.validate_plan(assignments, expected_version=state_version)
    )

# State-changing tools. Give commit access only to the final coordinator.

@tool
def commit_dispatch_plan(assignments_json: str, state_version: int) -> str:
    """Commit a plan; the simulator revalidates it before making changes.

    Args:
        assignments_json: JSON array, e.g. [{"tractor_id":"T1","job_id":"J2"}].
        state_version: Version used when planning.
    """
    try:
        assignments = json.loads(assignments_json)
    except json.JSONDecodeError as exc:
        return json.dumps({"accepted": False, "errors": [str(exc)]})

    return json.dumps(
        sim.commit_plan(assignments, expected_version=state_version)
    )

@tool
def report_tractor_breakdown(tractor_id: str, repair_after_ticks: int) -> str:
    """Report a breakdown and release unfinished work for reassignment.

    Args:
        tractor_id: ID of the failed tractor.
        repair_after_ticks: Ticks until repair; 0 means repair on next advance.
    """
    return json.dumps(
        sim.breakdown(tractor_id, repair_after_ticks=repair_after_ticks)
    )

@tool
def repair_tractor(tractor_id: str, add_fuel_liters: float) -> str:
    """Repair a broken or stranded tractor.

    Args:
        tractor_id: Tractor ID to repair.
        add_fuel_liters: Optional nonnegative fuel amount to add.
    """
    return json.dumps(
        sim.repair(tractor_id, add_fuel_liters=add_fuel_liters)
    )

@tool
def report_blocked_cell(x: int, y: int) -> str:
    """Block a map cell and trigger route recalculation.

    Args:
        x: Horizontal grid coordinate.
        y: Vertical grid coordinate.
    """
    return json.dumps(sim.block_cell((x, y)))

@tool
def reopen_cell(x: int, y: int) -> str:
    """Reopen a previously blocked map cell.

    Args:
        x: Horizontal grid coordinate.
        y: Vertical grid coordinate.
    """
    return json.dumps(sim.unblock_cell((x, y)))

@tool
def change_job_priority(job_id: str, priority: int) -> str:
    """Change a job's priority, from 1 (highest) to 5 (lowest).

    Args:
        job_id: Job ID to reprioritize.
        priority: New priority from 1 to 5.
    """
    return json.dumps(sim.change_priority(job_id, priority))

@tool
def request_refuel(tractor_id: str, target_pct: float) -> str:
    """Send a tractor to base to refuel.

    Args:
        tractor_id: Tractor ID to refuel.
        target_pct: Desired tank level, from 1 to 100 percent.
    """
    return json.dumps(sim.request_refuel(tractor_id, target_pct=target_pct))

@tool
def advance_simulation(steps: int) -> str:
    """Advance simulated time by a number of ticks.

    Args:
        steps: Number of ticks to advance, from 1 to 100.
    """
    if not 1 <= steps <= 100:
        return json.dumps({"ok": False, "reason": "steps must be from 1 to 100"})
    state = sim.advance(steps)
    return json.dumps({
        "ok": True,
        "tick": state["tick"],
        "state_version": state["version"],
        "metrics": state["metrics"],
    })

#agents
model = InferenceClientModel("Qwen/Qwen2.5-72B-Instruct")
fleet_agent = CodeAgent(
    additional_authorized_imports=["json"],  # tools return JSON strings
    tools=[
        get_fleet_state,
        get_pending_jobs,
        estimate_job,
        validate_dispatch_plan,
        retrieve_fleet_memory,
    ],
    model=model,
    max_steps=8,
    name="fleet_dispatch",
    description="Proposes and validates tractor-job assignments.",
    instructions=(
        "Read the current state and pending jobs. Estimate candidate pairings. "
        "Return a JSON assignments array and the state_version. Do not commit."
        + fleet_memory.MEMORY_PLANNER_NOTE
    ),
)

navigate_agent = CodeAgent(
    additional_authorized_imports=["json"],  # tools return JSON strings
    tools=[get_fleet_state, estimate_job, retrieve_fleet_memory],
    model=model,
    max_steps=6,
    name="navigation",
    description="Reviews route and travel feasibility.",
    instructions=(
        "Use the simulator's route estimates to review proposed assignments. "
        "Report infeasible or high-travel pairings. Do not mutate state."
        + fleet_memory.MEMORY_PLANNER_NOTE
    ),
)

safety_agent = CodeAgent(
    additional_authorized_imports=["json"],  # tools return JSON strings
    tools=[get_fleet_state, validate_dispatch_plan],
    model=model,
    max_steps=6,
    name="safety",
    description="Checks proposed plans against simulator constraints.",
    instructions=(
        "Validate the exact proposed assignments using their state_version. "
        "Report whether valid and list all violations. Do not commit."
    ),
)

agronomy_agent = CodeAgent(
    additional_authorized_imports=["json"],  # tools return JSON strings
    tools=[get_fleet_state, get_pending_jobs, retrieve_fleet_memory],
    model=model,
    max_steps=6,
    name="agronomy",
    description="Reviews field jobs and offers scheduling advice.",
    instructions=(
        "Use only the field and job data provided. State when crop, soil, "
        "growth-stage, or weather data are missing. Do not invent those facts "
        "and do not mutate simulator state."
        + fleet_memory.MEMORY_PLANNER_NOTE
    ),
)

# The coordinator can consult specialists and perform approved state changes.
final_agent = CodeAgent(
    additional_authorized_imports=["json"],  # tools return JSON strings
    tools=[
        get_fleet_state,
        commit_dispatch_plan,
        report_tractor_breakdown,
        repair_tractor,
        report_blocked_cell,
        reopen_cell,
        change_job_priority,
        request_refuel,
        advance_simulation,
        retrieve_fleet_memory,
    ],
    managed_agents=[
        fleet_agent,
        navigate_agent,
        safety_agent,
        agronomy_agent,
    ],
    model=model,
    max_steps=12,
    instructions=(
        "For dispatch, consult Fleet, Navigation, Agronomy, and Safety one at a "
        "time. Commit only if Safety validates the exact assignments and the "
        "state version is current. The simulator performs final validation."
        + fleet_memory.MEMORY_PLANNER_NOTE
    ),
)

# Post-run assessment. Read-only on the simulator: it can read metrics/events,
# look up memories, and save *candidate* lessons. It is not a managed agent of
# the coordinator and holds no dispatch/control tools (enforced by the builder).
#
# Usage after a run (register both sides so improvement claims can be checked):
#   comparisons.register_baseline_report("cmp-1", fcfs_report, scenario_dict, events, 60)
#   comparisons.register_coordinator_run("cmp-1", sim, events, 60)
#   assessment_agent.run("Assess the finished run for comparison_id cmp-1.")
assessment_agent = fleet_memory.build_assessment_agent(
    sim, model, store=memory_store, registry=comparisons,
)
