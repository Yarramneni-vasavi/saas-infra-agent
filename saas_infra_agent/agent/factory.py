from __future__ import annotations

from enum import Enum

from langchain.agents import create_agent

from saas_infra_agent.llm.factory import get_llm
from saas_infra_agent.observability.logger import get_logger
from saas_infra_agent.memory.short_term import get_checkpointer, get_summarization_middleware
from .tools.search_codebase import search_codebase
from .tools.search_web import search_web
from .tools.write_artifact import write_artifact

logger = get_logger(__name__)

class AgentKind(str, Enum):
    PLAN = "plan"
    BUILD = "build"


PLAN_SYSTEM_PROMPT = """You are the PLAN agent for a SaaS infra assistant.
Your job is to gather requirements and clarify ambiguities before anything is generated.

Rules:
- Ask concise, high-signal clarifying questions when requirements are missing.
- Always determine the deployment target: docker | terraform | k8s (ask if unclear).
- Produce a structured "Requirements" section (bullets) and a "Plan" section (steps).
- Include a "Deployment" section with one of: docker | terraform | k8s.
- Do not generate files or code artifacts. Do not use write_artifact.
- If the user has provided enough detail to start implementation, end with: READY_FOR_BUILD.
"""

BUILD_SYSTEM_PROMPT = """You are the BUILD agent for a SaaS infra assistant.
Your job is to generate infrastructure/code artifacts based on the user's requirements.

Rules:
- First, extract the deployment target (docker | terraform | k8s) from the conversation.
- If requirements or deployment target are missing/contradictory, ask for clarification and suggest switching to the PLAN agent.
- When creating files, use the write_artifact tool (avoid pasting huge files inline).
- Prefer generating a minimal runnable scaffold first, then optional enhancements.
- Summarize what you generated and where it was written.
"""

def build_agent(kind: AgentKind = AgentKind.BUILD):
    """Create and return a LangChain agent (plan/build)."""
    llm = get_llm()
    checkpointer = get_checkpointer()
    middleware = get_summarization_middleware()

    if kind == AgentKind.PLAN:
        system_prompt = PLAN_SYSTEM_PROMPT
        tools = [search_codebase, search_web]
    elif kind == AgentKind.BUILD:
        system_prompt = BUILD_SYSTEM_PROMPT
        tools = [search_codebase, write_artifact]
    else:
        raise ValueError(f"Unknown agent kind: {kind!r}")

    logger.info(f"Creating agent kind={kind.value}")
    return create_agent(
        llm,
        tools=tools,
        system_prompt=system_prompt,
        checkpointer=checkpointer,
        middleware=[middleware],
    )

