"""Run the BUILD-agent eval suite.

    poetry run python -m evals.run_build_evals                # all cases, 1 run each
    poetry run python -m evals.run_build_evals --only resume-stored-plan
    poetry run python -m evals.run_build_evals --runs 3       # pass-rate over repeats
    poetry run python -m evals.run_build_evals --judge        # add LLM fidelity judge

Needs OPENAI_API_KEY (read from .env). Each run gets a throwaway workspace
under evals/results/<timestamp>/ — kept for inspection — plus report.json.
A case passes when every *required* check passes; advisory checks and
skipped checks (e.g. terraform not installed) are reported but don't gate.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

from evals import checks as _checks  # noqa: E402  (path setup first)
from evals.cases import CASES, FIXTURES, EvalCase  # noqa: E402
from evals.harness import run_case  # noqa: E402

console = Console()


def _make_workspace(base: Path, case: EvalCase, attempt: int) -> Path:
    ws = base / f"{case.name}-{attempt}"
    ws.mkdir(parents=True)
    if case.arch_fixture:
        shutil.copy(FIXTURES / case.arch_fixture, ws / "architecture.md")
    return ws


def _run_one(case: EvalCase, ws: Path, use_judge: bool) -> dict:
    console.print(f"[bold]{case.name}[/bold] → {ws.name}")
    run = run_case(ws, case.query, list(case.interrupt_replies), case.seed_tasks)

    results = [chk(run, ws) for chk in case.checks]
    judge_result = None
    if use_judge and case.judge and run.error is None:
        from evals.judge import judge_fidelity

        judge_result = judge_fidelity(ws)
        results.append(_checks.CheckResult(
            "llm_judge_fidelity",
            judge_result.verdict == "pass",
            f"score={judge_result.score:.2f} issues={judge_result.issues[:3]}"
            + (f" error={judge_result.error}" if judge_result.error else ""),
            required=False,
        ))

    passed = all(r.passed is not False for r in results if r.required)
    for r in results:
        mark = {"True": "[green]pass[/green]", "False": "[red]FAIL[/red]", "None": "[yellow]skip[/yellow]"}[str(r.passed)]
        req = "" if r.required else " [dim](advisory)[/dim]"
        console.print(f"    {mark}  {r.name}{req}  [dim]{r.detail}[/dim]")

    return {
        "case": case.name,
        "workspace": str(ws),
        "passed": passed,
        "error": run.error,
        "interrupts": run.interrupt_prompts,
        "tool_calls": [c.name for c in run.tool_calls],
        "checks": [
            {"name": r.name, "passed": r.passed, "required": r.required, "detail": r.detail}
            for r in results
        ],
        "judge": judge_result.__dict__ if judge_result else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="BUILD-agent evals")
    parser.add_argument("--runs", type=int, default=1, help="repeats per case (pass rate)")
    parser.add_argument("--only", action="append", help="run only these case names (repeatable)")
    parser.add_argument("--judge", action="store_true", help="add the LLM fidelity judge")
    args = parser.parse_args()

    cases = [c for c in CASES if not args.only or c.name in args.only]
    if not cases:
        console.print(f"[red]No cases match {args.only}; available: {[c.name for c in CASES]}[/red]")
        return 2

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = Path(__file__).parent / "results" / stamp
    records = []
    for case in cases:
        for attempt in range(1, args.runs + 1):
            ws = _make_workspace(base, case, attempt)
            records.append(_run_one(case, ws, args.judge))

    table = Table(title=f"BUILD agent evals — {stamp}")
    table.add_column("case")
    table.add_column("pass rate")
    all_pass = True
    for case in cases:
        rs = [r for r in records if r["case"] == case.name]
        n_pass = sum(r["passed"] for r in rs)
        ok = n_pass == len(rs)
        all_pass &= ok
        table.add_row(case.name, f"[{'green' if ok else 'red'}]{n_pass}/{len(rs)}[/]")
    console.print(table)

    report = base / "report.json"
    report.write_text(json.dumps(records, indent=2))
    console.print(f"Report: {report}\nWorkspaces kept under {base} for inspection.")
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
