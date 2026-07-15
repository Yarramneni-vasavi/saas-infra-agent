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
from .tools.long_term_memory import list_long_term_projects, recall_long_term, remember_long_term
from .tools.monitoring import (
    get_recommended_promql_queries,
    get_simulated_service_health,
    get_simulated_service_metrics,
    query_prometheus,
    query_prometheus_range,
    read_architecture_for_monitoring,
)
from .tools.read_project_file import read_project_file
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
Your job is to help with observability and ops questions: service health, metrics,
PromQL, resource usage, latency, errors, token usage, and cost signals.

Rules:
- Always call read_architecture_for_monitoring first. Treat architecture.md as the
  source of truth for expected services and deployment context.
- If architecture.md is missing, stop and ask the user to complete the DESIGN
  agent flow before monitoring.
- Use query_prometheus or query_prometheus_range when the user asks for real
  runtime metrics and Prometheus is available.
- Use get_simulated_service_metrics or get_simulated_service_health when the user
  asks for a demo, sample data, local validation, or Prometheus is unavailable.
- Use get_recommended_promql_queries when the user asks what PromQL should be
  used or how metrics should be generated.
- Clearly label simulated data as simulated. Do not imply it is production data.
- Prefer concise operational summaries: status, evidence, likely cause, next action.
- If the user is still defining requirements or asking for architecture, suggest switching to DESIGN.
- If the user wants code/artifacts, suggest switching to BUILD.
"""


def create_monitor_agent():
    llm = get_llm()
    checkpointer = get_checkpointer()
    middleware = [*get_limit_middleware(), get_summarization_middleware()]
    return create_agent(
        llm,
        tools=[
            read_architecture_for_monitoring,
            get_simulated_service_metrics,
            get_simulated_service_health,
            get_recommended_promql_queries,
            query_prometheus,
            query_prometheus_range,
            remember_long_term,
            recall_long_term,
            list_long_term_projects,
            search_codebase,
            search_web,
        ],
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
