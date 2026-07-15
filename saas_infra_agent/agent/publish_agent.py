from __future__ import annotations

from langchain.agents import create_agent

from saas_infra_agent.llm.factory import get_small_llm
from saas_infra_agent.mcp import get_mcp_tools
from saas_infra_agent.memory.short_term import get_checkpointer, get_summarization_middleware

from .middleware.limits import get_limit_middleware
from .tools.list_project_files import list_project_files
from .tools.read_project_file import read_project_file


PUBLISH_SYSTEM_PROMPT = """You are the PUBLISH agent for a SaaS infra assistant.

Your ONLY job is to publish already-generated artifacts to GitHub using the
GitHub MCP tools (create_repository, create_branch, push_files, create_pull_request, ...).

Rules:
- Do NOT generate or modify infrastructure artifacts. Do NOT run terraform.
- Read the generated files from the workspace using list_project_files + read_project_file.
- Ask for missing info (owner/repo name, branch name, default visibility) if needed.
- Prefer a feature branch and a PR; do not push to the default branch unless explicitly asked.
- Keep commits small and focused: all generated artifacts in ONE commit.
"""


def create_publish_agent():
    llm = get_small_llm()
    checkpointer = get_checkpointer()
    middleware = [*get_limit_middleware(), get_summarization_middleware()]
    return create_agent(
        llm,
        tools=[list_project_files, read_project_file, *get_mcp_tools("github")],
        system_prompt=PUBLISH_SYSTEM_PROMPT,
        checkpointer=checkpointer,
        middleware=middleware,
    )


__all__ = ["create_publish_agent", "PUBLISH_SYSTEM_PROMPT"]

