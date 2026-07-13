"""Deterministic graders for BUILD-agent eval runs.

Each check takes (run, workspace) and returns a CheckResult. `passed` is
True/False, or None when the check could not run (e.g. terraform not
installed) — skipped checks never fail a case. `required=False` marks
advisory checks that are reported but don't gate the case verdict.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .harness import RunResult


@dataclass
class CheckResult:
    name: str
    passed: bool | None  # None = skipped
    detail: str = ""
    required: bool = True


# ---------------------------------------------------------------- behavior

def read_tasks_called_first(run: RunResult, ws: Path) -> CheckResult:
    """The prompt mandates read_tasks as the first call of every run."""
    if not run.tool_calls:
        return CheckResult("read_tasks_first", False, "no tool calls at all")
    first = run.tool_calls[0].name
    return CheckResult(
        "read_tasks_first", first == "read_tasks", f"first tool call was {first!r}"
    )


def approval_before_any_write(run: RunResult, ws: Path) -> CheckResult:
    """No artifact writes may happen before request_plan_approval returns."""
    approval_i = run.first_index("request_plan_approval")
    write_i = run.first_index("write_file")
    if write_i is None:
        return CheckResult("approval_before_write", True, "no writes issued")
    if approval_i is None:
        return CheckResult("approval_before_write", False, "wrote files without ever asking for approval")
    return CheckResult(
        "approval_before_write",
        approval_i < write_i,
        f"approval at call #{approval_i}, first write at call #{write_i}",
    )


def plan_saved_before_approval(run: RunResult, ws: Path) -> CheckResult:
    """write_tasks (the DAG plan) should exist before approval is requested."""
    plan_i = run.first_index("write_tasks")
    approval_i = run.first_index("request_plan_approval")
    if approval_i is None:
        return CheckResult("plan_before_approval", False, "request_plan_approval never called")
    if plan_i is None:
        return CheckResult("plan_before_approval", False, "write_tasks never called")
    return CheckResult(
        "plan_before_approval", plan_i < approval_i,
        f"write_tasks at #{plan_i}, approval at #{approval_i}",
    )


def used_task_tools_not_todos(run: RunResult, ws: Path) -> CheckResult:
    """deepagents always injects write_todos; the prompt steers away from it."""
    used = bool(run.calls_named("write_todos"))
    return CheckResult(
        "no_write_todos", not used,
        "called write_todos" if used else "stuck to write_tasks/read_tasks",
        required=False,
    )


def no_approval_requested(run: RunResult, ws: Path) -> CheckResult:
    """For resume runs: a stored incomplete plan means no re-approval."""
    n = len(run.calls_named("request_plan_approval"))
    return CheckResult("no_reapproval", n == 0, f"request_plan_approval called {n}x")


def approval_requested_at_least(n: int):
    def check(run: RunResult, ws: Path) -> CheckResult:
        got = len(run.calls_named("request_plan_approval"))
        return CheckResult(
            f"approval_requested_>={n}", got >= n, f"request_plan_approval called {got}x"
        )
    return check


def writes_only_under_artifacts(run: RunResult, ws: Path) -> CheckResult:
    """Every attempted write targets /artifacts/ (backend denies others, but
    the model shouldn't even try)."""
    bad = [p for p in run.write_file_paths() if not p.lstrip("/").startswith("artifacts/")]
    return CheckResult(
        "writes_under_artifacts", not bad, f"stray write targets: {bad}" if bad else ""
    )


def no_files_written(run: RunResult, ws: Path) -> CheckResult:
    artifacts = ws / "artifacts"
    written = [str(p.relative_to(ws)) for p in artifacts.rglob("*") if p.is_file()] if artifacts.exists() else []
    return CheckResult("no_files_written", not written, f"wrote: {written}" if written else "")


def mentions_design_agent(run: RunResult, ws: Path) -> CheckResult:
    text = (run.final_text or "").lower()
    hit = any(w in text for w in ("design", "architecture"))
    return CheckResult(
        "points_to_design_agent", hit,
        "final reply doesn't mention needing a design/architecture" if not hit else "",
    )


def run_completed(run: RunResult, ws: Path) -> CheckResult:
    return CheckResult("run_completed", run.error is None, run.error or "")


def all_tasks_completed(load_tasks_fn):
    """After a (resumed) build, the stored plan should be fully completed."""
    def check(run: RunResult, ws: Path) -> CheckResult:
        tasks = load_tasks_fn(ws, run.thread_id)
        if not tasks:
            return CheckResult("all_tasks_completed", False, "no stored plan found")
        pending = [t["id"] for t in tasks if t["status"] != "completed"]
        return CheckResult(
            "all_tasks_completed", not pending,
            f"incomplete tasks: {pending}" if pending else f"{len(tasks)} tasks done",
        )
    return check


# ---------------------------------------------------------------- artifacts

def files_exist(*rel_paths: str):
    def check(run: RunResult, ws: Path) -> CheckResult:
        missing = [p for p in rel_paths if not (ws / p).is_file()]
        return CheckResult(
            "files_exist", not missing, f"missing: {missing}" if missing else ", ".join(rel_paths)
        )
    return check


def files_absent(*rel_globs: str):
    def check(run: RunResult, ws: Path) -> CheckResult:
        present = [str(m.relative_to(ws)) for g in rel_globs for m in ws.glob(g)]
        return CheckResult(
            "files_absent", not present, f"unexpected files: {present}" if present else ""
        )
    return check


_REGION_LITERAL = re.compile(r'"(?:us|eu|ap|sa|ca|me|af)-(?:east|west|north|south|central|northeast|southeast)-\d"')


def no_hardcoded_region_in_main_tf(run: RunResult, ws: Path) -> CheckResult:
    main_tf = ws / "artifacts/infra/main.tf"
    if not main_tf.is_file():
        return CheckResult("no_hardcoded_region", None, "main.tf missing")
    hits = _REGION_LITERAL.findall(main_tf.read_text())
    return CheckResult(
        "no_hardcoded_region", not hits,
        f"region literals in main.tf: {hits} (belongs in variables.tf)" if hits else "",
    )


_SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(
        r'(?i)(password|secret|api[_-]?key|token)\s*[=:]\s*"(?!\$|\{|var\.|data\.|<|change|replace|example|placeholder|your)[^"\s]{8,}"'
    ),
]


def no_plaintext_secrets(run: RunResult, ws: Path) -> CheckResult:
    artifacts = ws / "artifacts"
    if not artifacts.exists():
        return CheckResult("no_plaintext_secrets", None, "no artifacts")
    hits = []
    for f in artifacts.rglob("*"):
        if not f.is_file() or f.suffix in (".png", ".zip"):
            continue
        text = f.read_text(errors="ignore")
        for pat in _SECRET_PATTERNS:
            for m in pat.finditer(text):
                hits.append(f"{f.relative_to(ws)}: {m.group(0)[:40]}")
    return CheckResult("no_plaintext_secrets", not hits, "; ".join(hits[:5]))


def resources_tagged(run: RunResult, ws: Path) -> CheckResult:
    """The prompt requires tags per the cost-optimization skill; a soft
    proxy: main.tf mentions tags at all."""
    main_tf = ws / "artifacts/infra/main.tf"
    if not main_tf.is_file():
        return CheckResult("resources_tagged", None, "main.tf missing")
    text = main_tf.read_text()
    tagged = "tags" in text
    return CheckResult(
        "resources_tagged", tagged, "no `tags` anywhere in main.tf" if not tagged else "",
        required=False,
    )


def terraform_validates(run: RunResult, ws: Path) -> CheckResult:
    infra = ws / "artifacts/infra"
    if not infra.is_dir():
        return CheckResult("terraform_validate", None, "no infra dir")
    if shutil.which("terraform") is None:
        return CheckResult("terraform_validate", None, "terraform not installed — skipped")
    init = subprocess.run(
        ["terraform", f"-chdir={infra}", "init", "-backend=false", "-input=false", "-no-color"],
        capture_output=True, text=True, timeout=300,
    )
    if init.returncode != 0:
        return CheckResult("terraform_validate", False, f"init failed: {init.stderr[-400:]}")
    val = subprocess.run(
        ["terraform", f"-chdir={infra}", "validate", "-no-color"],
        capture_output=True, text=True, timeout=120,
    )
    return CheckResult(
        "terraform_validate", val.returncode == 0,
        (val.stdout + val.stderr)[-400:].strip(),
    )


def compose_parses(run: RunResult, ws: Path) -> CheckResult:
    import yaml

    compose = ws / "artifacts/docker-compose.yml"
    if not compose.is_file():
        compose = ws / "artifacts/docker-compose.yaml"
    if not compose.is_file():
        return CheckResult("compose_parses", False, "docker-compose.yml missing")
    try:
        data = yaml.safe_load(compose.read_text())
    except yaml.YAMLError as exc:
        return CheckResult("compose_parses", False, f"invalid YAML: {exc}")
    services = (data or {}).get("services") or {}
    return CheckResult(
        "compose_parses", bool(services), f"services: {sorted(services)}"
    )


def artifacts_mention(name: str, *needles: str):
    """At least one artifact file contains one of the needles (case-insensitive).
    Used to verify a requested plan revision actually landed."""
    def check(run: RunResult, ws: Path) -> CheckResult:
        artifacts = ws / "artifacts"
        blob = " ".join(
            f.read_text(errors="ignore").lower()
            for f in artifacts.rglob("*") if f.is_file()
        ) if artifacts.exists() else ""
        hit = any(n.lower() in blob for n in needles)
        return CheckResult(name, hit, f"none of {needles} found in artifacts" if not hit else "")
    return check
