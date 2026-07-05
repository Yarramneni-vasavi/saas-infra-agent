from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from langchain_core.messages import HumanMessage
from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

from saas_infra_agent.config.config import config
from saas_infra_agent.llm.factory import get_llm, get_small_llm
from saas_infra_agent.observability.logger import get_logger

logger = get_logger(__name__)


Stage = Literal["clarify", "requirements", "architecture", "decisions", "save", "done"]
AwaitKind = Literal["clarify", "requirements_confirm", "architecture_feedback", "approve"]


class _ClarifyOut(BaseModel):
    questions: list[str] = Field(default_factory=list)


class _ConfirmOut(BaseModel):
    confirmed: bool
    notes: str | None = None


class DesignFlowState(BaseModel):
    # Input channel
    user_message: str = ""

    # Running context
    project_context: str = ""

    # Stage control
    stage: Stage = "clarify"
    awaiting_kind: AwaitKind | None = None
    awaiting_prompt: str | None = None

    # Artifacts
    requirements_md: str = ""
    architecture_md: str = ""
    decisions_md: str = ""

    # Output to the CLI
    assistant_output: str = ""


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _init_or_reset(state: DesignFlowState) -> DesignFlowState:
    # If a previous design run finished, start a new one on the next user message.
    if state.stage == "done" and state.user_message.strip():
        return DesignFlowState(user_message=state.user_message)
    return state


def _capture_user_message(state: DesignFlowState) -> DesignFlowState:
    msg = state.user_message.strip()
    if not msg:
        state.assistant_output = "Please describe what you want to build."
        return state

    if not state.project_context.strip():
        state.project_context = msg
    else:
        state.project_context = f"{state.project_context}\n\nUser: {msg}"
    return state


def _clarify_prepare(state: DesignFlowState) -> DesignFlowState:
    llm = get_small_llm().with_structured_output(_ClarifyOut)
    prompt = (
        "You are a requirements-clarification assistant.\n"
        "Given the project context, produce ONLY the highest-signal clarifying questions.\n"
        "Return JSON: {questions: [...]}.\n\n"
        f"Project context:\n{state.project_context}\n"
    )
    out = llm.invoke(prompt)
    questions = [q.strip() for q in out.questions if isinstance(q, str) and q.strip()]

    if not questions:
        state.stage = "requirements"
        return state

    state.awaiting_kind = "clarify"
    state.awaiting_prompt = "Clarifying questions:\n- " + "\n- ".join(questions) + "\n\nReply with answers."
    state.assistant_output = state.awaiting_prompt
    return state


def _requirements_prepare(state: DesignFlowState) -> DesignFlowState:
    llm = get_llm()
    system = (
        "You are a requirements writer.\n"
        "Write ONLY a 'Requirements' section in Markdown (bullets + short subsections).\n"
        "Do NOT propose architecture. Do NOT ask questions.\n"
        "If something is unknown, write it under 'Unknowns'.\n"
    )
    prompt = f"{system}\n\nProject context:\n{state.project_context}\n"
    resp = llm.invoke([HumanMessage(content=prompt)])
    state.requirements_md = getattr(resp, "content", str(resp)).strip()

    state.awaiting_kind = "requirements_confirm"
    state.awaiting_prompt = (
        "Draft requirements:\n\n"
        f"{state.requirements_md}\n\n"
        "Reply with `yes` to confirm, or provide corrections/changes."
    )
    state.assistant_output = state.awaiting_prompt
    return state


def _architecture_prepare(state: DesignFlowState) -> DesignFlowState:
    llm = get_llm()
    system = (
        "You are an infrastructure architect.\n"
        "Produce ONLY a 'Proposed Architecture' section in Markdown.\n"
        "Do NOT ask questions. Do NOT include requirements. Do NOT include decisions.\n"
        "Assume AWS unless specified otherwise.\n"
    )
    prompt = (
        f"{system}\n\n"
        "Confirmed requirements:\n"
        f"{state.requirements_md}\n"
    )
    resp = llm.invoke([HumanMessage(content=prompt)])
    state.architecture_md = getattr(resp, "content", str(resp)).strip()

    state.awaiting_kind = "architecture_feedback"
    state.awaiting_prompt = (
        "Proposed architecture:\n\n"
        f"{state.architecture_md}\n\n"
        "Reply with feedback/changes, or reply `ok` to proceed."
    )
    state.assistant_output = state.awaiting_prompt
    return state


def _decisions_prepare(state: DesignFlowState) -> DesignFlowState:
    llm = get_llm()
    system = (
        "You are a design decision assistant.\n"
        "Produce ONLY a 'Key Decisions' section in Markdown (bullets).\n"
        "Include rationale + trade-offs briefly.\n"
        "Do NOT ask questions. Do NOT include architecture diagrams.\n"
    )
    prompt = (
        f"{system}\n\n"
        "Requirements:\n"
        f"{state.requirements_md}\n\n"
        "Architecture:\n"
        f"{state.architecture_md}\n"
    )
    resp = llm.invoke([HumanMessage(content=prompt)])
    state.decisions_md = getattr(resp, "content", str(resp)).strip()

    state.awaiting_kind = "approve"
    state.awaiting_prompt = (
        "Key decisions:\n\n"
        f"{state.decisions_md}\n\n"
        "Reply `approve` to finalize and save `architecture.md`, or reply with changes."
    )
    state.assistant_output = state.awaiting_prompt
    return state


def _wait_for_user(state: DesignFlowState) -> DesignFlowState:
    if not state.awaiting_kind or not state.awaiting_prompt:
        state.assistant_output = "Internal error: nothing to wait for."
        return state

    user_reply = interrupt({"kind": state.awaiting_kind, "prompt": state.awaiting_prompt})
    # On resume, interrupt() returns the user's value.
    reply = str(user_reply or "").strip()
    if reply:
        state.project_context = f"{state.project_context}\n\nUser: {reply}"

    kind = state.awaiting_kind
    state.awaiting_kind = None
    state.awaiting_prompt = None

    if kind == "clarify":
        state.stage = "requirements"
        return state

    if kind == "requirements_confirm":
        confirm_llm = get_small_llm().with_structured_output(_ConfirmOut)
        out = confirm_llm.invoke(
            "Given the user's reply, decide if they confirmed the requirements.\n"
            "Return JSON: {confirmed: bool, notes: string|null}.\n\n"
            f"Draft requirements:\n{state.requirements_md}\n\n"
            f"User reply:\n{reply}\n"
        )
        if out.confirmed:
            state.stage = "architecture"
        else:
            # incorporate notes into context and regenerate requirements
            if out.notes:
                state.project_context = f"{state.project_context}\n\nNotes: {out.notes}"
            state.stage = "requirements"
        return state

    if kind == "architecture_feedback":
        if reply.lower() in {"ok", "looks good", "good", "yes"}:
            state.stage = "decisions"
        else:
            state.stage = "architecture"
        return state

    if kind == "approve":
        if reply.lower().startswith("approve") or reply.lower() in {"yes", "approved"}:
            state.stage = "save"
        else:
            state.stage = "architecture"
        return state

    state.assistant_output = "Internal error: unknown await kind."
    return state


def _save(state: DesignFlowState) -> DesignFlowState:
    path = Path.cwd() / "architecture.md"
    header = (
        "# Architecture\n\n"
        f"- Generated: {_now_iso()}\n"
        f"- Model: {(config.get('llm') or {}).get('model')}\n\n"
        "---\n\n"
    )
    body = "\n\n".join(
        [
            state.requirements_md.strip(),
            state.architecture_md.strip(),
            state.decisions_md.strip(),
            "\n\nREADY_FOR_BUILD",
        ]
    ).strip() + "\n"
    path.write_text(header + body, encoding="utf-8")
    state.assistant_output = "Saved design to architecture.md\n\nREADY_FOR_BUILD"
    state.stage = "done"
    return state


def _route(state: DesignFlowState) -> str:
    return state.stage


def create_design_workflow_graph(checkpointer):
    builder = StateGraph(DesignFlowState)
    builder.add_node("init", _init_or_reset)
    builder.add_node("capture", _capture_user_message)
    builder.add_node("clarify", _clarify_prepare)
    builder.add_node("requirements", _requirements_prepare)
    builder.add_node("architecture", _architecture_prepare)
    builder.add_node("decisions", _decisions_prepare)
    builder.add_node("wait", _wait_for_user)
    builder.add_node("save", _save)

    builder.set_entry_point("init")
    builder.add_edge("init", "capture")

    # After capture, go to stage route.
    builder.add_conditional_edges(
        "capture",
        _route,
        {
            "clarify": "clarify",
            "requirements": "requirements",
            "architecture": "architecture",
            "decisions": "decisions",
            "save": "save",
            "done": END,
        },
    )

    # Each stage either schedules a wait or advances.
    builder.add_conditional_edges(
        "clarify",
        lambda s: "wait" if s.awaiting_kind else _route(s),
        {"wait": "wait", "requirements": "requirements"},
    )
    builder.add_edge("requirements", "wait")
    builder.add_edge("architecture", "wait")
    builder.add_edge("decisions", "wait")

    builder.add_conditional_edges(
        "wait",
        _route,
        {
            "clarify": "clarify",
            "requirements": "requirements",
            "architecture": "architecture",
            "decisions": "decisions",
            "save": "save",
            "done": END,
        },
    )

    builder.add_edge("save", END)

    return builder.compile(checkpointer=checkpointer)

