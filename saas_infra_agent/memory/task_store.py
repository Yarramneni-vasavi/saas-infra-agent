"""Persistent task store for the BUILD agent's DAG task plan.

Tasks live in the same SQLite database as the checkpointer, keyed by
thread_id, so an interrupted build can be resumed in a later process by
reloading the plan instead of re-planning. The plan is a DAG: each task
declares the ids it depends on, and save_tasks rejects graphs with unknown
dependencies or cycles.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from saas_infra_agent.config.config import config
from saas_infra_agent.observability.logger import get_logger

logger = get_logger(__name__)

VALID_STATUSES = ("pending", "in_progress", "completed")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS build_tasks (
    thread_id   TEXT NOT NULL,
    position    INTEGER NOT NULL,
    task_id     TEXT NOT NULL,
    description TEXT NOT NULL,
    status      TEXT NOT NULL,
    depends_on  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (thread_id, task_id)
)
"""


def _connect() -> sqlite3.Connection:
    db_path = config["memory"]["db_path"]
    Path(db_path).parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute(_SCHEMA)
    return conn


def validate_dag(tasks: list[dict]) -> str | None:
    """Return an error message if the task list is not a valid DAG, else None."""
    ids = [t["id"] for t in tasks]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        return f"Duplicate task ids: {sorted(duplicates)}"

    known = set(ids)
    for t in tasks:
        for dep in t.get("depends_on", []):
            if dep == t["id"]:
                return f"Task '{t['id']}' depends on itself"
            if dep not in known:
                return f"Task '{t['id']}' depends on unknown task '{dep}'"
        if t.get("status", "pending") not in VALID_STATUSES:
            return f"Task '{t['id']}' has invalid status '{t['status']}' (use one of {VALID_STATUSES})"

    # Kahn's algorithm: if a topological order can't consume every task, there is a cycle.
    remaining_deps = {t["id"]: set(t.get("depends_on", [])) for t in tasks}
    resolved: set[str] = set()
    while True:
        ready = [tid for tid, deps in remaining_deps.items() if not deps - resolved and tid not in resolved]
        if not ready:
            break
        resolved.update(ready)
    unresolved = known - resolved
    if unresolved:
        return f"Dependency cycle involving tasks: {sorted(unresolved)}"
    return None


def save_tasks(thread_id: str, tasks: list[dict]) -> None:
    """Replace the stored plan for this thread with the given task list."""
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        with conn:
            conn.execute("DELETE FROM build_tasks WHERE thread_id = ?", (thread_id,))
            conn.executemany(
                "INSERT INTO build_tasks VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        thread_id,
                        position,
                        t["id"],
                        t["description"],
                        t.get("status", "pending"),
                        json.dumps(t.get("depends_on", [])),
                        now,
                    )
                    for position, t in enumerate(tasks)
                ],
            )
    finally:
        conn.close()
    logger.info(f"Saved {len(tasks)} tasks for thread {thread_id}")


def load_tasks(thread_id: str) -> list[dict]:
    """Load the stored plan for this thread, in original plan order."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT task_id, description, status, depends_on FROM build_tasks"
            " WHERE thread_id = ? ORDER BY position",
            (thread_id,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"id": task_id, "description": description, "status": status, "depends_on": json.loads(depends_on)}
        for task_id, description, status, depends_on in rows
    ]


def ready_tasks(tasks: list[dict]) -> list[dict]:
    """Pending tasks whose dependencies are all completed — safe to start now."""
    completed = {t["id"] for t in tasks if t["status"] == "completed"}
    return [
        t
        for t in tasks
        if t["status"] == "pending" and all(dep in completed for dep in t["depends_on"])
    ]


def render_tasks(tasks: list[dict]) -> str:
    """Human/model-readable view of the plan: statuses, dependencies, and what is ready."""
    marks = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}
    lines = []
    for t in tasks:
        deps = f" (depends on: {', '.join(t['depends_on'])})" if t["depends_on"] else ""
        lines.append(f"{marks[t['status']]} {t['id']}: {t['description']}{deps}")
    ready = ready_tasks(tasks)
    if ready:
        lines.append(f"\nReady to start now: {', '.join(t['id'] for t in ready)}")
    elif any(t["status"] != "completed" for t in tasks):
        lines.append("\nNo tasks ready — finish the in-progress tasks first.")
    else:
        lines.append("\nAll tasks completed.")
    return "\n".join(lines)
