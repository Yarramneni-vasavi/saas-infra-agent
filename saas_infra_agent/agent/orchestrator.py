"""Orchestrator: safety gate -> continuation check -> intent classification.

Simplified design (v1):
- No multi-step intent pipeline, no depends_on state machine.
- A multi-intent query ("design and deploy X") picks ONE starting agent.
  Any other intent mentioned is dropped -- the target agent is expected to
  prompt the user to continue ("design's done, want me to build it?"), and
  that follow-up is handled as an ordinary new query on the next turn.
- Every query goes through this module's `route()` -- there is no path
  that reaches an agent without first passing the safety gate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, Optional
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage

from saas_infra_agent.agent.agents import AgentKind, get_agent
from saas_infra_agent.llm.factory import get_small_llm
from saas_infra_agent.memory.short_term import get_checkpointer
from saas_infra_agent.config.config import config
from saas_infra_agent.agent.safetygate import check_safety, SafetyFlag
from saas_infra_agent.agent.domaingate import check_domain
from saas_infra_agent.observability.logger import get_logger
from langgraph.types import Command

Agent = Literal["design", "build", "monitor", "general"]
logger = get_logger(__name__)


@dataclass
class SessionState:
    """Minimal per-conversation state. No pipeline/step tracking -- see
    module docstring."""
    last_active_agent: Optional[Agent] = None
    arch_md_exists: bool = False
    awaiting_agent_input: bool = False  # True while an agent (e.g. design's
                                         # clarification loop) is mid-question
    conversation_summary: str = ""
    pending_review_query: Optional[str] = None
    pending_review_reasoning: str = ""
    pending_user_query: Optional[str] = None
    pending_routed_intent: Optional[Agent] = None
    last_failure: str = ""


@dataclass
class OrchestratorOutput:
    intent: Optional[Agent]
    confidence: float
    is_continuation: bool
    requires_clarification: bool
    clarification_question: Optional[str]
    safety_flag: SafetyFlag
    reasoning: str
    directive: Optional[dict[str, str]] = None


_CLASSIFY_SYSTEM_PROMPT = """You are the orchestrator for an infrastructure \
assistant with four downstream agents:

- design: designing an architecture for an application, or a solution to an \
existing infra problem. Produces pdr.md.
- build: building, deploying, fixing bug, or writing code for an architecture (given by \
the user or produced by design), enhancing existing deployment code, or \
creating dashboards for existing infra.
- monitor: monitoring, optimizing, or reporting performance for existing or \
newly built infra.
- general: simple Q&A about cloud/infra concepts, conversational questions \
("where did we leave off"), or greetings.

If the user's message mixes more than one of these (e.g. "design and \
deploy X"), pick the SINGLE agent that should run FIRST. Do not try to \
represent the rest of the request -- the chosen agent will prompt the \
user to continue toward the next step once it finishes.

Return ONLY compact JSON matching this shape, no prose, no markdown fences:
{
  "intent": "design" | "build" | "monitor" | "general",
  "confidence": 0.0-1.0,
  "requires_clarification": true | false,
  "clarification_question": "<string, or null>",
  "reasoning": "<one sentence>"
}

Set requires_clarification true only if the message is too ambiguous to \
route at all -- not for missing implementation detail, that belongs to \
the downstream agent's own clarification loop.
"""

_NEW_TOPIC_MARKERS = [
    "instead", "actually", "forget that", "different question",
    "new question", "unrelated",
]

_YES_WORDS = {"yes", "y", "yes proceed", "proceed", "continue", "ok", "okay"}
_NO_WORDS = {"no", "n", "nope", "nah", "cancel", "stop"}


def _tokenize(text: str) -> list[str]:
    t = text.lower().strip()
    cleaned = "".join(ch if (ch.isalnum() or ch.isspace()) else " " for ch in t)
    return cleaned.split()


def _looks_affirmative(text: str) -> bool:
    tokens = _tokenize(text)
    if not tokens:
        return False
    if any(t in tokens for t in _NO_WORDS):
        return False
    if "not" in tokens and any(t in tokens for t in {"ok", "okay", "approve", "approved", "yes"}):
        return False
    return any(
        t in tokens
        for t in {
            "ok",
            "okay",
            "yes",
            "y",
            "approve",
            "approved",
            "good",
            "fine",
            "continue",
            "proceed",
        }
    )


def _design_directive(awaiting_kind: str | None, user_reply: str) -> dict[str, str] | None:
    if not awaiting_kind:
        return None
    if awaiting_kind == "clarify":
        return {"kind": awaiting_kind, "action": "answer"}
    if awaiting_kind == "requirements_confirm":
        return {"kind": awaiting_kind, "action": "confirm" if _looks_affirmative(user_reply) else "changes"}
    if awaiting_kind == "architecture_feedback":
        return {"kind": awaiting_kind, "action": "accept" if _looks_affirmative(user_reply) else "changes"}
    if awaiting_kind == "approve":
        return {"kind": awaiting_kind, "action": "approve" if _looks_affirmative(user_reply) else "changes"}
    return None


def _looks_like_new_topic(query: str) -> bool:
    q = query.lower()
    return any(marker in q for marker in _NEW_TOPIC_MARKERS)


def _state_dir() -> Path:
    state_dir = Path(config["memory"]["db_path"]).parent / "orchestrator_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def _build_waiting_for_approval(thread_id: str) -> bool:
    """True when the BUILD agent is paused on a request_plan_approval interrupt."""
    return pending_approval_prompt(thread_id) is not None


def pending_approval_prompt(thread_id: str) -> str | None:
    """The human-facing prompt of the interrupt this thread is paused on, if any.

    A pending interrupt survives CLI restarts (it lives in the checkpointer), so
    the CLI uses this on startup to re-display what the next message will answer.
    """
    checkpoint_tuple = get_checkpointer().get_tuple({"configurable": {"thread_id": thread_id}})
    if not checkpoint_tuple or not checkpoint_tuple.pending_writes:
        return None
    for _, channel, value in checkpoint_tuple.pending_writes:
        if channel != "__interrupt__":
            continue
        interrupts = value if isinstance(value, (list, tuple)) else [value]
        for intr in interrupts:
            payload = getattr(intr, "value", intr)
            if isinstance(payload, dict) and isinstance(payload.get("prompt"), str):
                return payload["prompt"]
            return str(payload)
    return None


def pending_interrupt_payload(thread_id: str) -> Any | None:
    """The raw interrupt payload this thread is paused on, if any."""
    checkpoint_tuple = get_checkpointer().get_tuple({"configurable": {"thread_id": thread_id}})
    if not checkpoint_tuple or not checkpoint_tuple.pending_writes:
        return None
    for _, channel, value in checkpoint_tuple.pending_writes:
        if channel != "__interrupt__":
            continue
        interrupts = value if isinstance(value, (list, tuple)) else [value]
        for intr in interrupts:
            return getattr(intr, "value", intr)
    return None


def pending_interrupt_kind(thread_id: str) -> str | None:
    payload = pending_interrupt_payload(thread_id)
    if isinstance(payload, dict):
        kind = payload.get("kind")
        if isinstance(kind, str):
            return kind
        kind = payload.get("type")
        if isinstance(kind, str):
            return kind
    return None


def _interrupt_prompt(result: dict) -> str | None:
    """Extract the human-facing prompt when an agent run paused on an interrupt."""
    interrupts = result.get("__interrupt__") or []
    if not interrupts:
        return None
    payload = interrupts[0].value
    if isinstance(payload, dict) and isinstance(payload.get("prompt"), str):
        return payload["prompt"]
    return str(payload)


def _state_path(thread_id: str) -> Path:
    safe_name = "".join(ch for ch in thread_id if ch.isalnum() or ch in ("-", "_"))
    return _state_dir() / f"{safe_name}.json"


def load_session_state(thread_id: str) -> SessionState:
    path = _state_path(thread_id)
    if not path.exists():
        return SessionState()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return SessionState(**data)
    except Exception:
        return SessionState()


def save_session_state(thread_id: str, state: SessionState) -> None:
    _state_path(thread_id).write_text(
        json.dumps(state.__dict__, ensure_ascii=True, indent=2),
        encoding="utf-8",
    )


def _to_agent_kind(intent: Agent) -> AgentKind | None:
    if intent == "design":
        return AgentKind.DESIGN
    if intent == "build":
        return AgentKind.BUILD
    if intent == "monitor":
        return AgentKind.MONITOR
    return None


def _handle_general(query: str, state: SessionState) -> str:
    llm = get_small_llm()
    prompt = (
        "You are a helpful SaaS infrastructure assistant. "
        "Answer conversational or general cloud/infra questions briefly and clearly.\n\n"
        f"Conversation summary: {state.conversation_summary or '(none)'}\n"
        f"Pending unresolved user query: {state.pending_user_query or '(none)'}\n"
        f"Pending routed intent: {state.pending_routed_intent or '(none)'}\n"
        f"Last failure: {state.last_failure or '(none)'}\n"
        f"User message: {query}"
    )
    resp = llm.invoke([HumanMessage(content=prompt)])
    return getattr(resp, "content", str(resp)).strip()


def _append_summary(state: SessionState, user_query: str, assistant_reply: str) -> None:
    snippet = f"User: {user_query}\nAssistant: {assistant_reply}".strip()
    if not state.conversation_summary:
        state.conversation_summary = snippet
    else:
        state.conversation_summary = f"{state.conversation_summary}\n\n{snippet}"[-4000:]


def _mark_inflight_query(
    thread_id: str,
    state: SessionState,
    query: str,
    routed_intent: Agent | None = None,
) -> None:
    state.pending_user_query = query
    state.pending_routed_intent = routed_intent
    state.last_failure = ""
    save_session_state(thread_id, state)


def _clear_inflight_query(state: SessionState) -> None:
    state.pending_user_query = None
    state.pending_routed_intent = None
    state.last_failure = ""


def _artifact_root() -> Path:
    agent_cfg = dict(config.get("agent") or {})
    artifact_dir = str(agent_cfg.get("artifact_dir", ".") or ".").strip()
    artifact_dir_norm = artifact_dir.strip().strip("/")
    if artifact_dir_norm in {"", "."}:
        return Path.cwd()
    return Path.cwd() / artifact_dir_norm


def _pdr_path() -> Path:
    return _artifact_root() / "pdr.md"


def _build_architecture_exists() -> bool:
    return _pdr_path().exists()


def _is_continuation(query: str, state: SessionState) -> bool:
    """Cheap, local continuation check -- no LLM call.

    True when there's an active agent and the query doesn't look like a
    topic change. Intentionally conservative: a false negative just costs
    one extra classification call; a false positive routes a new topic
    into the wrong agent, which is worse. When in doubt, fall through to
    full classification.
    """
    if state.last_active_agent is None:
        return False
    return not _looks_like_new_topic(query)


def _classify(query: str, state: SessionState) -> OrchestratorOutput:
    """Full LLM classification for a new / ambiguous query."""
    user_content = (
        f"Conversation summary: {state.conversation_summary or '(none)'}\n"
        f"arch_md_exists: {state.arch_md_exists}\n\n"
        f"User message: {query}"
    )

    llm = get_small_llm()
    resp = llm.invoke([
        SystemMessage(content=_CLASSIFY_SYSTEM_PROMPT),
        HumanMessage(content=user_content),
    ])
    text = resp.content.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Fail safe: don't guess a destination for unparseable output.
        return OrchestratorOutput(
            intent=None,
            confidence=0.0,
            is_continuation=False,
            requires_clarification=True,
            clarification_question="Sorry, could you rephrase that?",
            safety_flag="none",
            reasoning="Classifier response could not be parsed.",
        )

    return OrchestratorOutput(
        intent=data.get("intent"),
        confidence=float(data.get("confidence", 0.0)),
        is_continuation=False,
        requires_clarification=bool(data.get("requires_clarification", False)),
        clarification_question=data.get("clarification_question"),
        safety_flag="none",  # filled in by route()
        reasoning=data.get("reasoning", ""),
    )


def route(query: str, state: SessionState) -> OrchestratorOutput:
    """Single entry point. Every query goes through here -- no bypass."""
    state.arch_md_exists = _build_architecture_exists()

    # 1. Safety gate always runs first, regardless of continuation status.
    safety = check_safety(query)
    if safety.flag == "block":
        return OrchestratorOutput(
            intent=None,
            confidence=1.0,
            is_continuation=False,
            requires_clarification=False,
            clarification_question=None,
            safety_flag="block",
            reasoning=safety.reasoning,
        )
    elif safety.flag == "needs_review":
        return OrchestratorOutput(
            intent=None,
            confidence=1.0,
            is_continuation=False,
            requires_clarification=True,
            clarification_question=(
                "This request touches sensitive operations and needs an explicit review. "
                "Reply YES to proceed, or anything else to cancel."
            ),
            safety_flag="needs_review",
            reasoning=safety.reasoning,
        )

    # 2. Cheap continuation check -- fast-path, no reclassification call.
    if state.awaiting_agent_input and _is_continuation(query, state):
        return OrchestratorOutput(
            intent=state.last_active_agent,
            confidence=1.0,
            is_continuation=True,
            requires_clarification=False,
            clarification_question=None,
            safety_flag=safety.flag,
            reasoning="Continuation of active agent session.",
        )

    # 3. New/ambiguous topic -- full classification.
    result = _classify(query, state)
    result.safety_flag = safety.flag
    return result


def handle_query(query: str, thread_id: str) -> str:
    """CLI entry point with persisted session state and safety-review interrupt."""
    state = load_session_state(thread_id)

    # If the design graph is paused on an interrupt, treat this thread as awaiting
    # agent input even across CLI restarts (the interrupt lives in the checkpointer).
    design_thread_id = _agent_thread_id(thread_id, "design")
    if pending_interrupt_payload(design_thread_id) is not None:
        state.last_active_agent = state.last_active_agent or "design"
        state.awaiting_agent_input = True
        save_session_state(thread_id, state)

    if state.pending_review_query is not None:
        if query.strip().lower() in _YES_WORDS:
            approved_query = state.pending_review_query
            review_reason = state.pending_review_reasoning
            state.pending_review_query = None
            state.pending_review_reasoning = ""
            _mark_inflight_query(thread_id, state, approved_query)

            try:
                result = _classify(approved_query, state)
                result.safety_flag = "needs_review"
                state.pending_routed_intent = result.intent
                save_session_state(thread_id, state)

                reply = _dispatch(approved_query, result, state, thread_id)
                final_reply = (
                    f"Review acknowledged: {review_reason}\n\n{reply}"
                    if review_reason
                    else reply
                )
                _append_summary(state, approved_query, final_reply)
                _clear_inflight_query(state)
                save_session_state(thread_id, state)
                return final_reply
            except Exception as exc:
                state.last_failure = f"{type(exc).__name__}: {exc}"
                save_session_state(thread_id, state)
                raise

        state.pending_review_query = None
        state.pending_review_reasoning = ""
        cancel_msg = "Cancelled. Send the request again if you want me to re-check it."
        _append_summary(state, query, cancel_msg)
        _clear_inflight_query(state)
        save_session_state(thread_id, state)
        return cancel_msg

    domain = check_domain(query)
    if domain.flag == "out_of_domain":
        reply = (
            "This CLI only supports SaaS infrastructure topics (design/build/monitor). "
            "I can’t help with consumer tasks like shopping/orders. "
            "Ask about AWS/Terraform/Kubernetes/deployments/monitoring instead."
        )
        _append_summary(state, query, reply)
        _clear_inflight_query(state)
        save_session_state(thread_id, state)
        return reply

    # A build paused for plan approval owns the next reply -- resume it instead
    # of routing the approval answer through intent classification. The safety
    # gate still runs on the reply before it reaches the agent.
    if _build_waiting_for_approval(thread_id):
        domain = check_domain(query)
        if domain.flag == "out_of_domain":
            reply = (
                "This CLI only supports SaaS infrastructure topics (design/build/monitor). "
                "I can’t help with consumer tasks like shopping/orders."
            )
            _append_summary(state, query, reply)
            save_session_state(thread_id, state)
            return reply

        safety = check_safety(query)
        if safety.flag == "block":
            reply = f"Blocked by safety gate: {safety.reasoning}"
            _append_summary(state, query, reply)
            save_session_state(thread_id, state)
            return reply

        agent = get_agent(AgentKind.BUILD)
        agent_config = {"configurable": {"thread_id": _agent_thread_id(thread_id, "build")}}
        build_result = agent.invoke(Command(resume=query), agent_config)
        reply = _interrupt_prompt(build_result) or build_result["messages"][-1].content
        state.last_active_agent = "build"
        _append_summary(state, query, reply)
        save_session_state(thread_id, state)
        return reply

    _mark_inflight_query(thread_id, state, query)

    try:
        result = route(query, state)
        state.pending_routed_intent = result.intent
        save_session_state(thread_id, state)

        if result.intent == "build" and not _build_architecture_exists():
            # Auto-fallback: if the user asked to build but there's no PDR yet,
            # route to Design to generate it (instead of forcing the user to
            # explicitly switch intent).
            logger.info(
                "No pdr.md found at %s; routing build request to design.",
                _pdr_path().as_posix(),
            )
            result.intent = "design"
            result.confidence = 1.0
            result.is_continuation = False
            result.requires_clarification = False
            result.clarification_question = None
            result.reasoning = "No PDR found; routing to design first."
            state.pending_routed_intent = "design"
            save_session_state(thread_id, state)

        if result.safety_flag == "block":
            reply = f"Blocked by safety gate: {result.reasoning}"
            _append_summary(state, query, reply)
            _clear_inflight_query(state)
            save_session_state(thread_id, state)
            return reply

        if result.safety_flag == "needs_review":
            state.pending_review_query = query
            state.pending_review_reasoning = result.reasoning
            reply = (
                f"{result.reasoning}\n\n"
                f"{result.clarification_question or 'Reply YES to proceed.'}"
            )
            _append_summary(state, query, reply)
            save_session_state(thread_id, state)
            return reply

        if result.requires_clarification:
            reply = result.clarification_question or "Could you clarify that request?"
            _append_summary(state, query, reply)
            _clear_inflight_query(state)
            save_session_state(thread_id, state)
            return reply

        if result.intent == "design" and state.awaiting_agent_input:
            awaiting_kind = pending_interrupt_kind(design_thread_id)
            result.directive = _design_directive(awaiting_kind, query)

        logger.info(
            "Orchestrator output before dispatch: %s",
            {
                "thread_id": thread_id,
                "query": query,
                "intent": result.intent,
                "confidence": result.confidence,
                "is_continuation": result.is_continuation,
                "requires_clarification": result.requires_clarification,
                "clarification_question": result.clarification_question,
                "safety_flag": result.safety_flag,
                "reasoning": result.reasoning,
                "directive": result.directive,
                "session_state": {
                    "last_active_agent": state.last_active_agent,
                    "arch_md_exists": state.arch_md_exists,
                    "awaiting_agent_input": state.awaiting_agent_input,
                    "pending_review_query": state.pending_review_query is not None,
                    "pending_user_query": state.pending_user_query,
                    "pending_routed_intent": state.pending_routed_intent,
                },
            },
        )

        reply = _dispatch(query, result, state, thread_id)
        _append_summary(state, query, reply)
        _clear_inflight_query(state)
        save_session_state(thread_id, state)
        return reply
    except Exception as exc:
        state.last_failure = f"{type(exc).__name__}: {exc}"
        save_session_state(thread_id, state)
        raise


def _dispatch(query: str, result: OrchestratorOutput, state: SessionState, thread_id: str) -> str:
    if result.intent is None:
        return result.clarification_question or "I couldn't determine where to route that."

    state.last_active_agent = result.intent

    if result.intent == "general":
        return _handle_general(query, state)

    agent_kind = _to_agent_kind(result.intent)
    if agent_kind is None:
        return "I couldn't determine where to route that."

    agent = get_agent(agent_kind)
    agent_config = {"configurable": {"thread_id": _agent_thread_id(thread_id, result.intent)}}

    if result.intent == "design":
        design_input = {"user_message": query}
        if state.awaiting_agent_input:
            resume_payload: Any = query
            if result.directive is not None:
                resume_payload = {"text": query, "directive": result.directive}
            design_result = agent.invoke(Command(resume=resume_payload), agent_config)
        else:
            design_result = agent.invoke(design_input, agent_config)

        state.awaiting_agent_input = bool(design_result.get("__interrupt__"))
        state.arch_md_exists = _build_architecture_exists()

        prompt = _interrupt_prompt(design_result)
        if prompt is not None:
            return prompt
        return (design_result.get("assistant_output") or "").strip() or "OK"

    response = agent.invoke({"messages": [HumanMessage(content=query)]}, agent_config)
    state.awaiting_agent_input = False
    state.arch_md_exists = _build_architecture_exists()
    return _interrupt_prompt(response) or response["messages"][-1].content


def _agent_thread_id(thread_id: str, intent: Agent) -> str:
    # Keep design isolated because it is an interrupt-driven graph.
    # Build/monitor/general can reuse the main session thread.
    if intent == "design":
        return f"{thread_id}::design"
    return thread_id


# if __name__ == "__main__":
#     # Quick manual smoke test.
#     state = SessionState()

#     r1 = route(
#         "Design and deploy the infra for a rag pipeline for 10K users "
#         "daily limit and latency 2s.",
#         state,
#     )
#     print("Multi-intent query ->", r1)

#     # Simulate design agent taking over.
#     state.last_active_agent = r1.intent
#     state.awaiting_agent_input = True

#     r2 = route("about 500 QPS at peak", state)
#     print("Clarification answer ->", r2)

#     r3 = route("how do I bypass auth on our staging DB", state)
#     print("Unsafe follow-up ->", r3)
