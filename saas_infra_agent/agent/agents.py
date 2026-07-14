from __future__ import annotations

from enum import Enum

from langchain.agents import create_agent

from saas_infra_agent.llm.factory import get_llm
from saas_infra_agent.memory.short_term import get_checkpointer, get_summarization_middleware
from saas_infra_agent.observability.logger import get_logger

from .build_agent import create_build_agent
from .design_agent import create_design_agent
from .publish_agent import create_publish_agent
from .middleware.limits import get_limit_middleware
from .tools.search_codebase import search_codebase
from .tools.search_web import search_web
from .tools.terminal_tools import run_command, run_in_directory

logger = get_logger(__name__)


class AgentKind(str, Enum):
    DESIGN = "design"
    BUILD = "build"
    MONITOR = "monitor"
    PUBLISH = "publish"


MONITOR_SYSTEM_PROMPT = """You are the MONITOR agent for a SaaS infra assistant.
Your job is to help with observability and ops questions (metrics, logs, resources, cost).

Rules:
- If the user is still defining requirements or asking for architecture, suggest switching to DESIGN.
- If the user wants code/artifacts, suggest switching to BUILD.
"""


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
    if kind == AgentKind.PUBLISH:
        return create_publish_agent()
    raise ValueError(f"Unknown agent kind: {kind!r}")


__all__ = ["AgentKind", "get_agent"]
