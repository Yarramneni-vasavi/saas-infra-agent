from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage

from dotenv import load_dotenv

# Load .env before anything else
load_dotenv(Path(__file__).parent.parent.parent / ".env")

# Allow running as a script: `python saas_infra_agent/evals/run_metrics.py ...`
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from saas_infra_agent.agent.domaingate import check_domain
from saas_infra_agent.config.config import config
from saas_infra_agent.llm.factory import get_small_llm

EVALS_ROOT = Path(__file__).resolve().parent
TMP_ROOT = REPO_ROOT / ".tmp_evals"


AgentName = Literal["orchestrator", "design_agent", "build_agent"]

ALL_AGENTS: list[AgentName] = ["orchestrator", "design_agent", "build_agent"]

QUALITY_THRESHOLDS = {
    "answer_relevancy": 0.70,
    "faithfulness": 0.70,
    "factual_correctness": 0.70,
}


def _load_dataset(agent_name: AgentName) -> dict[str, Any]:
    path = EVALS_ROOT / agent_name / "evals.json"
    return json.loads(path.read_text(encoding="utf-8"))


@contextmanager
def _isolated_run_env(case_id: str):
    original_cwd = Path.cwd()
    original_db_path = config["memory"]["db_path"]

    run_dir = TMP_ROOT / case_id
    run_dir.mkdir(parents=True, exist_ok=True)
    config["memory"]["db_path"] = str(run_dir / "memory.db")
    os.chdir(run_dir)
    try:
        yield run_dir
    finally:
        os.chdir(original_cwd)
        config["memory"]["db_path"] = original_db_path


def _build_orchestrator_reply(domain_flag: str, result: Any | None) -> str:
    if domain_flag == "out_of_domain":
        return (
            "This CLI only supports SaaS infrastructure topics (design/build/monitor). "
            "I can't help with consumer tasks like shopping/orders. "
            "Ask about AWS/Terraform/Kubernetes/deployments/monitoring instead."
        )
    if result is None:
        return ""
    if result.safety_flag == "block":
        return f"Blocked by safety gate: {result.reasoning}"
    if result.safety_flag == "needs_review":
        return (
            f"{result.reasoning}\n\n"
            f"{result.clarification_question or 'Reply YES to proceed.'}"
        )
    if result.requires_clarification:
        return result.clarification_question or "Could you clarify that request?"
    if result.intent == "general":
        return "Would answer via the general helper path."
    return f"Would dispatch to the {result.intent} agent."


def _run_orchestrator_case(case: dict[str, Any]) -> dict[str, Any]:
    from saas_infra_agent.agent.orchestrator import SessionState, route

    prompt = str(case["prompt"])
    state = SessionState(**dict(case.get("state_setup") or {}))
    domain = check_domain(prompt)
    result = None
    if domain.flag != "out_of_domain":
        result = route(prompt, state)

    return {
        "case_id": case["id"],
        "domain_flag": domain.flag,
        "domain_reason": domain.reason,
        "intent": getattr(result, "intent", None),
        "confidence": getattr(result, "confidence", None),
        "requires_clarification": getattr(result, "requires_clarification", None),
        "clarification_question": getattr(result, "clarification_question", None),
        "safety_flag": getattr(result, "safety_flag", None),
        "reasoning": getattr(result, "reasoning", None),
        "reply_preview": _build_orchestrator_reply(domain.flag, result),
    }


def _advance_design_state(state):
    from saas_infra_agent.agent.design_flow import (
        _architecture_prepare,
        _clarify_prepare_pdr,
        _decisions_prepare,
        _requirements_prepare,
        _save,
    )

    for _ in range(10):
        if state.stage == "clarify":
            return _clarify_prepare_pdr(state)
        if state.stage == "requirements":
            return _requirements_prepare(state)
        if state.stage == "architecture":
            return _architecture_prepare(state)
        if state.stage == "decisions":
            return _decisions_prepare(state)
        if state.stage == "save":
            state = _save(state)
            continue
        if state.stage in {"done", "wait"}:
            return state
        raise ValueError(f"Unsupported design stage during eval: {state.stage}")
    raise RuntimeError("Design eval runner exceeded the maximum step budget.")


def _run_design_case(case: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    from saas_infra_agent.agent.design_flow import (
        DesignFlowState,
        _capture_user_message,
        _init_or_reset,
        apply_pdr_user_reply,
    )

    mode = str(case.get("mode") or "invoke")
    state_setup = dict(case.get("state_setup") or {})

    if mode == "resume":
        state = DesignFlowState(**state_setup)
        state = apply_pdr_user_reply(state, case.get("input", ""))
        state = _advance_design_state(state)
    else:
        state = DesignFlowState(user_message=str(case.get("input", "")), **state_setup)
        state = _init_or_reset(state)
        state = _capture_user_message(state)
        state = _advance_design_state(state)

    pdr_path = run_dir / "pdr.md"
    pdr_text = pdr_path.read_text(encoding="utf-8") if pdr_path.exists() else ""

    return {
        "case_id": case["id"],
        "mode": mode,
        "stage": state.stage,
        "awaiting_kind": state.awaiting_kind,
        "awaiting_prompt": state.awaiting_prompt,
        "assistant_output": state.assistant_output,
        "pending_questions": state.pending_questions,
        "clarify_asked_count": state.clarify_asked_count,
        "requirements_md": state.requirements_md,
        "architecture_md": state.architecture_md,
        "decisions_md": state.decisions_md,
        "project_context": state.project_context,
        "selected_skills": state.selected_skills,
        "pdr_exists": pdr_path.exists(),
        "pdr_excerpt": pdr_text[:8000],
    }


def _interrupt_payload(result: dict) -> Any | None:
    interrupts = result.get("__interrupt__") or []
    if not interrupts:
        return None
    return getattr(interrupts[0], "value", interrupts[0])


def _run_build_case(case: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    from langgraph.types import Command

    from saas_infra_agent.agent.build_agent import create_build_agent
    from saas_infra_agent.memory.task_store import load_tasks, save_tasks

    mode = str(case.get("mode") or "invoke")
    thread_id = f"eval-{case['id']}"

    files_setup = dict(case.get("files_setup") or {})
    for rel_path, content in files_setup.items():
        path = run_dir / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content), encoding="utf-8")

    task_plan_setup = list(case.get("task_plan_setup") or [])
    if task_plan_setup:
        save_tasks(thread_id, task_plan_setup)

    # Must be created inside the isolated env: the filesystem backend roots at
    # cwd and the checkpointer/task store use the swapped-in db_path.
    agent = create_build_agent()
    agent_config = {"configurable": {"thread_id": thread_id}}

    interrupt_prompts: list[str] = []

    def _record_interrupt(result: dict) -> Any | None:
        payload = _interrupt_payload(result)
        if isinstance(payload, dict) and isinstance(payload.get("prompt"), str):
            interrupt_prompts.append(payload["prompt"])
        elif payload is not None:
            interrupt_prompts.append(str(payload))
        return payload

    result = agent.invoke(
        {"messages": [HumanMessage(content=str(case.get("input", "")))]},
        agent_config,
    )
    payload = _record_interrupt(result)

    if mode == "approve" and payload is not None:
        result = agent.invoke(
            Command(resume=str(case.get("approval_reply", "approve"))),
            agent_config,
        )
        payload = _record_interrupt(result)

    pending_interrupt = None
    if payload is not None:
        pending_interrupt = payload.get("type") if isinstance(payload, dict) else str(payload)

    final_message = ""
    messages = result.get("messages") or []
    if messages:
        content = getattr(messages[-1], "content", "")
        final_message = content if isinstance(content, str) else json.dumps(content, ensure_ascii=True)

    generated_files = sorted(
        str(p.relative_to(run_dir)).replace(os.sep, "/")
        for p in run_dir.rglob("*")
        if p.is_file() and not p.name.startswith("memory.db")
    )
    generated_files = [f for f in generated_files if f not in files_setup]

    return {
        "case_id": case["id"],
        "mode": mode,
        "interrupt_count": len(interrupt_prompts),
        "pending_interrupt": pending_interrupt,
        "interrupt_prompts": [p[:4000] for p in interrupt_prompts],
        "final_message": final_message[:8000],
        "generated_files": generated_files,
        "task_plan": load_tasks(thread_id),
    }


def _safe_div(n: float, d: float) -> float:
    return 0.0 if d == 0 else n / d


@dataclass(frozen=True)
class PR:
    precision: float
    recall: float


def _precision_recall(y_true: list[str], y_pred: list[str], labels: list[str]) -> dict[str, PR]:
    out: dict[str, PR] = {}
    for lab in labels:
        tp = sum(1 for t, p in zip(y_true, y_pred) if t == lab and p == lab)
        fp = sum(1 for t, p in zip(y_true, y_pred) if t != lab and p == lab)
        fn = sum(1 for t, p in zip(y_true, y_pred) if t == lab and p != lab)
        out[lab] = PR(precision=_safe_div(tp, tp + fp), recall=_safe_div(tp, tp + fn))
    return out


def _judge_quality(*, metric_input: str, context: str, output: str) -> dict[str, Any]:
    system = (
        "You are an evaluation judge. Score the assistant output on these metrics from 0.0 to 1.0:\n"
        "- answer_relevancy: does OUTPUT address the user's INPUT (or the current workflow step) in a useful way?\n"
        "- faithfulness: does OUTPUT avoid introducing new specific facts not supported by CONTEXT?\n"
        "- factual_correctness: is OUTPUT consistent with CONTEXT and free of clear contradictions?\n\n"
        "Return ONLY compact JSON, no prose, no markdown fences:\n"
        "{\n"
        '  "answer_relevancy": {"score": 0.0-1.0, "rationale": "<one sentence>"},\n'
        '  "faithfulness": {"score": 0.0-1.0, "rationale": "<one sentence>"},\n'
        '  "factual_correctness": {"score": 0.0-1.0, "rationale": "<one sentence>"}\n'
        "}\n"
    )
    prompt = (
        f"INPUT:\n{metric_input}\n\n"
        f"CONTEXT:\n{context or '(none)'}\n\n"
        f"OUTPUT:\n{output}\n"
    )
    try:
        llm = get_small_llm()
        resp = llm.invoke([SystemMessage(content=system), HumanMessage(content=prompt)])
        text = getattr(resp, "content", str(resp)).strip()
    except Exception as exc:
        msg = f"Judge call failed: {type(exc).__name__}"
        return {
            "answer_relevancy": {"score": 0.0, "rationale": msg},
            "faithfulness": {"score": 0.0, "rationale": msg},
            "factual_correctness": {"score": 0.0, "rationale": msg},
        }
    try:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]
        return json.loads(text)
    except Exception:
        return {
            "answer_relevancy": {"score": 0.0, "rationale": "Could not parse judge output."},
            "faithfulness": {"score": 0.0, "rationale": "Could not parse judge output."},
            "factual_correctness": {"score": 0.0, "rationale": "Could not parse judge output."},
        }


def _pass_pct(passes: int, total: int) -> float:
    return 0.0 if total == 0 else (100.0 * passes / total)


def _deterministic_checks(
    agent_name: AgentName, case: dict[str, Any], actual: dict[str, Any], run_dir: Path
) -> list[dict[str, Any]]:
    """Rule-based expectations that need no LLM judge."""
    checks: list[dict[str, Any]] = []

    if agent_name == "orchestrator":
        for field, actual_key in (
            ("expected_domain_flag", "domain_flag"),
            ("expected_intent", "intent"),
            ("expected_safety_flag", "safety_flag"),
        ):
            expected = case.get(field)
            if isinstance(expected, str):
                # Intent/safety are only produced for in-domain prompts.
                if field != "expected_domain_flag" and case.get("expected_domain_flag") == "out_of_domain":
                    continue
                got = actual.get(actual_key)
                checks.append(
                    {"name": actual_key, "expected": expected, "actual": got, "pass": str(got) == expected}
                )
        return checks

    if agent_name == "build_agent":
        if "expected_interrupt" in case:
            expected = case["expected_interrupt"]
            got = actual.get("pending_interrupt")
            checks.append(
                {"name": "pending_interrupt", "expected": expected, "actual": got, "pass": got == expected}
            )
        for rel in case.get("expected_files") or []:
            exists = (run_dir / rel).exists()
            checks.append({"name": f"file_exists:{rel}", "expected": True, "actual": exists, "pass": exists})
        for rel in case.get("forbidden_files") or []:
            exists = (run_dir / rel).exists()
            checks.append({"name": f"file_absent:{rel}", "expected": True, "actual": not exists, "pass": not exists})
        if "expects_task_plan" in case:
            expected = bool(case["expects_task_plan"])
            got = bool(actual.get("task_plan"))
            checks.append({"name": "task_plan_saved", "expected": expected, "actual": got, "pass": got == expected})
        return checks

    # design_agent: expected files written into the run workspace.
    for rel in case.get("files") or []:
        exists = (run_dir / rel).exists()
        checks.append({"name": f"file_exists:{rel}", "expected": True, "actual": exists, "pass": exists})
    return checks


def _judge_io(agent_name: AgentName, case: dict[str, Any], actual: dict[str, Any]) -> tuple[str, str, str]:
    """(metric_input, context, output) fed to the LLM judge for one case."""
    if agent_name == "orchestrator":
        metric_input = str(case.get("prompt", ""))
        context = json.dumps(case.get("state_setup") or {}, ensure_ascii=True, indent=2)
        output = str(actual.get("reply_preview", ""))
        return metric_input, context, output

    if agent_name == "build_agent":
        metric_input = str(case.get("input", ""))
        if case.get("approval_reply"):
            metric_input += f"\n\n[Reply to the plan-approval prompt]: {case['approval_reply']}"
        context_parts = []
        for rel_path, content in (case.get("files_setup") or {}).items():
            context_parts.append(f"--- {rel_path} ---\n{content}")
        if case.get("task_plan_setup"):
            context_parts.append(
                "--- stored task plan ---\n"
                + json.dumps(case["task_plan_setup"], ensure_ascii=True, indent=2)
            )
        context = "\n\n".join(context_parts)
        output_parts = []
        if actual.get("interrupt_prompts"):
            output_parts.append("INTERRUPT_PROMPTS:\n" + "\n---\n".join(actual["interrupt_prompts"]))
        output_parts.append("FINAL_MESSAGE:\n" + str(actual.get("final_message") or ""))
        output_parts.append("GENERATED_FILES:\n" + json.dumps(actual.get("generated_files") or []))
        return metric_input, context, "\n\n".join(output_parts)

    # design_agent
    metric_input = str(case.get("input", ""))
    context = str(actual.get("project_context") or "")
    output = str(actual.get("assistant_output") or "")
    if actual.get("pdr_exists"):
        output = output + "\n\nPDR_EXCERPT:\n" + str(actual.get("pdr_excerpt") or "")
    return metric_input, context, output


def _score_case(
    agent_name: AgentName,
    case: dict[str, Any],
    actual: dict[str, Any],
    run_dir: Path,
    *,
    use_llm: bool,
) -> dict[str, Any]:
    """Per-question metrics: LLM-judged quality + deterministic checks."""
    metric_input, context, output = _judge_io(agent_name, case, actual)

    metrics: dict[str, Any] = {}
    if use_llm:
        judge = _judge_quality(metric_input=metric_input, context=context, output=output)
        for metric, thr in QUALITY_THRESHOLDS.items():
            entry = judge.get(metric, {}) if isinstance(judge, dict) else {}
            score = float(entry.get("score", 0.0) or 0.0)
            metrics[metric] = {
                "score": round(score, 3),
                "threshold": thr,
                "pass": score >= thr,
                "rationale": str(entry.get("rationale", "")),
            }

    checks = _deterministic_checks(agent_name, case, actual, run_dir)
    if checks:
        passed = sum(1 for c in checks if c["pass"])
        metrics["deterministic"] = {
            "score": round(_safe_div(passed, len(checks)), 3),
            "passed": passed,
            "total": len(checks),
            "pass": passed == len(checks),
            "checks": checks,
        }

    return {
        "case_id": case["id"],
        "category": case.get("category"),
        "case_pass": all(m["pass"] for m in metrics.values()) if metrics else None,
        "metrics": metrics,
        "actual": actual,
    }


def _overall_metrics(scored: list[dict[str, Any]]) -> dict[str, Any]:
    overall: dict[str, Any] = {}
    metric_names = sorted({name for row in scored for name in row["metrics"]})
    for name in metric_names:
        rows = [row["metrics"][name] for row in scored if name in row["metrics"]]
        overall[name] = {
            "cases": len(rows),
            "avg_score": round(_safe_div(sum(float(r["score"]) for r in rows), len(rows)), 3),
            "pass_percent": round(_pass_pct(sum(1 for r in rows if r["pass"]), len(rows)), 1),
        }
        if name in QUALITY_THRESHOLDS:
            overall[name]["threshold"] = QUALITY_THRESHOLDS[name]
    graded = [row for row in scored if row["case_pass"] is not None]
    overall["cases_passing_all_metrics"] = {
        "cases": len(graded),
        "passed": sum(1 for row in graded if row["case_pass"]),
        "pass_percent": round(_pass_pct(sum(1 for row in graded if row["case_pass"]), len(graded)), 1),
    }
    return overall


def run_dataset(agent_name: AgentName, case_id: str | None = None, *, use_llm: bool = True) -> dict[str, Any]:
    dataset = _load_dataset(agent_name)
    cases = dataset["evals"]
    if case_id is not None:
        cases = [case for case in cases if str(case["id"]) == case_id]
    if not cases:
        raise SystemExit(f"No eval cases matched agent={agent_name!r} case_id={case_id!r}.")

    actuals: list[dict[str, Any]] = []
    scored: list[dict[str, Any]] = []

    for case in cases:
        with _isolated_run_env(str(case["id"])) as run_dir:
            if agent_name == "orchestrator":
                actual = _run_orchestrator_case(case)
            elif agent_name == "build_agent":
                actual = _run_build_case(case, run_dir)
            else:
                actual = _run_design_case(case, run_dir)

            actual["artifacts_dir"] = str(run_dir)
            actuals.append(actual)
            scored.append(_score_case(agent_name, case, actual, run_dir, use_llm=use_llm))

    summary: dict[str, Any] = {
        "agent": agent_name,
        "cases": len(cases),
        "per_case": scored,
        "overall": _overall_metrics(scored),
        "classification": {},
    }

    if agent_name == "orchestrator":
        # Deterministic classification metrics when expected_* fields exist.
        y_true_intent: list[str] = []
        y_pred_intent: list[str] = []
        y_true_domain: list[str] = []
        y_pred_domain: list[str] = []
        y_true_safety: list[str] = []
        y_pred_safety: list[str] = []

        for case, actual in zip(cases, actuals):
            exp_domain = case.get("expected_domain_flag")
            exp_intent = case.get("expected_intent")
            exp_safety = case.get("expected_safety_flag")

            if isinstance(exp_domain, str):
                y_true_domain.append(exp_domain)
                y_pred_domain.append(str(actual.get("domain_flag")))

            if isinstance(exp_intent, str) and exp_domain != "out_of_domain":
                y_true_intent.append(exp_intent)
                y_pred_intent.append(str(actual.get("intent")))

            if isinstance(exp_safety, str) and exp_domain != "out_of_domain":
                y_true_safety.append(exp_safety)
                y_pred_safety.append(str(actual.get("safety_flag")))

        for key, y_true, y_pred in (
            ("intent", y_true_intent, y_pred_intent),
            ("domain_flag", y_true_domain, y_pred_domain),
            ("safety_flag", y_true_safety, y_pred_safety),
        ):
            if not y_true:
                continue
            labels = sorted(set(y_true) | set(y_pred))
            pr = _precision_recall(y_true, y_pred, labels)
            acc = sum(1 for t, p in zip(y_true, y_pred) if t == p) / len(y_true)
            summary["classification"][key] = {
                "accuracy": acc,
                "per_label": {k: {"precision": v.precision, "recall": v.recall} for k, v in pr.items()},
            }

    return summary


def _print_readable_report(report: dict[str, Any]) -> None:
    for agent_name, summary in report["agents"].items():
        scored = summary["per_case"]
        metric_names = sorted({name for row in scored for name in row["metrics"]})

        print(f"\n=== {agent_name} ({summary['cases']} cases) ===")
        header = f"{'case':<20} {'category':<26}" + "".join(f" {m[:16]:>18}" for m in metric_names) + f" {'result':>8}"
        print(header)
        print("-" * len(header))
        for row in scored:
            cells = ""
            for name in metric_names:
                metric = row["metrics"].get(name)
                if metric is None:
                    cells += f" {'-':>18}"
                elif name == "deterministic":
                    cells += f" {metric['passed']}/{metric['total']} {'PASS' if metric['pass'] else 'FAIL':>4}".rjust(19)
                else:
                    cells += f" {metric['score']:.2f} {'PASS' if metric['pass'] else 'FAIL':>4}".rjust(19)
            result = "-" if row["case_pass"] is None else ("PASS" if row["case_pass"] else "FAIL")
            print(f"{row['case_id']:<20} {str(row['category'] or ''):<26}{cells} {result:>8}")

        print("\nOverall:")
        for name, stats in summary["overall"].items():
            if name == "cases_passing_all_metrics":
                print(
                    f"  cases passing all metrics: {stats['passed']}/{stats['cases']} "
                    f"({stats['pass_percent']}%)"
                )
            else:
                thr = f", threshold {stats['threshold']}" if "threshold" in stats else ""
                print(
                    f"  {name:<22} avg score {stats['avg_score']:.2f}, "
                    f"pass {stats['pass_percent']}%{thr}"
                )

        for key, stats in (summary.get("classification") or {}).items():
            print(f"  classification/{key}: accuracy {stats['accuracy']:.2f}")
            for label, pr in stats["per_label"].items():
                print(f"    {label:<16} precision {pr['precision']:.2f}, recall {pr['recall']:.2f}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run eval metrics and report per-case and overall scores.")
    parser.add_argument(
        "--agent",
        choices=["orchestrator", "design_agent", "build_agent", "all"],
        default="all",
    )
    parser.add_argument("--case-id", help="Run only one eval case by id.")
    parser.add_argument("--out", help="Write full JSON report to this path.")
    parser.add_argument("--json", action="store_true", help="Print the full JSON report to stdout as well.")
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Skip LLM-based quality judging (still computes deterministic checks and classification).",
    )
    args = parser.parse_args()

    agents: list[AgentName] = list(ALL_AGENTS) if args.agent == "all" else [args.agent]  # type: ignore[list-item]
    report: dict[str, Any] = {"agents": {}}
    for agent in agents:
        report["agents"][agent] = run_dataset(agent, case_id=args.case_id, use_llm=(not args.no_llm))

    _print_readable_report(report)

    text = json.dumps(report, ensure_ascii=True, indent=2, default=str)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"\nFull JSON report written to {args.out}")
    if args.json:
        print(text)


if __name__ == "__main__":
    main()
