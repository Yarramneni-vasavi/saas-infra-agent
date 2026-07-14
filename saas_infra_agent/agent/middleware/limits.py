from __future__ import annotations

from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware

from saas_infra_agent.config.config import config


def get_limit_middleware(agent_cfg: dict | None = None):
    """Return middleware that limits model and tool calls.

    Config keys (under `agent:` in `config.yaml`):
    - model_call_run_limit: int | null
    - model_call_thread_limit: int | null
    - model_call_exit_behavior: "end" | "error"
    - tool_call_run_limit: int | null
    - tool_call_thread_limit: int | null
    - tool_call_exit_behavior: "continue" | "end" | "error"

    Pass `agent_cfg` to override the global `agent:` section (e.g. the build
    agent merges `agent.build:` on top for higher long-run limits).
    """
    if agent_cfg is None:
        agent_cfg = (config.get("agent") or {}) if isinstance(config, dict) else {}

    model_run_limit = agent_cfg.get("model_call_run_limit")
    model_thread_limit = agent_cfg.get("model_call_thread_limit")
    model_exit_behavior = agent_cfg.get("model_call_exit_behavior", "end")

    tool_run_limit = agent_cfg.get("tool_call_run_limit")
    tool_thread_limit = agent_cfg.get("tool_call_thread_limit")
    tool_exit_behavior = agent_cfg.get("tool_call_exit_behavior", "continue")

    middleware = []

    if model_run_limit is not None or model_thread_limit is not None:
        middleware.append(
            ModelCallLimitMiddleware(
                run_limit=model_run_limit,
                thread_limit=model_thread_limit,
                exit_behavior=model_exit_behavior,
            )
        )

    if tool_run_limit is not None or tool_thread_limit is not None:
        middleware.append(
            ToolCallLimitMiddleware(
                run_limit=tool_run_limit,
                thread_limit=tool_thread_limit,
                exit_behavior=tool_exit_behavior,
            )
        )

    return middleware

