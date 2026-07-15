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
_CONSECUTIVE_FAILURES = 0
_MAX_CONSECUTIVE_FAILURES = 3

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

def _deploy_emulator_enabled() -> bool:
    deploy_cfg = dict(config.get("deploy") or {})
    return bool(deploy_cfg.get("emulator"))


def _floci_endpoint() -> str:
    deploy_cfg = dict(config.get("deploy") or {})
    floci_cfg = dict(deploy_cfg.get("floci") or {})
    return str(floci_cfg.get("endpoint") or "").strip()


def _floci_provider_preflight(tf_dir: Path) -> str | None:
    """Best-effort check that the generated Terraform includes Floci-friendly AWS provider config."""
    if not _deploy_emulator_enabled():
        return None

    tf_files = sorted(tf_dir.glob("*.tf"))
    if not tf_files:
        return None

    text = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in tf_files)
    endpoint = _floci_endpoint()

    # Minimal requirements for local emulators like Floci/LocalStack: custom endpoints
    # plus dummy creds and/or skip validations.
    has_provider = 'provider "aws"' in text
    has_endpoints = "endpoints" in text and "s3" in text
    has_endpoint_value = (endpoint in text) if endpoint else ("localhost:4566" in text)
    has_skip = "skip_credentials_validation" in text or "skip_requesting_account_id" in text
    has_dummy_creds = "access_key" in text and "secret_key" in text

    if not has_provider:
        return (
            "Floci emulator mode is enabled (config.deploy.emulator=true), but no `provider \"aws\" { ... }` "
            "block was found. Add the AWS provider configuration per `/skills/terraform-floci-emulator/SKILL.md`."
        )
    if not (has_endpoints and has_endpoint_value):
        return (
            "Floci emulator mode is enabled, but the AWS provider `endpoints { ... }` configuration "
            "does not appear to be set for the Floci endpoint. Follow `/skills/terraform-floci-emulator/SKILL.md`."
        )
    if not (has_skip or has_dummy_creds):
        return (
            "Floci emulator mode is enabled, but the AWS provider config does not appear to include "
            "dummy credentials and/or skip-* validation flags. Follow `/skills/terraform-floci-emulator/SKILL.md`."
        )
    return None


@tool
def terraform_validate(path: str = "infra") -> str:
    """Run `terraform init -backend=false` and `terraform validate` in a directory `infra` inside current working directory.

    Human approval:
    - On first call in this process, pauses for explicit approval via interrupt().
    - Subsequent calls reuse that approval (so the agent can iterate fixes).
    """
    global _APPROVED_THIS_PROCESS
    global _CONSECUTIVE_FAILURES

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

    preflight_err = _floci_provider_preflight(tf_dir)
    if preflight_err:
        _CONSECUTIVE_FAILURES += 1
        if _CONSECUTIVE_FAILURES >= _MAX_CONSECUTIVE_FAILURES:
            reply = interrupt(
                {
                    "type": "exec_continue",
                    "prompt": (
                        f"Terraform validation has failed {_CONSECUTIVE_FAILURES} times in a row.\n\n"
                        f"Latest issue:\n{preflight_err}\n\n"
                        "Reply 'continue' to keep iterating fixes, or anything else to stop for now."
                    ),
                }
            )
            if not _is_approved(str(reply)):
                return json.dumps(
                    {
                        "approved": True,
                        "ok": False,
                        "halt": True,
                        "error": "Stopped after repeated validation failures.",
                        "tf_dir": str(tf_dir),
                        "consecutive_failures": _CONSECUTIVE_FAILURES,
                    },
                    ensure_ascii=True,
                    indent=2,
                )
            _CONSECUTIVE_FAILURES = 0

        return json.dumps(
            {
                "approved": True,
                "ok": False,
                "error": preflight_err,
                "tf_dir": str(tf_dir),
                "consecutive_failures": _CONSECUTIVE_FAILURES,
            },
            ensure_ascii=True,
            indent=2,
        )

    logger.info("terraform_validate: running terraform init/validate")
    init_res = _run(init_cmd, tf_dir)
    validate_res = _run(validate_cmd, tf_dir) if init_res["ok"] else {"ok": False, "code": None, "stdout": "", "stderr": ""}
    ok = bool(init_res["ok"] and validate_res["ok"])
    if ok:
        _CONSECUTIVE_FAILURES = 0
    else:
        _CONSECUTIVE_FAILURES += 1
        if _CONSECUTIVE_FAILURES >= _MAX_CONSECUTIVE_FAILURES:
            # Pause the agent after repeated failures to avoid infinite loops.
            reply = interrupt(
                {
                    "type": "exec_continue",
                    "prompt": (
                        f"Terraform validation has failed {_CONSECUTIVE_FAILURES} times in a row.\n\n"
                        f"- Working dir: {tf_dir}\n"
                        f"- Last init ok: {init_res['ok']} (code {init_res['code']})\n"
                        f"- Last validate ok: {validate_res['ok']} (code {validate_res.get('code')})\n\n"
                        "Reply 'continue' to keep iterating fixes, or anything else to stop for now."
                    ),
                }
            )
            if not _is_approved(str(reply)):
                return json.dumps(
                    {
                        "approved": True,
                        "tf_dir": str(tf_dir),
                        "init": init_res,
                        "validate": validate_res,
                        "ok": False,
                        "halt": True,
                        "error": "Stopped after repeated validation failures.",
                        "consecutive_failures": _CONSECUTIVE_FAILURES,
                    },
                    ensure_ascii=True,
                    indent=2,
                )
            _CONSECUTIVE_FAILURES = 0

    return json.dumps(
        {
            "approved": True,
            "tf_dir": str(tf_dir),
            "init": init_res,
            "validate": validate_res,
            "ok": ok,
            "consecutive_failures": _CONSECUTIVE_FAILURES,
        },
        ensure_ascii=True,
        indent=2,
    )
