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


def _pdr_skill_excerpt() -> str:
    """Load the pdr-generation skill and keep only the parts relevant to intake."""
    doc = read_skill_md("pdr-generation") or ""
    if not doc.strip():
        return ""

    start = doc.find("## PDR Document Structure")
    if start == -1:
        return doc.strip()[:8000]
    end = doc.find("## Writing Conventions")
    if end == -1:
        end = len(doc)
    return doc[start:end].strip()[:12000]


def _pdr_gap_questions(state: "DesignFlowState", *, max_questions: int) -> list[str]:
    """Ask only what we still need to write a build-ready PDR."""
    excerpt = _pdr_skill_excerpt()
    llm = get_small_llm().with_structured_output(_ClarifyOut)
    prompt = (
        "You are the intake/clarification assistant for a SaaS infrastructure Design Agent.\n"
        "Your goal is to gather ONLY the MINIMUM critical inputs needed to write a complete PDR\n"
        "that is unambiguous enough for Terraform generation.\n\n"
        "Use the PDR template requirements below as the checklist. If anything required by the\n"
        "template/completeness gate is missing or ambiguous, you may ask a question to fill that gap.\n"
        "However, for non-critical items you should assume sensible AWS industry defaults and the\n"
        "Design agent will record them as Assumptions in the final PDR.\n\n"
        "Rules:\n"
        f"- Return AT MOST {max_questions} questions.\n"
        "- Ask only the TOP questions whose answers materially affect the infrastructure design.\n"
        "- If the context already contains the answer, DO NOT ask it again.\n"
        "- Keep questions crisp and answerable in 1-2 sentences.\n"
        "- Prefer requirements that map to the Requirements Summary table (workload, scale, SLA,\n"
        "  compliance, budget).\n"
        "- Do NOT ask about specific AWS service choices unless the user already mentioned that\n"
        "  component (e.g. don't ask about RDS Multi-AZ if no database was specified).\n"
        "- If unclear, prefer a single question that clarifies the overall architecture pattern.\n\n"
        "Return JSON: {questions: [...]}.\n\n"
        f"PDR template excerpt:\n{excerpt or '(missing skill doc)'}\n\n"
        f"Current project context (Q/A log + notes):\n{state.project_context}\n\n"
        f"Current draft requirements (if any):\n{state.requirements_md or '(none)'}\n"
    )
    out = llm.invoke(prompt)
    questions = [q.strip() for q in out.questions if isinstance(q, str) and q.strip()]
    return questions[:max_questions]


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
    clarify_asked_count: int = 0
    last_clarify_question: str | None = None
    pdr_gap_rounds: int = 0
    pdr_gap_checked: bool = False

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


def _artifact_root() -> Path:
    agent_cfg = dict(config.get("agent") or {})
    artifact_dir = str(agent_cfg.get("artifact_dir", ".") or ".").strip()
    artifact_dir_norm = artifact_dir.strip().strip("/")
    if artifact_dir_norm in {"", "."}:
        return Path.cwd()
    return Path.cwd() / artifact_dir_norm


def _pdr_output_path() -> Path:
    return _artifact_root() / "pdr.md"


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

def _clarify_prepare_pdr(state: DesignFlowState) -> DesignFlowState:
    """Clarification driven by the pdr-generation skill template."""
    target_total_questions = 3

    # If an older session already generated a long list, cap it so we only ask
    # the top N questions total for this run.
    if state.clarify_asked_count >= target_total_questions:
        state.pending_questions = []
    else:
        remaining_budget = target_total_questions - state.clarify_asked_count
        if remaining_budget >= 0 and len(state.pending_questions) > remaining_budget:
            state.pending_questions = state.pending_questions[:remaining_budget]

    if not state.pending_questions:
        # Run the PDR-gap question generator once; keep it lightweight.
        if not state.pdr_gap_checked:
            max_questions = target_total_questions
            state.pending_questions = _pdr_gap_questions(state, max_questions=max_questions)
            state.pdr_gap_checked = True
        else:
            state.pending_questions = []
        state.clarify_asked_count = 0
        state.last_clarify_question = None
        logger.info(f"clarify(pdr): generated {len(state.pending_questions)} PDR-gap questions")

    if not state.pending_questions:
        state.awaiting_kind = "clarify"
        state.awaiting_prompt = (
            "I have enough context to proceed with a build-ready PDR.\n"
            "Reply `ok` to continue, or tell me anything else I should know."
        )
        state.assistant_output = state.awaiting_prompt
        return state

    next_q = state.pending_questions.pop(0)
    state.clarify_asked_count += 1
    state.last_clarify_question = next_q
    state.awaiting_kind = "clarify"
    state.awaiting_prompt = f"Question {state.clarify_asked_count}: {next_q}"
    state.assistant_output = state.awaiting_prompt
    logger.info(f"clarify(pdr): asking question: {next_q!r} | remaining={len(state.pending_questions)}")
    return state


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
        "Reply `approve` to save the design to `pdr.md`,\n"
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

    directive = None
    if isinstance(user_reply, dict):
        maybe_directive = user_reply.get("directive")
        if isinstance(maybe_directive, dict):
            directive = maybe_directive
        reply = str(user_reply.get("text") or user_reply.get("reply") or "").strip()
    else:
        reply = str(user_reply or "").strip()

    logger.info(
        f"wait: resumed kind={state.awaiting_kind!r} reply={reply[:60]!r} directive={bool(directive)}"
    )

    if reply:
        _append_context(state, reply)

    reply_lc = reply.lower()
    reply_tokens = "".join(
        ch if (ch.isalnum() or ch.isspace()) else " " for ch in reply_lc
    ).split()
    is_negative = any(t in reply_tokens for t in {"no", "not", "nope", "nah"})

    kind = state.awaiting_kind
    state.awaiting_kind   = None
    state.awaiting_prompt = None

    directive_action = None
    if isinstance(directive, dict) and directive.get("kind") == kind:
        directive_action = directive.get("action")

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
        if directive_action == "confirm":
            state.stage = "architecture"
            logger.info("wait/requirements: directive confirmed, advancing to architecture")
            return state
        if directive_action == "changes":
            state.stage = "requirements"
            logger.info("wait/requirements: directive requests changes, regenerating requirements")
            return state

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
        if directive_action == "accept":
            state.stage = "decisions"
            logger.info("wait/architecture: directive accepted, advancing to decisions")
            return state
        if directive_action == "changes":
            state.stage = "architecture"
            logger.info("wait/architecture: directive requests changes, regenerating")
            return state

        if (
            not is_negative
            and (
                reply_lc in {"ok", "looks good", "good", "yes", "approved", "fine"}
                or any(t in reply_tokens for t in {"ok", "good", "yes", "fine", "approve", "approved"})
            )
        ):
            state.stage = "decisions"
            logger.info("wait/architecture: accepted, advancing to decisions")
        else:
            # User has changes → context already updated, regenerate architecture
            state.stage = "architecture"
            logger.info("wait/architecture: changes requested, regenerating")
        return state

    # ── approve ───────────────────────────────────────────────────────────────
    if kind == "approve":
        if directive_action == "approve":
            state.stage = "save"
            logger.info("wait/approve: directive approved, advancing to save")
            return state
        if directive_action == "changes":
            state.stage = "architecture"
            logger.info("wait/approve: directive requests changes, looping to architecture")
            return state

        if (
            not is_negative
            and (
                reply_lc.startswith("approve")
                or reply_lc in {"approve", "yes", "approved", "ok"}
                or "approve" in reply_tokens
                or "approved" in reply_tokens
            )
        ):
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
    """Write the finalised design to pdr.md."""
    out_dir = _artifact_root()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = _pdr_output_path()

    pdr_skill = read_skill_md("pdr-generation") or ""
    llm = get_llm()
    prompt = (
        "You are the DESIGN module of a SaaS infrastructure agent.\n"
        "Write the final `pdr.md` as a complete Preliminary Design Review document.\n\n"
        "Follow the PDR skill exactly (section order, required tables, and writing conventions).\n"
        "Do NOT omit required sections.\n"
        "Do NOT invent requirements or numbers; only use what the user provided.\n"
        "If any required detail is unknown, capture it in 'Open Issues & Assumptions' as:\n"
        "- Type = Open issue if it blocks build execution\n"
        "- Type = Assumption if a reasonable default is acceptable\n\n"
        "Return ONLY Markdown for pdr.md.\n\n"
        f"PDR skill:\n{pdr_skill or '(missing pdr-generation skill)'}\n\n"
        f"Conversation Q/A context:\n{state.project_context}\n\n"
        f"Draft requirements section:\n{state.requirements_md}\n\n"
        f"Draft architecture section:\n{state.architecture_md}\n\n"
        f"Draft decisions section:\n{state.decisions_md}\n"
    )
    resp = llm.invoke([HumanMessage(content=prompt)])
    md = getattr(resp, "content", str(resp)).strip()

    path.write_text(md + ("\n" if not md.endswith("\n") else ""), encoding="utf-8")
    logger.info(f"save: wrote pdr.md at {path} ({path.stat().st_size} bytes)")

    state.assistant_output = (
        f"✅ Design saved to `{path.as_posix()}`.\n\n"
        "Say `build` when you're ready to generate Terraform and Docker configs."
    )
    state.stage = "done"
    return state


# ── routing ───────────────────────────────────────────────────────────────────

def apply_pdr_user_reply(
    state: DesignFlowState,
    user_reply: str | dict | None,
) -> DesignFlowState:
    """Apply a resumed human reply to the PDR-oriented wait state.

    This mirrors the logic inside `_wait_for_user_pdr` so eval tooling can
    exercise the design workflow without having to seed a LangGraph checkpoint.
    """
    directive = None
    if isinstance(user_reply, dict):
        maybe_directive = user_reply.get("directive")
        if isinstance(maybe_directive, dict):
            directive = maybe_directive
        reply = str(user_reply.get("text") or user_reply.get("reply") or "").strip()
    else:
        reply = str(user_reply or "").strip()

    logger.info(
        f"wait(pdr): resumed kind={state.awaiting_kind!r} reply={reply[:60]!r} directive={bool(directive)}"
    )

    kind = state.awaiting_kind
    if reply:
        if kind == "clarify" and state.last_clarify_question:
            _append_context(state, f"Q: {state.last_clarify_question}\nA: {reply}")
        else:
            _append_context(state, reply)

    reply_lc = reply.lower()
    reply_tokens = "".join(ch if (ch.isalnum() or ch.isspace()) else " " for ch in reply_lc).split()

    state.awaiting_kind = None
    state.awaiting_prompt = None

    directive_action = None
    if isinstance(directive, dict) and directive.get("kind") == kind:
        directive_action = directive.get("action")

    if kind == "clarify":
        state.last_clarify_question = None
        if state.pending_questions:
            state.stage = "clarify"
            logger.info(f"wait(pdr)/clarify: {len(state.pending_questions)} questions remaining")
        else:
            state.stage = "requirements"
            logger.info("wait(pdr)/clarify: all questions answered, advancing to requirements")
        return state

    if kind == "requirements_confirm":
        if directive_action == "confirm":
            state.stage = "architecture"
            logger.info("wait(pdr)/requirements: directive confirmed, advancing to architecture")
            return state

        if directive_action == "changes":
            state.stage = "requirements"
            logger.info("wait(pdr)/requirements: directive requests changes, regenerating requirements")
            return state

        confirm_llm = get_small_llm().with_structured_output(_ConfirmOut)
        out = confirm_llm.invoke(
            "Did the user confirm the requirements?\n"
            "Return JSON: {confirmed: bool, notes: string|null}.\n\n"
            f"Draft requirements:\n{state.requirements_md}\n\n"
            f"User reply:\n{reply}\n"
        )
        if out.confirmed:
            state.stage = "architecture"
            logger.info("wait(pdr)/requirements: confirmed, advancing to architecture")
        else:
            if out.notes:
                _append_context(state, f"Correction: {out.notes}")
            state.stage = "requirements"
            logger.info("wait(pdr)/requirements: not confirmed, regenerating requirements")
        return state

    if kind == "architecture_feedback":
        if directive_action == "accept":
            state.stage = "decisions"
            logger.info("wait(pdr)/architecture: directive accepted, advancing to decisions")
            return state
        if directive_action == "changes":
            state.stage = "architecture"
            logger.info("wait(pdr)/architecture: directive requests changes, regenerating")
            return state

        if reply_tokens and any(t in reply_tokens for t in {"ok", "okay", "yes", "y"}):
            state.stage = "decisions"
            logger.info("wait(pdr)/architecture: accepted, advancing to decisions")
        else:
            state.stage = "architecture"
            logger.info("wait(pdr)/architecture: changes requested, regenerating")
        return state

    if kind == "approve":
        if directive_action == "approve":
            state.stage = "save"
            logger.info("wait(pdr)/approve: directive approved, advancing to save")
            return state
        if directive_action == "changes":
            state.stage = "architecture"
            logger.info("wait(pdr)/approve: directive requests changes, looping to architecture")
            return state

        if reply_tokens and any(t in reply_tokens for t in {"approve", "approved"}):
            state.stage = "save"
            logger.info("wait(pdr)/approve: approved, advancing to save")
        else:
            state.stage = "architecture"
            logger.info("wait(pdr)/approve: changes requested, looping to architecture")
        return state

    state.assistant_output = f"Internal error: unknown await kind '{kind}'."
    return state


def _wait_for_user_pdr(state: DesignFlowState) -> DesignFlowState:
    """
    PDR-aware interrupt node.

    - Captures clarify answers as Q/A pairs in project_context.
    - After requirements confirmation, re-checks for PDR gaps and asks follow-ups
      before moving on to architecture.
    """
    if not state.awaiting_kind:
        state.assistant_output = "Internal error: wait node reached with no awaiting_kind."
        return state

    user_reply = interrupt({
        "kind": state.awaiting_kind,
        "prompt": state.awaiting_prompt,
    })

    directive = None
    if isinstance(user_reply, dict):
        maybe_directive = user_reply.get("directive")
        if isinstance(maybe_directive, dict):
            directive = maybe_directive
        reply = str(user_reply.get("text") or user_reply.get("reply") or "").strip()
    else:
        reply = str(user_reply or "").strip()

    logger.info(
        f"wait(pdr): resumed kind={state.awaiting_kind!r} reply={reply[:60]!r} directive={bool(directive)}"
    )

    kind = state.awaiting_kind
    if reply:
        if kind == "clarify" and state.last_clarify_question:
            _append_context(state, f"Q: {state.last_clarify_question}\nA: {reply}")
        else:
            _append_context(state, reply)

    reply_lc = reply.lower()
    reply_tokens = "".join(ch if (ch.isalnum() or ch.isspace()) else " " for ch in reply_lc).split()
    is_negative = any(t in reply_tokens for t in {"no", "not", "nope", "nah"})

    state.awaiting_kind = None
    state.awaiting_prompt = None

    directive_action = None
    if isinstance(directive, dict) and directive.get("kind") == kind:
        directive_action = directive.get("action")

    if kind == "clarify":
        state.last_clarify_question = None
        if state.pending_questions:
            state.stage = "clarify"
            logger.info(f"wait(pdr)/clarify: {len(state.pending_questions)} questions remaining")
        else:
            state.stage = "requirements"
            logger.info("wait(pdr)/clarify: all questions answered, advancing to requirements")
        return state

    if kind == "requirements_confirm":
        if directive_action == "confirm":
            state.stage = "architecture"
            logger.info("wait(pdr)/requirements: directive confirmed, advancing to architecture")
            return state

        if directive_action == "changes":
            state.stage = "requirements"
            logger.info("wait(pdr)/requirements: directive requests changes, regenerating requirements")
            return state

        confirm_llm = get_small_llm().with_structured_output(_ConfirmOut)
        out = confirm_llm.invoke(
            "Did the user confirm the requirements?\n"
            "Return JSON: {confirmed: bool, notes: string|null}.\n\n"
            f"Draft requirements:\n{state.requirements_md}\n\n"
            f"User reply:\n{reply}\n"
        )
        if out.confirmed:
            state.stage = "architecture"
            logger.info("wait(pdr)/requirements: confirmed, advancing to architecture")
        else:
            if out.notes:
                _append_context(state, f"Correction: {out.notes}")
            state.stage = "requirements"
            logger.info("wait(pdr)/requirements: not confirmed, regenerating requirements")
        return state

    if kind == "architecture_feedback":
        if directive_action == "accept":
            state.stage = "decisions"
            logger.info("wait(pdr)/architecture: directive accepted, advancing to decisions")
            return state
        if directive_action == "changes":
            state.stage = "architecture"
            logger.info("wait(pdr)/architecture: directive requests changes, regenerating")
            return state

        if reply_tokens and any(t in reply_tokens for t in {"ok", "okay", "yes", "y"}):
            state.stage = "decisions"
            logger.info("wait(pdr)/architecture: accepted, advancing to decisions")
        else:
            state.stage = "architecture"
            logger.info("wait(pdr)/architecture: changes requested, regenerating")
        return state

    if kind == "approve":
        if directive_action == "approve":
            state.stage = "save"
            logger.info("wait(pdr)/approve: directive approved, advancing to save")
            return state
        if directive_action == "changes":
            state.stage = "architecture"
            logger.info("wait(pdr)/approve: directive requests changes, looping to architecture")
            return state

        if reply_tokens and any(t in reply_tokens for t in {"approve", "approved"}):
            state.stage = "save"
            logger.info("wait(pdr)/approve: approved, advancing to save")
        else:
            state.stage = "architecture"
            logger.info("wait(pdr)/approve: changes requested, looping to architecture")
        return state

    state.assistant_output = f"Internal error: unknown await kind '{kind}'."
    return state


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
    builder.add_node("clarify",      _clarify_prepare_pdr)
    builder.add_node("requirements", _requirements_prepare)
    builder.add_node("architecture", _architecture_prepare)
    builder.add_node("decisions",    _decisions_prepare)
    builder.add_node("wait",         _wait_for_user_pdr)
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
