from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from saas_infra_agent.config.config import config
from saas_infra_agent.observability.logger import get_logger

logger = get_logger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _memory_db_path() -> Path:
    memory_cfg = dict(config.get("memory") or {})
    path = str(memory_cfg.get("long_term_db_path") or ".memory/long_term.db").strip()
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


_SCHEMA = """
CREATE TABLE IF NOT EXISTS long_term_memories (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  project       TEXT NOT NULL,
  category      TEXT NOT NULL,
  memory_key    TEXT NOT NULL,
  value_json    TEXT NOT NULL,
  tags_json     TEXT NOT NULL DEFAULT '[]',
  source        TEXT NOT NULL DEFAULT '',
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  pinned        INTEGER NOT NULL DEFAULT 0,
  UNIQUE(project, category, memory_key)
);

CREATE INDEX IF NOT EXISTS idx_ltm_project_updated ON long_term_memories(project, updated_at);
CREATE INDEX IF NOT EXISTS idx_ltm_category ON long_term_memories(category);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_memory_db_path(), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.executescript(_SCHEMA)
    return conn


@dataclass(frozen=True)
class MemoryRecord:
    id: int
    project: str
    category: str
    memory_key: str
    value: Any
    tags: list[str]
    source: str
    created_at: str
    updated_at: str
    pinned: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project": self.project,
            "category": self.category,
            "key": self.memory_key,
            "value": self.value,
            "tags": self.tags,
            "source": self.source,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "pinned": self.pinned,
        }


class SqliteStore:
    """Simple long-term memory store backed by SQLite.

    This is intentionally lightweight: key/value records grouped by project
    and category (facts, preferences, workflows, etc) with basic substring
    search. It is NOT an embedding/vector store.
    """

    def put(
        self,
        *,
        project: str,
        category: str,
        key: str,
        value: Any,
        tags: Iterable[str] | None = None,
        source: str = "",
        pinned: bool = False,
    ) -> MemoryRecord:
        project_n = (project or "").strip() or "default"
        category_n = (category or "").strip().lower() or "fact"
        key_n = (key or "").strip()
        if not key_n:
            raise ValueError("memory key is required")

        now = _utc_now()
        value_json = json.dumps(value, ensure_ascii=False)
        tags_list = sorted({t.strip() for t in (tags or []) if t and str(t).strip()})
        tags_json = json.dumps(tags_list, ensure_ascii=False)

        conn = _connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO long_term_memories
                      (project, category, memory_key, value_json, tags_json, source, created_at, updated_at, pinned)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(project, category, memory_key) DO UPDATE SET
                      value_json=excluded.value_json,
                      tags_json=excluded.tags_json,
                      source=excluded.source,
                      updated_at=excluded.updated_at,
                      pinned=excluded.pinned
                    """,
                    (
                        project_n,
                        category_n,
                        key_n,
                        value_json,
                        tags_json,
                        source or "",
                        now,
                        now,
                        1 if pinned else 0,
                    ),
                )
                row = conn.execute(
                    """
                    SELECT id, project, category, memory_key, value_json, tags_json, source, created_at, updated_at, pinned
                    FROM long_term_memories
                    WHERE project=? AND category=? AND memory_key=?
                    """,
                    (project_n, category_n, key_n),
                ).fetchone()
        finally:
            conn.close()

        assert row is not None
        return _row_to_record(row)

    def get(self, *, project: str, category: str, key: str) -> MemoryRecord | None:
        conn = _connect()
        try:
            row = conn.execute(
                """
                SELECT id, project, category, memory_key, value_json, tags_json, source, created_at, updated_at, pinned
                FROM long_term_memories
                WHERE project=? AND category=? AND memory_key=?
                """,
                ((project or "").strip() or "default", (category or "").strip().lower() or "fact", (key or "").strip()),
            ).fetchone()
        finally:
            conn.close()
        return _row_to_record(row) if row else None

    def search(
        self,
        *,
        project: str | None = None,
        category: str | None = None,
        query: str | None = None,
        limit: int = 10,
    ) -> list[MemoryRecord]:
        limit_n = max(1, min(int(limit or 10), 50))
        where: list[str] = []
        params: list[Any] = []

        if project:
            where.append("project = ?")
            params.append(project.strip())
        if category:
            where.append("category = ?")
            params.append(category.strip().lower())
        if query:
            q = f"%{query.strip().lower()}%"
            where.append(
                "(lower(memory_key) LIKE ? OR lower(value_json) LIKE ? OR lower(tags_json) LIKE ?)"
            )
            params.extend([q, q, q])

        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        sql = f"""
        SELECT id, project, category, memory_key, value_json, tags_json, source, created_at, updated_at, pinned
        FROM long_term_memories
        {where_sql}
        ORDER BY pinned DESC, updated_at DESC
        LIMIT ?
        """
        params.append(limit_n)

        conn = _connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        return [_row_to_record(r) for r in rows]

    def list_projects(self, *, limit: int = 20) -> list[dict[str, Any]]:
        limit_n = max(1, min(int(limit or 20), 100))
        conn = _connect()
        try:
            rows = conn.execute(
                """
                SELECT project, MAX(updated_at) as last_updated, COUNT(*) as item_count
                FROM long_term_memories
                GROUP BY project
                ORDER BY last_updated DESC
                LIMIT ?
                """,
                (limit_n,),
            ).fetchall()
        finally:
            conn.close()
        return [{"project": p, "last_updated": u, "item_count": c} for (p, u, c) in rows]


def _row_to_record(row: sqlite3.Row | tuple | None) -> MemoryRecord:
    (id_, project, category, key, value_json, tags_json, source, created_at, updated_at, pinned) = row  # type: ignore[misc]
    try:
        value = json.loads(value_json) if isinstance(value_json, str) else value_json
    except Exception:
        value = value_json
    try:
        tags = json.loads(tags_json) if isinstance(tags_json, str) else []
        if not isinstance(tags, list):
            tags = []
        tags = [str(t) for t in tags]
    except Exception:
        tags = []
    return MemoryRecord(
        id=int(id_),
        project=str(project),
        category=str(category),
        memory_key=str(key),
        value=value,
        tags=tags,
        source=str(source or ""),
        created_at=str(created_at),
        updated_at=str(updated_at),
        pinned=bool(int(pinned)),
    )


_STORE: SqliteStore | None = None


def get_long_term_store() -> SqliteStore:
    global _STORE
    if _STORE is None:
        _STORE = SqliteStore()
        logger.info("Initialized long-term memory store at %s", _memory_db_path())
    return _STORE


__all__ = ["MemoryRecord", "SqliteStore", "get_long_term_store"]
