"""Load MCP tools as LangChain tools usable from the sync agent pipeline.

langchain-mcp-adapters produces async-only tools, but the CLI drives agents
with sync .invoke(). Each loaded tool therefore gets a sync func that runs its
coroutine on a private event loop. Adapter tools open a fresh MCP session per
call, so nothing is lost by running each call on its own loop.

Loading is cached per server set: listing tools requires connecting to the
server, and agents are recreated on every query.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from functools import lru_cache
from typing import Awaitable, TypeVar

from langchain_core.tools import BaseTool, StructuredTool, ToolException
from langchain_mcp_adapters.client import MultiServerMCPClient

from saas_infra_agent.observability.logger import get_logger

from .servers import allowed_tools, get_connections

logger = get_logger(__name__)

T = TypeVar("T")


def _run_coro(coro: Awaitable[T]) -> T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # Called from inside a running loop (e.g. an async server later on):
    # run on a fresh loop in a worker thread instead of blocking this one.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _with_sync_invoke(tool: StructuredTool) -> StructuredTool:
    # Server-side failures (bad token, missing permission, 404...) must come
    # back to the model as the tool's result so it can react — raised as-is
    # they abort the whole agent run.
    def _func(**kwargs):
        try:
            return _run_coro(tool.coroutine(**kwargs))
        except Exception as exc:  # noqa: BLE001
            raise ToolException(f"{tool.name} failed: {exc}") from exc

    async def _coro(**kwargs):
        try:
            return await tool.coroutine(**kwargs)
        except Exception as exc:  # noqa: BLE001
            raise ToolException(f"{tool.name} failed: {exc}") from exc

    return StructuredTool(
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
        func=_func,
        coroutine=_coro,
        response_format=tool.response_format,
        metadata=tool.metadata,
        handle_tool_error=True,
    )


@lru_cache(maxsize=None)
def get_mcp_tools(*server_names: str) -> tuple[BaseTool, ...]:
    """Tools from the named MCP servers, sync-invokable and allowlist-filtered.

    Returns an empty tuple (with a warning) when no server is configured or a
    server cannot be reached, so agents degrade gracefully instead of failing.
    """
    connections = get_connections(server_names)
    if not connections:
        return ()

    client = MultiServerMCPClient(connections)
    tools: list[BaseTool] = []
    for name in connections:
        try:
            raw_tools = _run_coro(client.get_tools(server_name=name))
        except Exception:
            logger.exception("Failed to load MCP tools from %r; continuing without them.", name)
            continue
        allow = set(allowed_tools(name))
        server_tools = [
            _with_sync_invoke(t) for t in raw_tools if not allow or t.name in allow
        ]
        logger.info("Loaded %d MCP tools from %r: %s",
                    len(server_tools), name, [t.name for t in server_tools])
        tools.extend(server_tools)
    return tuple(tools)


__all__ = ["get_mcp_tools"]
