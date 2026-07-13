"""Eval harness for the BUILD agent.

Each eval case runs in a throwaway workspace directory. Everything the agent
touches resolves relative to Path.cwd() — the artifact dir, the SQLite task
store, and the checkpointer — so chdir-ing into the workspace before
create_build_agent() gives full isolation between runs. GitHub MCP tools are
disabled (token env vars stripped) so runs are hermetic and never push.

The agent pauses on request_plan_approval via a LangGraph interrupt; the
harness answers each interrupt from the case's scripted `interrupt_replies`
(falling back to "approve") and records every interrupt prompt so checks can
assert on how many times approval was requested.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from langchain_core.messages import HumanMessage
from langgraph.types import Command

MAX_INTERRUPTS = 5


@dataclass
class ToolCall:
    name: str
    args: dict


@dataclass
class RunResult:
    workspace: Path
    thread_id: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    interrupt_prompts: list[str] = field(default_factory=list)
    interrupt_replies: list[str] = field(default_factory=list)
    final_text: str = ""
    error: str | None = None

    def calls_named(self, name: str) -> list[ToolCall]:
        return [c for c in self.tool_calls if c.name == name]

    def first_index(self, name: str) -> int | None:
        for i, c in enumerate(self.tool_calls):
            if c.name == name:
                return i
        return None

    def write_file_paths(self) -> list[str]:
        """Paths the model *attempted* to write via the filesystem tools."""
        paths = []
        for c in self.tool_calls:
            if c.name in ("write_file", "edit_file"):
                p = c.args.get("file_path") or c.args.get("path")
                if p:
                    paths.append(str(p))
        return paths


def _strip_github_env() -> None:
    os.environ.pop("GITHUB_PERSONAL_ACCESS_TOKEN", None)
    os.environ.pop("GITHUB_TOKEN", None)


def _extract_tool_calls(messages: list) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for m in messages:
        for tc in getattr(m, "tool_calls", None) or []:
            calls.append(ToolCall(name=tc.get("name", "?"), args=tc.get("args") or {}))
    return calls


def _interrupt_prompt(result: dict) -> str | None:
    interrupts = result.get("__interrupt__") or []
    if not interrupts:
        return None
    payload = interrupts[0].value
    if isinstance(payload, dict) and isinstance(payload.get("prompt"), str):
        return payload["prompt"]
    return str(payload)


def run_case(
    workspace: Path,
    query: str,
    interrupt_replies: list[str],
    seed_tasks: list[dict] | None = None,
) -> RunResult:
    """Run one BUILD-agent conversation inside `workspace` and capture it."""
    _strip_github_env()
    prev_cwd = os.getcwd()
    os.chdir(workspace)
    thread_id = f"eval-{uuid.uuid4().hex[:8]}"
    run = RunResult(workspace=workspace, thread_id=thread_id)

    try:
        # Import after chdir: the backend root, task store, and checkpointer
        # all resolve paths at call time, relative to cwd.
        from saas_infra_agent.agent.build_agent import create_build_agent
        from saas_infra_agent.memory.task_store import save_tasks

        if seed_tasks:
            save_tasks(thread_id, seed_tasks)

        agent = create_build_agent()
        cfg = {"configurable": {"thread_id": thread_id}}
        result = agent.invoke({"messages": [HumanMessage(content=query)]}, cfg)

        replies = list(interrupt_replies)
        for _ in range(MAX_INTERRUPTS):
            prompt = _interrupt_prompt(result)
            if prompt is None:
                break
            run.interrupt_prompts.append(prompt)
            reply = replies.pop(0) if replies else "approve"
            run.interrupt_replies.append(reply)
            result = agent.invoke(Command(resume=reply), cfg)
        else:
            run.error = f"still interrupted after {MAX_INTERRUPTS} replies"

        messages = result.get("messages", [])
        run.tool_calls = _extract_tool_calls(messages)
        run.final_text = messages[-1].content if messages else ""
        if isinstance(run.final_text, list):  # content blocks
            run.final_text = " ".join(
                b.get("text", "") if isinstance(b, dict) else str(b) for b in run.final_text
            )
    except Exception as exc:  # the eval report shows the failure; don't crash the suite
        run.error = f"{type(exc).__name__}: {exc}"
    finally:
        os.chdir(prev_cwd)

    return run


def load_stored_tasks(workspace: Path, thread_id: str) -> list[dict]:
    """Read the persisted task plan for a finished run (for grading)."""
    prev_cwd = os.getcwd()
    os.chdir(workspace)
    try:
        from saas_infra_agent.memory.task_store import load_tasks

        return load_tasks(thread_id)
    finally:
        os.chdir(prev_cwd)
