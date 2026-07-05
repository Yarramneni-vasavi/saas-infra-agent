from __future__ import annotations

from enum import Enum

from langchain.agents import create_agent

from saas_infra_agent.llm.factory import get_llm
from saas_infra_agent.memory.short_term import get_checkpointer, get_summarization_middleware
from saas_infra_agent.observability.logger import get_logger

from .design_agent import create_design_agent
from .middleware.limits import get_limit_middleware
from .tools.read_project_file import read_project_file
from .tools.search_codebase import search_codebase
from .tools.search_web import search_web
from .tools.write_artifact import write_artifact

logger = get_logger(__name__)


class AgentKind(str, Enum):
    DESIGN = "design"
    BUILD = "build"
    MONITOR = "monitor"


BUILD_SYSTEM_PROMPT = """You are the BUILD agent for a SaaS infra assistant.
Your job is to generate infrastructure/code artifacts based on the user's requirements.

Rules:
- If `architecture.md` exists in the project root, read it first using read_project_file and treat it as source-of-truth requirements.
- First, extract the deployment target (docker | terraform | k8s) from the conversation.
- If requirements or deployment target are missing/contradictory, ask for clarification and suggest switching to the DESIGN agent.
- When creating files, use the write_artifact tool (avoid pasting huge files inline).
- Prefer generating a minimal runnable scaffold first, then optional enhancements.
- Summarize what you generated and where it was written.
"""


MONITOR_SYSTEM_PROMPT = """You are the MONITOR agent for a SaaS infra assistant.
Your job is to help with observability and ops questions (metrics, logs, resources, cost).

Rules:
- If the user is still defining requirements or asking for architecture, suggest switching to DESIGN.
- If the user wants code/artifacts, suggest switching to BUILD.
"""


def create_build_agent():
    llm = get_llm()
    checkpointer = get_checkpointer()
    middleware = [*get_limit_middleware(), get_summarization_middleware()]
    return create_agent(
        llm,
        tools=[search_codebase, read_project_file, write_artifact],
        system_prompt=BUILD_SYSTEM_PROMPT,
        checkpointer=checkpointer,
        middleware=middleware,
    )


def create_monitor_agent():
    llm = get_llm()
    checkpointer = get_checkpointer()
    middleware = [*get_limit_middleware(), get_summarization_middleware()]
    return create_agent(
        llm,
        tools=[search_codebase, search_web],
        system_prompt=MONITOR_SYSTEM_PROMPT,
        checkpointer=checkpointer,
        middleware=middleware,
    )


def get_agent(kind: AgentKind):
    logger.info(f"Creating agent kind={kind.value}")
    if kind == AgentKind.DESIGN:
        return create_design_agent()
    if kind == AgentKind.BUILD:
        return create_build_agent()
    if kind == AgentKind.MONITOR:
        return create_monitor_agent()
    raise ValueError(f"Unknown agent kind: {kind!r}")


__all__ = ["AgentKind", "get_agent"]
