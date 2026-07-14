from __future__ import annotations

import argparse
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from saas_infra_agent.agent.design_flow import (
    DesignFlowState,
    _architecture_prepare,
    _capture_user_message,
    _clarify_prepare_pdr,
    _decisions_prepare,
    _init_or_reset,
    _requirements_prepare,
    _save,
    apply_pdr_user_reply,
)
from saas_infra_agent.agent.domaingate import check_domain
from saas_infra_agent.config.config import config

REPO_ROOT = Path(__file__).resolve().parents[2]
EVALS_ROOT = Path(__file__).resolve().parent
TMP_ROOT = REPO_ROOT / ".tmp_evals"


def _load_deepeval():
    try:
        from deepeval import assert_test
        from deepeval.metrics import GEval
        from deepeval.test_case import LLMTestCase, SingleTurnParams
    except ImportError as exc:  # pragma: no cover - runtime guard only
        raise SystemExit(
            "DeepEval is not installed. Run `poetry install --with evals` first."
        ) from exc
    return assert_test, GEval, LLMTestCase, SingleTurnParams


def _load_dataset(agent_name: str) -> dict[str, Any]:
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
        "is_continuation": getattr(result, "is_continuation", None),
        "requires_clarification": getattr(result, "requires_clarification", None),
        "clarification_question": getattr(result, "clarification_question", None),
        "safety_flag": getattr(result, "safety_flag", None),
        "reasoning": getattr(result, "reasoning", None),
        "reply_preview": _build_orchestrator_reply(domain.flag, result),
    }


def _advance_design_state(state: DesignFlowState) -> DesignFlowState:
    for _ in range(8):
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
        "pdr_ready_for_build": "READY_FOR_BUILD" in pdr_text,
    }


def _build_expected_payload(case: dict[str, Any]) -> str:
    return json.dumps(
        {
            "summary": case.get("expected_output", ""),
            "expectations": case.get("expectations", []),
        },
        ensure_ascii=True,
        indent=2,
    )


def _build_metric(case: dict[str, Any], SingleTurnParams: Any, GEval: Any):
    criteria = (
        "Judge whether the ACTUAL_OUTPUT satisfies the EXPECTED_OUTPUT summary and checklist. "
        "Be strict about routing intent, safety and domain gates, workflow stage transitions, "
        "interrupt kind, and whether build-ready artifacts were produced. "
        "Pass only if the output clearly satisfies the important checklist items."
    )
    threshold = float(case.get("threshold", 0.68))
    return GEval(
        name=f"golden-{case['id']}",
        criteria=criteria,
        evaluation_params=[
            SingleTurnParams.INPUT,
            SingleTurnParams.ACTUAL_OUTPUT,
            SingleTurnParams.EXPECTED_OUTPUT,
        ],
        threshold=threshold,
    )


def _write_actual_output(agent_name: str, case_id: str, payload: dict[str, Any]) -> Path:
    out_dir = TMP_ROOT / "results" / agent_name
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{case_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
    return path


def run_case(agent_name: str, case: dict[str, Any]) -> dict[str, Any]:
    with _isolated_run_env(str(case["id"])) as run_dir:
        if agent_name == "orchestrator":
            actual = _run_orchestrator_case(case)
        elif agent_name == "design_agent":
            actual = _run_design_case(case, run_dir)
        else:
            raise ValueError(f"Unsupported agent dataset: {agent_name}")
        actual["artifacts_dir"] = str(run_dir)
        actual["actual_output_path"] = str(_write_actual_output(agent_name, str(case["id"]), actual))
        return actual


def run_dataset(agent_name: str, case_id: str | None = None) -> int:
    assert_test, GEval, LLMTestCase, SingleTurnParams = _load_deepeval()
    dataset = _load_dataset(agent_name)
    cases = dataset["evals"]
    if case_id is not None:
        cases = [case for case in cases if str(case["id"]) == case_id]

    if not cases:
        raise SystemExit(f"No eval cases matched agent={agent_name!r} case_id={case_id!r}.")

    failures = 0
    for case in cases:
        actual = run_case(agent_name, case)
        test_case = LLMTestCase(
            input=str(case.get("input", case.get("prompt", ""))),
            actual_output=json.dumps(actual, ensure_ascii=True, indent=2),
            expected_output=_build_expected_payload(case),
        )
        metric = _build_metric(case, SingleTurnParams, GEval)
        try:
            assert_test(test_case, [metric])
            print(f"[PASS] {agent_name}:{case['id']}")
        except Exception as exc:
            failures += 1
            print(f"[FAIL] {agent_name}:{case['id']}")
            print(f"  Reason: {exc}")
            print(f"  Actual output: {actual['actual_output_path']}")
    return failures


def main() -> None:
    parser = argparse.ArgumentParser(description="Run golden agent evals with DeepEval.")
    parser.add_argument(
        "--agent",
        choices=["orchestrator", "design_agent", "all"],
        default="all",
        help="Which eval dataset to run.",
    )
    parser.add_argument(
        "--case-id",
        help="Run only one eval case by id.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available eval case ids and exit.",
    )
    args = parser.parse_args()

    agents = ["orchestrator", "design_agent"] if args.agent == "all" else [args.agent]
    if args.list:
        for agent_name in agents:
            dataset = _load_dataset(agent_name)
            print(f"{agent_name}:")
            for case in dataset["evals"]:
                print(f"  - {case['id']}")
        return

    failures = 0
    for agent_name in agents:
        failures += run_dataset(agent_name, case_id=args.case_id)

    if failures:
        raise SystemExit(1)
