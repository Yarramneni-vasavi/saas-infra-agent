"""MCP server registry: connection configs for langchain-mcp-adapters.

Each entry maps a server name to a builder that returns a connection dict
(or None when the server is not configured, e.g. a missing token). Server
settings live under the `mcp:` section of config.yaml; secrets come from env.
"""

from __future__ import annotations

import os
from typing import Callable, Optional

from saas_infra_agent.config.config import config
from saas_infra_agent.observability.logger import get_logger

logger = get_logger(__name__)

GITHUB_HOSTED_URL = "https://api.githubcopilot.com/mcp/"
GITHUB_DOCKER_IMAGE = "ghcr.io/github/github-mcp-server"


def _server_cfg(name: str) -> dict:
    return dict((config.get("mcp") or {}).get(name) or {})


def _github_connection() -> Optional[dict]:
    """Official GitHub MCP server.

    transport `http` (default) uses GitHub's hosted server — nothing to
    install; `stdio` runs the same server locally in Docker. Both auth with
    GITHUB_PERSONAL_ACCESS_TOKEN (GITHUB_TOKEN also accepted).
    """
    token = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        logger.warning(
            "GitHub MCP server skipped: set GITHUB_PERSONAL_ACCESS_TOKEN in .env to enable it."
        )
        return None

    cfg = _server_cfg("github")
    if cfg.get("transport") == "stdio":
        return {
            "transport": "stdio",
            "command": "docker",
            "args": [
                "run", "-i", "--rm",
                "-e", "GITHUB_PERSONAL_ACCESS_TOKEN",
                cfg.get("docker_image", GITHUB_DOCKER_IMAGE),
            ],
            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": token},
        }
    return {
        "transport": "streamable_http",
        "url": cfg.get("url", GITHUB_HOSTED_URL),
        "headers": {"Authorization": f"Bearer {token}"},
    }


_SERVERS: dict[str, Callable[[], Optional[dict]]] = {
    "github": _github_connection,
}


def get_connections(names: tuple[str, ...]) -> dict[str, dict]:
    """Connection configs for the named servers, skipping unconfigured ones."""
    connections: dict[str, dict] = {}
    for name in names:
        builder = _SERVERS.get(name)
        if builder is None:
            logger.warning("Unknown MCP server %r; known: %s", name, sorted(_SERVERS))
            continue
        conn = builder()
        if conn is not None:
            connections[name] = conn
    return connections


def allowed_tools(name: str) -> list[str]:
    """Optional per-server tool allowlist from config.yaml (empty = all tools)."""
    return list(_server_cfg(name).get("allowed_tools") or [])


__all__ = ["get_connections", "allowed_tools"]
