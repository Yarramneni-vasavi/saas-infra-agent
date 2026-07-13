# Agent Evals

This folder contains golden datasets for the router and design workflow.

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
- `run_deepeval.py`: runs the golden datasets against local code and scores
  them with DeepEval `GEval`.
- `run_metrics.py`: runs the datasets and outputs % pass for
  `answer_relevancy`, `faithfulness`, `factual_correctness` (LLM-judged), plus
  deterministic precision/recall-style stats for orchestrator intent/domain/safety
  when `expected_*` fields are present in the dataset.

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

Run metrics report (prints JSON):

```bash
python -m saas_infra_agent.evals.run_metrics --agent all --out .tmp_evals/metrics_report.json
```

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
