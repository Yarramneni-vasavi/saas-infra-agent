"""DAG task-plan tools for the BUILD agent.

write_tasks/read_tasks replace the built-in flat todo list as the build's
source of truth. The plan is persisted per thread in the task store
(saas_infra_agent.memory.task_store), so a build interrupted mid-run — or
resumed in a fresh process — picks up from the stored plan instead of
re-planning. Tasks form a DAG via depends_on; write_tasks rejects graphs
with cycles or unknown dependencies so the model can correct and retry.
"""

from __future__ import annotations

from typing import Literal

from langchain.tools import ToolRuntime, tool
from pydantic import BaseModel, Field

from saas_infra_agent.memory.task_store import load_tasks, render_tasks, save_tasks, validate_dag
from saas_infra_agent.observability.logger import get_logger

logger = get_logger(__name__)


class Task(BaseModel):
    id: str = Field(description="Short unique kebab-case id, e.g. 'read-arch' or 'gen-main-tf'")
    description: str = Field(description="What this task does, one sentence")
    depends_on: list[str] = Field(
        default_factory=list,
        description="Ids of tasks that must be completed before this one can start",
    )
    status: Literal["pending", "in_progress", "completed"] = "pending"


def _thread_id(runtime: ToolRuntime) -> str:
    return runtime.config["configurable"]["thread_id"]


@tool
def write_tasks(tasks: list[Task], runtime: ToolRuntime) -> str:
    """
    Save the build plan as a DAG of tasks. Pass the FULL task list every time —
    this replaces the stored plan.

    Call it when you first create the plan, and again whenever a task changes
    status (in_progress when you start it, completed when you finish it).
    Each task's depends_on must list only tasks that genuinely have to finish
    first; the graph must have no cycles. The plan is persisted, so an
    interrupted build resumes from it.
    """
    task_dicts = [t.model_dump() for t in tasks]
    error = validate_dag(task_dicts)
    if error:
        logger.warning(f"write_tasks rejected invalid DAG: {error}")
        return f"INVALID PLAN — nothing saved. {error}. Fix the task graph and call write_tasks again."

    save_tasks(_thread_id(runtime), task_dicts)
    return f"Plan saved ({len(task_dicts)} tasks).\n\n{render_tasks(task_dicts)}"


@tool
def read_tasks(runtime: ToolRuntime) -> str:
    """
    Load the stored build plan for this session. Call this FIRST on every run:
    if a plan with incomplete tasks exists, resume it (starting with the tasks
    listed as ready) instead of planning again.
    """
    tasks = load_tasks(_thread_id(runtime))
    if not tasks:
        return "No stored plan for this session — create one with write_tasks."
    return render_tasks(tasks)
