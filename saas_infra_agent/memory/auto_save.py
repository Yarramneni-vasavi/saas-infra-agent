from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from saas_infra_agent.config.config import config
from saas_infra_agent.llm.factory import get_small_llm
from saas_infra_agent.memory.long_term import MemoryRecord, get_long_term_store
from saas_infra_agent.observability.logger import get_logger

logger = get_logger(__name__)


_DEFAULT_PROJECT = "default"

_SKIP_USER_MESSAGES = {
    "approve",
    "approved",
    "continue",
    "proceed",
    "ok",
    "okay",
    "yes",
    "y",
    "no",
    "n",
    "/exit",
    "/new",
    "/session",
    "/list_long_term",
}

# Very conservative: if the turn includes anything that looks like a secret,
# do not persist it.
_SECRET_PATTERNS = [
    r"\bsk-[A-Za-z0-9]{10,}\b",
    r"\bAKIA[0-9A-Z]{16}\b",
    r"\bASIA[0-9A-Z]{16}\b",
    r"\baws_secret_access_key\b",
    r"\bopenai_api_key\b",
    r"\bpassword\b",
    r"\bsecret\b",
    r"\btoken\b",
    r"\bprivate[_\s-]?key\b",
]
_SECRET_RX = re.compile("|".join(_SECRET_PATTERNS), re.IGNORECASE)

_TRIGGER_HINTS = [
    "prefer",
    "preference",
    "always",
    "never",
    "from now on",
    "for this project",
    "in this project",
    "we use",
    "use floci",
    "emulator",
    "localstack",
    "terraform",
    "kubernetes",
]


@dataclass(frozen=True)
class ExtractedMemory:
    category: str
    key: str
    value: Any
    tags: list[str]
    pinned: bool = False


def _auto_save_enabled() -> bool:
    memory_cfg = dict(config.get("memory") or {})
    return bool(memory_cfg.get("long_term_auto_save", True))


def _default_project_name() -> str:
    memory_cfg = dict(config.get("memory") or {})
    name = str(memory_cfg.get("long_term_default_project") or "").strip()
    if name:
        return name
    # Fall back to current workspace dir name so different checkouts don't collide.
    return Path.cwd().name or _DEFAULT_PROJECT


def _should_consider(user_text: str, assistant_text: str) -> bool:
    t = (user_text or "").strip().lower()
    if not t:
        return False
    if t in _SKIP_USER_MESSAGES:
        return False
    if t.startswith("/"):
        return False
    blob = f"{user_text}\n{assistant_text}"
    if _SECRET_RX.search(blob or ""):
        return False
    lowered = (blob or "").lower()
    return any(h in lowered for h in _TRIGGER_HINTS)


def _extract_memories_via_llm(user_text: str, assistant_text: str) -> list[ExtractedMemory]:
    system = (
        "You extract LONG-TERM memory from an infrastructure CLI chat.\n"
        "Only output durable, reusable information:\n"
        "- preferences (e.g. 'use Floci emulator by default')\n"
        "- facts about the user's environment/workflow (non-sensitive)\n"
        "- project conventions or decisions (e.g. 'pin terraform module versions')\n"
        "Do NOT store secrets or credentials. Do NOT store one-off troubleshooting details.\n\n"
        "Return ONLY compact JSON, no prose:\n"
        "{\n"
        '  "memories": [\n'
        '    {"category":"preference|fact|workflow|project","key":"...",'
        ' "value": <string|number|object|array>, "tags":["..."], "pinned": true|false}\n'
        "  ]\n"
        "}\n"
        "Rules:\n"
        "- At most 3 memories.\n"
        "- Keys should be short snake_case.\n"
        "- If nothing is worth saving, return {\"memories\": []}.\n"
    )
    user = (
        "Latest user message:\n"
        f"{user_text}\n\n"
        "Assistant reply:\n"
        f"{assistant_text}\n"
    )

    try:
        llm = get_small_llm()
        resp = llm.invoke([SystemMessage(content=system), HumanMessage(content=user)])
        text = getattr(resp, "content", "")
        if not isinstance(text, str):
            text = str(text)
    except Exception as exc:
        # Missing credentials / offline environments should not break the CLI.
        logger.info("Long-term memory distillation skipped (LLM unavailable): %s", exc)
        return _extract_memories_heuristic(user_text, assistant_text)

    try:
        data = json.loads(_coerce_json(text))
        memories = data.get("memories", [])
        if not isinstance(memories, list):
            return []
        extracted: list[ExtractedMemory] = []
        for item in memories[:3]:
            if not isinstance(item, dict):
                continue
            category = str(item.get("category") or "").strip().lower()
            key = str(item.get("key") or "").strip()
            if not key or category not in {"preference", "fact", "workflow", "project"}:
                continue
            value = item.get("value")
            tags = item.get("tags") or []
            if not isinstance(tags, list):
                tags = []
            tags = [str(t).strip() for t in tags if str(t).strip()]
            pinned = bool(item.get("pinned", False))
            extracted.append(ExtractedMemory(category=category, key=_snakeish(key), value=value, tags=tags, pinned=pinned))
        return extracted
    except Exception:
        logger.exception("Failed to parse long-term memory distillation JSON.")
        return []


def _coerce_json(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return t
    start = t.find("{")
    end = t.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return t
    return t[start : end + 1]


def _snakeish(key: str) -> str:
    k = re.sub(r"[^a-zA-Z0-9]+", "_", (key or "").strip()).strip("_").lower()
    return k[:80] if k else ""


def _extract_memories_heuristic(user_text: str, assistant_text: str) -> list[ExtractedMemory]:
    """Fallback extractor when the LLM isn't available.

    Keep this intentionally conservative; only capture strong explicit
    preferences/decisions.
    """
    blob = f"{user_text}\n{assistant_text}".lower()
    out: list[ExtractedMemory] = []

    if "floci" in blob and ("always" in blob or "by default" in blob or "emulator" in blob):
        out.append(
            ExtractedMemory(
                category="preference",
                key="terraform_target",
                value="floci_emulator",
                tags=["terraform", "floci", "emulator"],
                pinned=True,
            )
        )

    if "pin" in blob and "terraform" in blob and "version" in blob:
        out.append(
            ExtractedMemory(
                category="workflow",
                key="terraform_pin_versions",
                value=True,
                tags=["terraform"],
                pinned=False,
            )
        )

    return out[:3]


def auto_save_long_term(
    *,
    user_text: str,
    assistant_text: str,
    project: str | None = None,
) -> list[MemoryRecord]:
    """Maybe persist long-term memories extracted from a single turn.

    Returns the records that were stored (possibly empty).
    """
    if not _auto_save_enabled():
        return []

    if not _should_consider(user_text, assistant_text):
        return []

    extracted = _extract_memories_via_llm(user_text, assistant_text)
    if not extracted:
        return []

    store = get_long_term_store()
    project_name = (project or "").strip() or _default_project_name()
    saved: list[MemoryRecord] = []
    for m in extracted:
        # Extra safety: never persist if the extracted value itself contains secret-like text.
        try:
            rendered = json.dumps(m.value, ensure_ascii=False)
        except Exception:
            rendered = str(m.value)
        if _SECRET_RX.search(rendered):
            continue

        rec = store.put(
            project=project_name,
            category=m.category,
            key=m.key,
            value=m.value,
            tags=m.tags,
            pinned=m.pinned,
            source="auto",
        )
        saved.append(rec)

    if saved:
        logger.info("Auto-saved %d long-term memories for project=%r", len(saved), project_name)
    return saved


__all__ = ["auto_save_long_term"]
