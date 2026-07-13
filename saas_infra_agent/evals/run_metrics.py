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


AgentName = Literal["orchestrator", "design_agent"]


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
                output_for_judge = actual.get("reply_preview", "")
                context = json.dumps(case.get("state_setup") or {}, ensure_ascii=True, indent=2)
                metric_input = str(case.get("prompt", ""))
            else:
                actual = _run_design_case(case, run_dir)
                output_for_judge = str(actual.get("assistant_output") or "")
                context = str(actual.get("project_context") or "")
                if actual.get("pdr_exists"):
                    output_for_judge = output_for_judge + "\n\nPDR_EXCERPT:\n" + str(actual.get("pdr_excerpt") or "")
                metric_input = str(case.get("input", ""))

            actual["artifacts_dir"] = str(run_dir)
            actuals.append(actual)

            judge = (
                _judge_quality(metric_input=metric_input, context=context, output=output_for_judge)
                if use_llm
                else {
                    "answer_relevancy": {"score": 0.0, "rationale": "LLM judging disabled."},
                    "faithfulness": {"score": 0.0, "rationale": "LLM judging disabled."},
                    "factual_correctness": {"score": 0.0, "rationale": "LLM judging disabled."},
                }
            )
            scored.append({"case_id": case["id"], "judge": judge, "actual": actual})

    summary: dict[str, Any] = {
        "agent": agent_name,
        "cases": len(cases),
        "quality_pass_percent": {},
        "classification": {},
        "scored_cases": scored,
    }

    thresholds = {"answer_relevancy": 0.70, "faithfulness": 0.70, "factual_correctness": 0.70}
    for metric, thr in thresholds.items():
        passes = 0
        for row in scored:
            score = float(row["judge"].get(metric, {}).get("score", 0.0))
            if score >= thr:
                passes += 1
        summary["quality_pass_percent"][metric] = {
            "threshold": thr,
            "pass_percent": _pass_pct(passes, len(scored)),
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

        if y_true_intent:
            labels = sorted(set(y_true_intent) | set(y_pred_intent))
            pr = _precision_recall(y_true_intent, y_pred_intent, labels)
            acc = sum(1 for t, p in zip(y_true_intent, y_pred_intent) if t == p) / len(y_true_intent)
            summary["classification"]["intent"] = {
                "accuracy": acc,
                "per_label": {k: {"precision": v.precision, "recall": v.recall} for k, v in pr.items()},
            }

        if y_true_domain:
            labels = sorted(set(y_true_domain) | set(y_pred_domain))
            pr = _precision_recall(y_true_domain, y_pred_domain, labels)
            acc = sum(1 for t, p in zip(y_true_domain, y_pred_domain) if t == p) / len(y_true_domain)
            summary["classification"]["domain_flag"] = {
                "accuracy": acc,
                "per_label": {k: {"precision": v.precision, "recall": v.recall} for k, v in pr.items()},
            }

        if y_true_safety:
            labels = sorted(set(y_true_safety) | set(y_pred_safety))
            pr = _precision_recall(y_true_safety, y_pred_safety, labels)
            acc = sum(1 for t, p in zip(y_true_safety, y_pred_safety) if t == p) / len(y_true_safety)
            summary["classification"]["safety_flag"] = {
                "accuracy": acc,
                "per_label": {k: {"precision": v.precision, "recall": v.recall} for k, v in pr.items()},
            }

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Run eval metrics and report pass% per metric.")
    parser.add_argument("--agent", choices=["orchestrator", "design_agent", "all"], default="all")
    parser.add_argument("--case-id", help="Run only one eval case by id.")
    parser.add_argument("--out", help="Write full JSON report to this path.")
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Skip LLM-based quality judging (still computes deterministic classification where possible).",
    )
    args = parser.parse_args()

    agents: list[AgentName] = ["orchestrator", "design_agent"] if args.agent == "all" else [args.agent]  # type: ignore[list-item]
    report = {"generated_at": str(Path.cwd()), "agents": {}}
    for agent in agents:
        report["agents"][agent] = run_dataset(agent, case_id=args.case_id, use_llm=(not args.no_llm))

    text = json.dumps(report, ensure_ascii=True, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
