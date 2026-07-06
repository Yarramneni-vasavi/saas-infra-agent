from __future__ import annotations

from langgraph.types import Command

from saas_infra_agent.memory.short_term import get_checkpointer

from .design_flow import create_design_workflow_graph


def create_design_agent():
    """Create the DESIGN agent as an interrupt-driven LangGraph workflow.

    This enforces step-by-step interaction:
    clarify -> requirements confirm -> architecture feedback -> approve -> save.
    """
    return create_design_workflow_graph(get_checkpointer())


__all__ = ["create_design_agent", "Command"]
