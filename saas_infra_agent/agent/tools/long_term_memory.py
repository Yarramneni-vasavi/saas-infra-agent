from __future__ import annotations

import json
from typing import Any

from langchain.tools import tool

from saas_infra_agent.memory.long_term import get_long_term_store
from saas_infra_agent.observability.logger import get_logger

logger = get_logger(__name__)


def _json(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=True)


@tool
def remember_long_term(
    project: str,
    category: str,
    key: str,
    value: str,
    tags: str = "",
    pinned: bool = False,
    source: str = "",
) -> str:
    """Store long-term memory (facts/preferences/workflows) for a project.

    Args:
      project: Project identifier/name (e.g. "coding-platform").
      category: "fact" | "preference" | "workflow" | "project" | etc.
      key: Short lookup key (e.g. "terraform_target", "cloud_provider").
      value: Value to store (string; use JSON text if you need structure).
      tags: Comma-separated tags (optional).
      pinned: If true, keep this memory at top of search results.
      source: Where this came from (optional, e.g. "user", "pdr.md").
    """
    logger.info("Tool called: remember_long_term project=%r category=%r key=%r", project, category, key)
    store = get_long_term_store()
    tags_list = [t.strip() for t in (tags or "").split(",") if t.strip()]
    try:
        value_obj: Any = json.loads(value)
    except Exception:
        value_obj = value
    rec = store.put(
        project=project,
        category=category,
        key=key,
        value=value_obj,
        tags=tags_list,
        pinned=bool(pinned),
        source=source or "user",
    )
    return _json({"ok": True, "record": rec.to_dict()})


@tool
def recall_long_term(
    query: str,
    project: str = "",
    category: str = "",
    limit: int = 10,
) -> str:
    """Search long-term memory by substring match over key/value/tags."""
    logger.info("Tool called: recall_long_term query=%r project=%r category=%r", query, project, category)
    store = get_long_term_store()
    results = store.search(
        query=query,
        project=project.strip() or None,
        category=category.strip() or None,
        limit=limit,
    )
    return _json({"ok": True, "results": [r.to_dict() for r in results]})


@tool
def list_long_term_projects(limit: int = 20) -> str:
    """List projects with long-term memories stored."""
    logger.info("Tool called: list_long_term_projects limit=%r", limit)
    store = get_long_term_store()
    return _json({"ok": True, "projects": store.list_projects(limit=limit)})


__all__ = ["remember_long_term", "recall_long_term", "list_long_term_projects"]
