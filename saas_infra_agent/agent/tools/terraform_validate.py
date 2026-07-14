"""Local Terraform validation tool (human-approved).

This is intentionally narrow: it does NOT run `terraform apply`.
It only runs `terraform init -backend=false` and `terraform validate` so the
BUILD agent can catch and fix obvious Terraform errors before handing output to
the user.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from langchain.tools import tool
from langgraph.types import interrupt

from saas_infra_agent.config.config import config
from saas_infra_agent.observability.logger import get_logger

logger = get_logger(__name__)

_APPROVED_THIS_PROCESS = False

_YES = {"approve", "approved", "yes", "y", "ok", "okay", "run", "continue", "proceed"}


def _is_approved(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    tokens = "".join(ch if (ch.isalnum() or ch.isspace()) else " " for ch in t).split()
    return any(tok in _YES for tok in tokens)


def _artifact_dir() -> Path:
    agent_cfg = dict(config.get("agent") or {})
    artifact_dir = str(agent_cfg.get("artifact_dir", ".") or ".").strip().strip("/").strip("\\")
    if artifact_dir in {"", "."}:
        return Path.cwd()
    return Path.cwd() / artifact_dir


def _pick_tf_dir(rel_path: str) -> Path:
    # Caller can pass a direct directory.
    p = Path(rel_path)
    if p.is_absolute():
        return p

    root = _artifact_dir()
    candidates = [
        root / rel_path,
        root / "infra",
        root,
    ]
    for c in candidates:
        if c.is_dir() and any(c.glob("*.tf")):
            return c
    # Fall back to the intended location (even if empty) for clearer errors.
    return root / rel_path


def _run(cmd: list[str], cwd: Path) -> dict:
    env = dict(os.environ)
    env["TF_IN_AUTOMATION"] = "1"
    try:
        p = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            env=env,
            timeout=600,
        )
        out = (p.stdout or "").strip()
        err = (p.stderr or "").strip()
        # Keep tool output bounded so it doesn't explode the agent context.
        max_chars = 12000
        if len(out) > max_chars:
            out = out[: max_chars // 2] + "\n...\n" + out[-max_chars // 2 :]
        if len(err) > max_chars:
            err = err[: max_chars // 2] + "\n...\n" + err[-max_chars // 2 :]
        return {"ok": p.returncode == 0, "code": p.returncode, "stdout": out, "stderr": err}
    except FileNotFoundError:
        return {"ok": False, "code": -1, "stdout": "", "stderr": "terraform binary not found on PATH."}
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": -2, "stdout": "", "stderr": f"Command timed out: {' '.join(cmd)}"}


@tool
def terraform_validate(path: str = "infra") -> str:
    """Run `terraform init -backend=false` and `terraform validate` in a directory `infra` inside current working directory.

    Human approval:
    - On first call in this process, pauses for explicit approval via interrupt().
    - Subsequent calls reuse that approval (so the agent can iterate fixes).
    """
    global _APPROVED_THIS_PROCESS

    tf_dir = _pick_tf_dir(path)
    init_cmd = ["terraform", "init", "-backend=false", "-input=false", "-no-color"]
    validate_cmd = ["terraform", "validate", "-no-color"]

    if not _APPROVED_THIS_PROCESS:
        logger.info("Tool called: terraform_validate — requesting human approval")
        reply = interrupt(
            {
                "type": "exec_approval",
                "prompt": (
                    "The BUILD agent wants to run local Terraform validation:\n\n"
                    f"- Working dir: {tf_dir}\n"
                    f"- Command 1: {' '.join(init_cmd)}\n"
                    f"- Command 2: {' '.join(validate_cmd)}\n\n"
                    "Reply 'approve' to run these commands (and allow re-runs for iterative fixes), "
                    "or anything else to cancel."
                ),
            }
        )
        if not _is_approved(str(reply)):
            return json.dumps(
                {
                    "approved": False,
                    "ok": False,
                    "error": "Cancelled by user.",
                    "tf_dir": str(tf_dir),
                },
                ensure_ascii=True,
                indent=2,
            )
        _APPROVED_THIS_PROCESS = True

    logger.info("terraform_validate: running terraform init/validate")
    init_res = _run(init_cmd, tf_dir)
    validate_res = _run(validate_cmd, tf_dir) if init_res["ok"] else {"ok": False, "code": None, "stdout": "", "stderr": ""}
    return json.dumps(
        {
            "approved": True,
            "tf_dir": str(tf_dir),
            "init": init_res,
            "validate": validate_res,
            "ok": bool(init_res["ok"] and validate_res["ok"]),
        },
        ensure_ascii=True,
        indent=2,
    )

