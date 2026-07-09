from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from langchain_core.messages import HumanMessage
from langgraph.graph import END, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from saas_infra_agent.config.config import config
from saas_infra_agent.llm.factory import get_llm, get_small_llm
from saas_infra_agent.observability.logger import get_logger
from .skills_catalog import list_skills, read_skill_md

logger = get_logger(__name__)

# ── types ─────────────────────────────────────────────────────────────────────

Stage     = Literal["clarify", "requirements", "architecture", "decisions", "save", "done"]
AwaitKind = Literal["clarify", "requirements_confirm", "architecture_feedback", "approve"]


# ── structured outputs ────────────────────────────────────────────────────────

class _ClarifyOut(BaseModel):
    questions: list[str] = Field(default_factory=list)


class _ConfirmOut(BaseModel):
    confirmed: bool
    notes: str | None = None


class _SkillSelectOut(BaseModel):
    skills: list[str] = Field(default_factory=list)


# ── state ─────────────────────────────────────────────────────────────────────

class DesignFlowState(BaseModel):
    # Input channel
    user_message: str = ""

    # Running context — append-only log of the conversation so far
    project_context: str = ""

    # Stage control
    stage: Stage = "clarify"
    awaiting_kind: AwaitKind | None = None
    awaiting_prompt: str | None = None

    # FIX 3: store questions so we can pop one at a time
    pending_questions: list[str] = Field(default_factory=list)

    # Artifacts built up through the flow
    requirements_md: str = ""
    selected_skills: list[str] = Field(default_factory=list)
    skills_md: str = ""
    architecture_md: str = ""
    decisions_md: str = ""

    # What the CLI should print to the user this turn
    assistant_output: str = ""


# ── helpers ───────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _append_context(state: DesignFlowState, text: str) -> None:
    """Append a line to project_context in-place."""
    state.project_context = (
        f"{state.project_context}\n\nUser: {text}" if state.project_context else text
    )


# ── node: init ────────────────────────────────────────────────────────────────

def _init_or_reset(state: DesignFlowState) -> DesignFlowState:
    """If a previous run finished, reset for the new session."""
    if state.stage == "done" and state.user_message.strip():
        return DesignFlowState(user_message=state.user_message)
    return state


# ── node: capture ─────────────────────────────────────────────────────────────

def _capture_user_message(state: DesignFlowState) -> DesignFlowState:
    msg = state.user_message.strip()
    if not msg:
        state.assistant_output = "Please describe what you want to build."
        return state
    _append_context(state, msg)
    return state


# ── node: clarify ─────────────────────────────────────────────────────────────

def _clarify_prepare(state: DesignFlowState) -> DesignFlowState:
    """
    FIX 1: Always interrupt — never skip silently to requirements.
    FIX 2: Hard cap at 3 questions in prompt AND in Python.
    FIX 3: Ask ONE question per interrupt cycle using pending_questions.
    """

    # Only generate the question list on the FIRST entry to this node.
    # On subsequent entries (looping through questions) the list is already set.
    if not state.pending_questions:
        llm = get_small_llm().with_structured_output(_ClarifyOut)
        prompt = (
            "You are a requirements-clarification assistant for a cloud infrastructure agent.\n"
            "The user is responsible for their own application code and business logic.\n"
            "Your job is ONLY to gather information that directly influences AWS infrastructure decisions.\n\n"

            "TWO CATEGORIES of questions to consider:\n\n"

            "1. SCALE + OPERATIONS (infrastructure-direct):\n"
            "   - Expected daily users, peak traffic, latency SLA\n"
            "   - Budget, cloud region, GPU needed\n"
            "   - Traffic pattern: steady / bursty / scheduled\n\n"

            "2. APPLICATION COMPONENTS that constrain infrastructure:\n"
            "   - Which database engine does the app use? (PostgreSQL/MySQL/MongoDB)\n"
            "     → determines RDS engine, version, extensions (e.g. pgvector)\n"
            "   - Does the app use a cache? (Redis/Memcached)\n"
            "     → determines ElastiCache engine\n"
            "   - Does the app use WebSockets or long-lived connections?\n"
            "     → affects ALB configuration\n"
            "   - Does the app have background workers or cron jobs?\n"
            "     → determines if separate ECS task definitions needed\n"
            "   - Does the app store or serve large files?\n"
            "     → determines S3 setup and CDN need\n"
            "   - Does the app send emails or SMS?\n"
            "     → determines SES / SNS setup\n"
            "   - Does the app run ML inference or embedding generation?\n"
            "     → determines GPU instance need\n\n"

            "STRICT RULES:\n"
            "- Return AT MOST 5 questions. Hard limit — never exceed it.\n"
            "- Only ask questions whose answers would meaningfully CHANGE the infrastructure.\n"
            "- If the context already answers a question, do NOT ask it again.\n"
            "- Do NOT ask about business logic, UI, frameworks, or features.\n"
            "- Prioritise application component questions if the app stack is unclear.\n\n"

            "Return JSON: {questions: [...]}\n\n"
            f"Project context:\n{state.project_context}\n"
        )
        out = llm.invoke(prompt)
        # FIX 2 — enforce cap in Python regardless of what LLM returned
        questions = [q.strip() for q in out.questions if isinstance(q, str) and q.strip()]
        state.pending_questions = questions[:5]

        logger.info(f"clarify: generated {len(state.pending_questions)} questions")

    # No questions at all → still interrupt once to confirm before proceeding
    if not state.pending_questions:
        state.awaiting_kind   = "clarify"
        state.awaiting_prompt = (
            "I have enough context to proceed.\n"
            "Reply `ok` to continue, or tell me anything else I should know."
        )
        state.assistant_output = state.awaiting_prompt
        return state

    # FIX 3 — pop ONE question, ask it, leave rest in pending_questions
    next_q = state.pending_questions.pop(0)
    q_total = 5 - len(state.pending_questions)  # which question number we are on

    state.awaiting_kind   = "clarify"
    state.awaiting_prompt = f"Question {q_total}: {next_q}"
    state.assistant_output = state.awaiting_prompt

    logger.info(f"clarify: asking question — {next_q!r} | remaining={len(state.pending_questions)}")
    return state


# ── node: requirements ────────────────────────────────────────────────────────

def _requirements_prepare(state: DesignFlowState) -> DesignFlowState:
    """Generate requirements summary and interrupt for user confirmation."""
    llm = get_llm()
    system = (
        "You are a requirements writer for a cloud infrastructure agent.\n"
        "Write ONLY a '## Requirements' section in Markdown (bullets + short subsections).\n"
        "Do NOT propose architecture. Do NOT ask questions.\n"
        "Mark anything still unknown under '### Unknowns'.\n"
    )
    prompt = f"{system}\n\nProject context:\n{state.project_context}\n"
    resp = llm.invoke([HumanMessage(content=prompt)])
    state.requirements_md = getattr(resp, "content", str(resp)).strip()

    state.awaiting_kind   = "requirements_confirm"
    state.awaiting_prompt = (
        f"{state.requirements_md}\n\n"
        "---\n"
        "Does this capture your requirements correctly?\n"
        "Reply `yes` to confirm, or describe what needs changing."
    )
    state.assistant_output = state.awaiting_prompt
    logger.info("requirements: generated draft, waiting for confirmation")
    return state


# ── node: architecture ────────────────────────────────────────────────────────

def _architecture_prepare(state: DesignFlowState) -> DesignFlowState:
    """Load skills, generate architecture proposal, interrupt for feedback."""

    # Load skills only once
    if not state.skills_md.strip():
        catalog = [
            {"name": s.name, "description": s.description, "path": s.path}
            for s in list_skills()
        ]
        if catalog:
            selector = get_small_llm().with_structured_output(_SkillSelectOut)
            sel = selector.invoke(
                "Select the MINIMUM set of skills (by name) needed for this project.\n"
                "Return JSON: {skills: [...]}.\n\n"
                f"Project context:\n{state.project_context}\n\n"
                f"Skill catalog:\n{catalog}\n"
            )
            chosen = [n.strip() for n in sel.skills[:5] if isinstance(n, str) and n.strip()]
            docs   = [doc for n in chosen if (doc := read_skill_md(n))]
            state.selected_skills = chosen
            state.skills_md       = "\n\n".join(docs).strip()
            logger.info(f"architecture: loaded skills {chosen}")

    llm = get_llm()
    system = (
        "You are an AWS cloud infrastructure architect.\n"
        "Propose ONLY the infrastructure design — not application code.\n"
        "Produce a '## Proposed Architecture' section in Markdown.\n"
        "For every AWS service, state: instance type/size, justification, and monthly cost range.\n"
        "Do NOT ask questions. Do NOT include decisions or trade-offs (those come next).\n"
        "Assume AWS unless the requirements state otherwise.\n"
    )
    prompt = (
        f"{system}\n\n"
        f"Confirmed requirements:\n{state.requirements_md}\n"
        + (f"\n\nSkill guidance:\n{state.skills_md}\n" if state.skills_md else "")
    )
    resp = llm.invoke([HumanMessage(content=prompt)])
    state.architecture_md = getattr(resp, "content", str(resp)).strip()

    state.awaiting_kind   = "architecture_feedback"
    state.awaiting_prompt = (
        f"{state.architecture_md}\n\n"
        "---\n"
        "Reply `ok` to proceed to key decisions, or describe what you'd like to change."
    )
    state.assistant_output = state.awaiting_prompt
    logger.info("architecture: generated proposal, waiting for feedback")
    return state


# ── node: decisions ───────────────────────────────────────────────────────────

def _decisions_prepare(state: DesignFlowState) -> DesignFlowState:
    """Generate key decisions + trade-offs, interrupt for final approval."""
    llm = get_llm()
    system = (
        "You are a design decision assistant.\n"
        "Produce ONLY a '## Key Decisions' section in Markdown (bullet list).\n"
        "For each decision include: what was chosen, why, and one alternative with trade-off.\n"
        "Add a '## Risks / Unknowns' section at the end.\n"
        "Do NOT ask questions. Do NOT repeat the architecture.\n"
    )
    prompt = (
        f"{system}\n\n"
        f"Requirements:\n{state.requirements_md}\n\n"
        f"Architecture:\n{state.architecture_md}\n"
        + (f"\n\nSkill guidance:\n{state.skills_md}\n" if state.skills_md else "")
    )
    resp = llm.invoke([HumanMessage(content=prompt)])
    state.decisions_md = getattr(resp, "content", str(resp)).strip()

    state.awaiting_kind   = "approve"
    state.awaiting_prompt = (
        f"{state.decisions_md}\n\n"
        "---\n"
        "Reply `approve` to save the design to `architecture.md`,\n"
        "or describe what you'd like to change."
    )
    state.assistant_output = state.awaiting_prompt
    logger.info("decisions: generated, waiting for approval")
    return state


# ── node: wait (human-in-the-loop) ────────────────────────────────────────────

def _wait_for_user(state: DesignFlowState) -> DesignFlowState:
    """
    Single interrupt node — all human-in-the-loop pauses route through here.
    Resumes when the user replies, then advances or loops stage accordingly.
    """
    if not state.awaiting_kind:
        state.assistant_output = "Internal error: wait node reached with no awaiting_kind."
        return state

    # ── INTERRUPT — execution stops here until user replies ───────────────────
    user_reply = interrupt({
        "kind":   state.awaiting_kind,
        "prompt": state.awaiting_prompt,
    })
    # ── RESUME — user has replied, interrupt() returns their input ─────────────

    reply = str(user_reply or "").strip()
    logger.info(f"wait: resumed kind={state.awaiting_kind!r} reply={reply[:60]!r}")

    if reply:
        _append_context(state, reply)

    kind = state.awaiting_kind
    state.awaiting_kind   = None
    state.awaiting_prompt = None

    # ── clarify: one question answered, check if more remain ─────────────────
    if kind == "clarify":
        if state.pending_questions:
            # Still questions left → loop back to clarify for next one
            state.stage = "clarify"
            logger.info(f"wait/clarify: {len(state.pending_questions)} questions remaining")
        else:
            # All questions answered → move to requirements
            state.stage = "requirements"
            logger.info("wait/clarify: all questions answered, advancing to requirements")
        return state

    # ── requirements confirm ──────────────────────────────────────────────────
    if kind == "requirements_confirm":
        confirm_llm = get_small_llm().with_structured_output(_ConfirmOut)
        out = confirm_llm.invoke(
            "Did the user confirm the requirements?\n"
            "Return JSON: {confirmed: bool, notes: string|null}.\n\n"
            f"Draft requirements:\n{state.requirements_md}\n\n"
            f"User reply:\n{reply}\n"
        )
        if out.confirmed:
            state.stage = "architecture"
            logger.info("wait/requirements: confirmed, advancing to architecture")
        else:
            if out.notes:
                _append_context(state, f"Correction: {out.notes}")
            state.stage = "requirements"
            logger.info("wait/requirements: not confirmed, regenerating requirements")
        return state

    # ── architecture feedback ─────────────────────────────────────────────────
    if kind == "architecture_feedback":
        if reply.lower() in {"ok", "looks good", "good", "yes", "approved", "fine"}:
            state.stage = "decisions"
            logger.info("wait/architecture: accepted, advancing to decisions")
        else:
            # User has changes → context already updated, regenerate architecture
            state.stage = "architecture"
            logger.info("wait/architecture: changes requested, regenerating")
        return state

    # ── approve ───────────────────────────────────────────────────────────────
    if kind == "approve":
        if reply.lower().startswith("approve") or reply.lower() in {"yes", "approved", "ok"}:
            state.stage = "save"
            logger.info("wait/approve: approved, advancing to save")
        else:
            # User wants changes → loop back to architecture
            state.stage = "architecture"
            logger.info("wait/approve: changes requested, looping to architecture")
        return state

    state.assistant_output = f"Internal error: unknown await kind '{kind}'."
    return state


# ── node: save ────────────────────────────────────────────────────────────────

def _save(state: DesignFlowState) -> DesignFlowState:
    """Write the finalised design to architecture.md."""
    path   = Path.cwd() / "architecture.md"
    model  = (config.get("llm") or {}).get("model", "unknown")
    header = (
        "# Architecture\n\n"
        f"- Generated: {_now_iso()}\n"
        f"- Model: {model}\n\n"
        "---\n\n"
    )
    body = "\n\n---\n\n".join(
        filter(None, [
            state.requirements_md.strip(),
            state.architecture_md.strip(),
            state.decisions_md.strip(),
        ])
    ) + "\n\nREADY_FOR_BUILD\n"

    path.write_text(header + body, encoding="utf-8")
    logger.info(f"save: wrote architecture.md ({path.stat().st_size} bytes)")

    state.assistant_output = (
        "✅ Design saved to `architecture.md`.\n\n"
        "Say `build` when you're ready to generate Terraform and Docker configs."
    )
    state.stage = "done"
    return state


# ── routing ───────────────────────────────────────────────────────────────────

def _route_from_stage(state: DesignFlowState) -> str:
    return state.stage


def _route_clarify(state: DesignFlowState) -> str:
    """After clarify node runs, go to wait if there's a question, else requirements."""
    return "wait" if state.awaiting_kind else "requirements"


# ── graph assembly ────────────────────────────────────────────────────────────

def create_design_workflow_graph(checkpointer):
    builder = StateGraph(DesignFlowState)

    # Register nodes
    builder.add_node("init",         _init_or_reset)
    builder.add_node("capture",      _capture_user_message)
    builder.add_node("clarify",      _clarify_prepare)
    builder.add_node("requirements", _requirements_prepare)
    builder.add_node("architecture", _architecture_prepare)
    builder.add_node("decisions",    _decisions_prepare)
    builder.add_node("wait",         _wait_for_user)
    builder.add_node("save",         _save)

    # Entry
    builder.set_entry_point("init")
    builder.add_edge("init", "capture")

    # After capture → route by stage
    builder.add_conditional_edges(
        "capture",
        _route_from_stage,
        {
            "clarify":      "clarify",
            "requirements": "requirements",
            "architecture": "architecture",
            "decisions":    "decisions",
            "save":         "save",
            "done":         END,
        },
    )

    # clarify → wait (if question) or requirements (if no questions at all)
    builder.add_conditional_edges(
        "clarify",
        _route_clarify,
        {"wait": "wait", "requirements": "requirements"},
    )

    # These nodes always go to wait
    builder.add_edge("requirements", "wait")
    builder.add_edge("architecture", "wait")
    builder.add_edge("decisions",    "wait")

    # wait → route by updated stage (clarify loop, requirements, architecture, decisions, save)
    builder.add_conditional_edges(
        "wait",
        _route_from_stage,
        {
            "clarify":      "clarify",
            "requirements": "requirements",
            "architecture": "architecture",
            "decisions":    "decisions",
            "save":         "save",
            "done":         END,
        },
    )

    builder.add_edge("save", END)

    return builder.compile(checkpointer=checkpointer)