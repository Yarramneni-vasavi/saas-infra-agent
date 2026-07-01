from __future__ import annotations

from pathlib import Path

from langchain.tools import tool

from saas_infra_agent.config.config import config
from saas_infra_agent.observability.logger import get_logger


logger = get_logger(__name__)


def _artifact_root() -> Path:
    artifact_dir = (config.get("agent") or {}).get("artifact_dir", "artifacts")
    return Path.cwd() / artifact_dir


def _safe_relpath(path: str) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        raise ValueError("path must be relative (no drive letter / leading slash)")
    if ".." in candidate.parts:
        raise ValueError("path must not contain '..'")
    return candidate


@tool
def write_artifact(path: str, content: str) -> str:
    """
    Write a generated artifact (Dockerfile, compose, Terraform, etc.) under the configured artifact directory.

    Safety: only allows relative paths and prevents directory traversal.
    """
    logger.info(f"Tool called: write_artifact path={path!r} bytes={len(content)}")

    relpath = _safe_relpath(path)
    root = _artifact_root()
    target = (root / relpath).resolve()

    # Ensure resolved target stays within the artifact root.
    root_resolved = root.resolve()
    if root_resolved not in target.parents and target != root_resolved:
        raise ValueError("path escapes artifact root")

    root.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    try:
        shown = target.relative_to(Path.cwd())
    except ValueError:
        shown = target
    return f"Wrote artifact: {shown}"
