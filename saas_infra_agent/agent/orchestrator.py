from __future__ import annotations

from saas_infra_agent.agent.factory import AgentKind, build_agent
from saas_infra_agent.llm.factory import get_small_llm
from saas_infra_agent.observability.logger import get_logger
from saas_infra_agent.memory.short_term import get_session_history

logger = get_logger(__name__)

ROUTER_SYSTEM_PROMPT = """You are a router for a SaaS Infrastructure Agent.

You have 3 agents available:
- plan   : collects requirements, clarifies scope, recommends infra stack, updates design decisions
- build  : generates infra artifacts (Terraform, Docker, K8s) — only when requirements are clear
- monitor: shows metrics, token usage, cost analysis, optimization recommendations

ROUTING RULES:

Route to PLAN when:
- User describes a new project or app idea (e.g. "create a todo app", "we need a RAG pipeline")
- User adds or changes requirements (e.g. "also add a caching layer", "support 50k users instead")
- User asks about architecture, stack choices, cost estimates, or trade-offs
- There is no arch.md yet OR requirements are incomplete

Route to BUILD when:
- User explicitly says "build it", "generate the code", "start building", "proceed"
- arch.md exists AND requirements are already fully discussed
- User asks to regenerate or update existing infra artifacts

Route to MONITOR when:
- User asks about system health, metrics, CPU, memory, GPU
- User asks about token usage, API costs, LLM costs, burn rate
- User asks for optimization recommendations

IMPORTANT:
- "Create X" or "Build X" with no prior context → PLAN (requirements not collected yet)
- Only route to BUILD after requirements have been gathered in arch.md
- When in doubt between plan and build → always choose PLAN

Return ONLY one word: plan OR build OR monitor.
"""


def _strip_mode_prefix(question: str) -> tuple[str, AgentKind | None]:
    q = question.strip()
    if q.lower().startswith("/plan"):
        return q[len("/plan") :].lstrip(), AgentKind.PLAN
    if q.lower().startswith("/build"):
        return q[len("/build") :].lstrip(), AgentKind.BUILD
    return question, None


def _keyword_route(text: str) -> AgentKind:
    q = text.lower()
    plan_markers = ("requirement", "requirements", "scope", "clarif", "features", "plan", "roadmap")
    build_markers = ("docker", "dockerfile", "compose", "terraform", "kubernetes", "helm", "cicd", "pipeline", "generate", "scaffold", "create")

    if any(m in q for m in build_markers):
        return AgentKind.BUILD
    if any(m in q for m in plan_markers):
        return AgentKind.PLAN
    return AgentKind.PLAN


def select_agent_kind(question: str, thread_id: str) -> AgentKind:
    stripped, explicit = _strip_mode_prefix(question)
    if explicit is not None:
        return explicit

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
        if content.startswith("plan"):
            return AgentKind.PLAN
    except Exception as exc:
        logger.warning(f"LLM routing failed, falling back to keywords: {exc}")

    return _keyword_route(stripped)


def handle_query(question: str, thread_id: str) -> str:
    """Entry point for user queries - routes to plan/build agent and runs it."""
    stripped, explicit = _strip_mode_prefix(question)
    agent_kind = explicit or select_agent_kind(stripped, thread_id)

    logger.info(f"Handling query agent_kind={agent_kind.value} question={stripped!r}")
    agent = build_agent(agent_kind)
    agent_config = {"configurable": {"thread_id": thread_id}}
    response = agent.invoke({"messages": [{"role": "user", "content": stripped}]}, agent_config)

    return response["messages"][-1].content
