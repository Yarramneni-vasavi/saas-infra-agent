from __future__ import annotations

from pathlib import Path

from saas_infra_agent.agent.agents import AgentKind, get_agent
from saas_infra_agent.llm.factory import get_small_llm
from saas_infra_agent.observability.logger import get_logger
from saas_infra_agent.memory.short_term import get_checkpointer, get_session_history
from langchain_core.messages import HumanMessage
from langgraph.types import Command

logger = get_logger(__name__)

ROUTER_SYSTEM_PROMPT = """You are a router for a SaaS Infrastructure Agent.

You have 3 agents available:
- design   : collects requirements, clarifies scope, recommends infra stack, updates design decisions
- build  : generates infra artifacts (Terraform, Docker, K8s) — only when requirements are clear
- monitor: shows metrics, token usage, cost analysis, optimization recommendations

ROUTING RULES:

Route to DESIGN when:
- User describes a new project or app idea (e.g. "create a todo app", "we need a RAG pipeline")
- User adds or changes requirements (e.g. "also add a caching layer", "support 50k users instead")
- User asks about architecture, stack choices, cost estimates, or trade-offs
- There is no architecture.md yet OR requirements are incomplete

Route to BUILD when:
- User explicitly says "build it", "generate the code", "start building", "proceed"
- architecture.md exists AND requirements are already fully discussed
- User asks to regenerate or update existing infra artifacts

Route to MONITOR when:
- User asks about system health, metrics, CPU, memory, GPU
- User asks about token usage, API costs, LLM costs, burn rate
- User asks for optimization recommendations

IMPORTANT:
- "Create X" or "Build X" with no prior context → DESIGN (requirements not collected yet)
- Only route to BUILD after requirements have been gathered in architecture.md
- When in doubt between plan and build → always choose PLAN

Return ONLY one word: design OR build OR monitor.
"""

def _architecture_doc_exists() -> bool:
    return (Path.cwd() / "architecture.md").exists() or (Path.cwd() / "arch.md").exists()


def _design_thread_id(thread_id: str) -> str:
    return f"{thread_id}::design"


def _design_waiting_for_user(design_thread_id: str) -> bool:
    checkpoint = get_checkpointer().get({"configurable": {"thread_id": design_thread_id}})
    if not checkpoint:
        return False
    channel_values = checkpoint.get("channel_values") or {}
    return bool(channel_values.get("awaiting_kind"))


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


def _interrupt_prompt(result: dict) -> str | None:
    """Extract the human-facing prompt when an agent run paused on an interrupt."""
    interrupts = result.get("__interrupt__") or []
    if not interrupts:
        return None
    payload = interrupts[0].value
    if isinstance(payload, dict) and isinstance(payload.get("prompt"), str):
        return payload["prompt"]
    return str(payload)


def _is_build_intent(text: str) -> bool:
    q = text.lower()
    return any(
        m in q
        for m in (
            "go ahead and build",
            "go ahead, build",
            "build this",
            "build",
            "start building",
            "implement",
            "proceed",
            "generate the code",
        )
    )


def _strip_mode_prefix(question: str) -> tuple[str, AgentKind | None]:
    q = question.strip()
    if q.lower().startswith("/design"):
        return q[len("/design") :].lstrip(), AgentKind.DESIGN
    if q.lower().startswith("/build"):
        return q[len("/build") :].lstrip(), AgentKind.BUILD
    if q.lower().startswith("/monitor"):
        return q[len("/monitor") :].lstrip(), AgentKind.MONITOR
    return question, None


def _keyword_route(text: str) -> AgentKind:
    q = text.lower()
    design_markers = ("requirement", "requirements", "scope", "clarif", "features", "plan", "roadmap", "design", "architecture")
    build_markers = ("build", "docker", "dockerfile", "compose", "terraform", "kubernetes", "helm", "cicd", "pipeline", "generate", "scaffold", "create", "implement")
    monitor_markers = ("monitor", "metrics", "logs", "traces", "cpu", "memory", "gpu", "token", "cost", "burn rate")

    if any(m in q for m in monitor_markers):
        return AgentKind.MONITOR
    if any(m in q for m in build_markers):
        return AgentKind.BUILD if _architecture_doc_exists() else AgentKind.DESIGN
    if any(m in q for m in design_markers):
        return AgentKind.DESIGN
    return AgentKind.DESIGN


def select_agent_kind(question: str, thread_id: str) -> AgentKind:
    stripped, explicit = _strip_mode_prefix(question)
    if explicit is not None:
        return explicit

    # If the user is explicitly asking to build, only allow BUILD when a design handoff doc exists.
    if _is_build_intent(stripped):
        return AgentKind.BUILD if _architecture_doc_exists() else AgentKind.DESIGN

    # Try LLM routing first; fall back to keyword routing on errors.
    try:
        history = get_session_history(thread_id)
        router_llm = get_small_llm()
        router_input = {
            "role": "user",
            "content": (
                "Conversation so far:\n"
                f"{history[-10:]}\n\n"
                "New user message:\n"
                f"{stripped}\n"
            ),
        }
        result = router_llm.invoke(
            [
                {"role": "system", "content": ROUTER_SYSTEM_PROMPT},
                router_input,
            ]
        )
        content = getattr(result, "content", str(result)).strip().lower()
        if content.startswith("build"):
            return AgentKind.BUILD
        if content.startswith("design"):
            return AgentKind.DESIGN
        if content.startswith("monitor"):
            return AgentKind.MONITOR
    except Exception as exc:
        logger.warning(f"LLM routing failed, falling back to keywords: {exc}")

    return _keyword_route(stripped)


def handle_query(question: str, thread_id: str) -> str:
    """Entry point for user queries - routes to design/build agent and runs it."""
    stripped, explicit = _strip_mode_prefix(question)

    # A build paused for plan approval owns the next reply — resume it instead
    # of routing, unless the user explicitly switches to another agent.
    if explicit in (None, AgentKind.BUILD) and _build_waiting_for_approval(thread_id):
        agent = get_agent(AgentKind.BUILD)
        agent_config = {"configurable": {"thread_id": thread_id}}
        result = agent.invoke(Command(resume=stripped), agent_config)
        return _interrupt_prompt(result) or result["messages"][-1].content

    agent_kind = explicit or select_agent_kind(stripped, thread_id)

    logger.info(f"Handling query agent_kind={agent_kind.value} question={stripped!r}")
    agent = get_agent(agent_kind)

    if agent_kind == AgentKind.DESIGN:
        design_tid = _design_thread_id(thread_id)
        agent_config = {"configurable": {"thread_id": design_tid}}
        if _design_waiting_for_user(design_tid):
            result = agent.invoke(Command(resume=stripped), agent_config)
        else:
            result = agent.invoke({"user_message": stripped}, agent_config)

        prompt = _interrupt_prompt(result)
        if prompt is not None:
            return prompt

        return (result.get("assistant_output") or "").strip() or "OK"

    agent_config = {"configurable": {"thread_id": thread_id}}
    response = agent.invoke({"messages": [HumanMessage(content=stripped)]}, agent_config)
    return _interrupt_prompt(response) or response["messages"][-1].content
