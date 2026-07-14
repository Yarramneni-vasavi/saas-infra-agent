from __future__ import annotations

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
def read_project_file(path: str) -> str:
    """
    Read a text file from the project working directory (CWD).

    Safety: only allows relative paths and prevents directory traversal.
    """
    logger.info(f"Tool called: read_project_file path={path!r}")
    relpath = _safe_relpath(path)
    target = (Path.cwd() / relpath).resolve()
    cwd = Path.cwd().resolve()
    if cwd not in target.parents and target != cwd:
        raise ValueError("path escapes project root")
    try:
        return target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return f"File not found: {relpath.as_posix()}"
