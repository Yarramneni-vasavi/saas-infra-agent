from __future__ import annotations

"""Compatibility wrapper (deprecated).

Prefer importing from `saas_infra_agent.agent.agents`.
"""

from .agents import AgentKind, get_agent


__all__ = ["AgentKind", "get_agent"]
