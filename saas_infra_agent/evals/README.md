# Agent Evals

This folder contains golden datasets for the router, design workflow, and
build workflow.

There is no repo-wide eval runner yet, so the datasets are intentionally
plain JSON and easy to adapt to LangSmith, OpenAI Evals, or a custom harness.

This folder also includes a local DeepEval runner:
`saas_infra_agent/evals/run_deepeval.py`.
It also includes a lightweight metrics runner:
`saas_infra_agent/evals/run_metrics.py`.

## Files

- `orchestrator/evals.json`: single-turn routing, safety-gate, and domain-gate
  checks for `saas_infra_agent/agent/orchestrator.py`.
- `design_agent/evals.json`: design-workflow checks for
  `saas_infra_agent/agent/design_flow.py`.
- `build_agent/evals.json`: build-workflow checks for
  `saas_infra_agent/agent/build_agent.py` (plan approval gate, artifact
  generation, plan revision, resuming a stored DAG plan, secrets hygiene).
- `run_deepeval.py`: runs the golden datasets against local code and scores
  them with DeepEval `GEval`.
- `run_metrics.py`: runs the datasets and reports metrics **per question and
  overall**. Per case it scores `answer_relevancy`, `faithfulness`,
  `factual_correctness` (LLM-judged, threshold 0.70 each) plus a
  `deterministic` check score built from the case's `expected_*` fields
  (routing flags for the orchestrator; interrupt kind, expected/forbidden
  files, and task-plan persistence for the build agent). The overall section
  aggregates avg score and pass % per metric, the % of cases passing every
  metric, and precision/recall for orchestrator intent/domain/safety.

## Run

Install the optional eval dependency group:

```bash
poetry install --with evals
```

Run all evals:

```bash
poetry run saas-evals
```

Run one dataset:

```bash
poetry run saas-evals --agent orchestrator
poetry run saas-evals --agent design_agent
```

Run one case:

```bash
poetry run saas-evals --agent orchestrator --case-id orchestrator-010
```

List available ids:

```bash
poetry run saas-evals --list
```

Run metrics report (prints a per-case table plus overall aggregates; `--out`
writes the full JSON report, `--json` also prints it):

```bash
python -m saas_infra_agent.evals.run_metrics --agent all --out .tmp_evals/metrics_report.json
python -m saas_infra_agent.evals.run_metrics --agent build_agent
python -m saas_infra_agent.evals.run_metrics --agent build_agent --case-id build-001 --no-llm
```

Note: build_agent cases run the real deep agent end to end (several LLM
calls per case; `approve`-mode cases execute the full build), so they are
slower and costlier than orchestrator/design cases.

## Notes

- `OPENAI_API_KEY` is required for the agent under test and for DeepEval's
  LLM-as-a-judge scoring.
- Eval runs are isolated under `.tmp_evals/`, so temporary `pdr.md` and SQLite
  files do not pollute the repo root.

## Design Agent Schema

The design agent is interrupt-driven, so its dataset supports two modes:

- `invoke`: start or continue the graph with a normal user message.
- `resume`: resume a waiting interrupt with a human reply and preloaded state.

Useful fields:

- `state_setup`: minimal state to preload before running the eval.
- `resume_kind`: the interrupt kind being resumed (`clarify`,
  `requirements_confirm`, `architecture_feedback`, or `approve`).
- `input`: the initial user message or resume reply.
- `expectations`: checklist items for the grader.

## Build Agent Schema

The build agent pauses on a `request_plan_approval` interrupt before writing
artifacts, so its dataset supports two modes:

- `invoke`: send one user message and run until the agent finishes or pauses
  on the plan-approval interrupt.
- `approve`: same, then resume the interrupt with `approval_reply` (an
  approval executes the build; a change request should trigger a revised plan
  and a second interrupt).

Useful fields:

- `files_setup`: files (e.g. `pdr.md`) written into the isolated workspace
  before the run.
- `task_plan_setup`: a stored DAG task plan preloaded into the task store to
  test resume-from-plan behavior.
- `expected_interrupt`: interrupt type the run should end paused on
  (`build_plan_approval`), or `null` when the run should finish.
- `expected_files` / `forbidden_files`: artifacts that must / must not exist
  in the workspace after the run.
- `expects_task_plan`: whether a plan must be persisted via `write_tasks`.
- `expectations`: checklist items for the grader.
