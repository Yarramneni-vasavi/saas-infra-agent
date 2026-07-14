from __future__ import annotations

import json
from pathlib import Path

from langchain.tools import tool

from saas_infra_agent.observability.logger import get_logger

logger = get_logger(__name__)


def _safe_relpath(path: str) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        raise ValueError("path must be relative (no drive letter / leading slash)")
    if ".." in candidate.parts:
        raise ValueError("path must not contain '..'")
    return candidate


@tool
def list_project_files(path: str = "infra", max_files: int = 200) -> str:
    """List files under a relative directory (project CWD), bounded.

    Returns JSON: { "root": "...", "files": ["a/b.tf", ...] }
    """
    logger.info(f"Tool called: list_project_files path={path!r} max_files={max_files!r}")
    relpath = _safe_relpath(path)
    root = (Path.cwd() / relpath).resolve()
    cwd = Path.cwd().resolve()
    if cwd not in root.parents and root != cwd:
        raise ValueError("path escapes project root")
    if not root.exists():
        return json.dumps({"root": relpath.as_posix(), "files": [], "error": "not_found"}, ensure_ascii=True, indent=2)
    if not root.is_dir():
        return json.dumps({"root": relpath.as_posix(), "files": [], "error": "not_a_dir"}, ensure_ascii=True, indent=2)

    files: list[str] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        # Skip obvious large/vendor dirs
        if any(part in {".terraform", ".git", ".memory", "__pycache__"} for part in p.parts):
            continue
        rel = p.relative_to(cwd).as_posix()
        files.append(rel)
        if len(files) >= max(1, int(max_files)):
            break

    files.sort()
    return json.dumps({"root": relpath.as_posix(), "files": files}, ensure_ascii=True, indent=2)

