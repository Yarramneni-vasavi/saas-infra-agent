# BUILD-agent evals

End-to-end evals for the BUILD agent ([../saas_infra_agent/agent/build_agent.py](../saas_infra_agent/agent/build_agent.py)).
Each case runs the real agent (real LLM calls) inside a throwaway workspace,
auto-answers the `request_plan_approval` interrupt with scripted replies, and
grades the run with deterministic checks plus an optional LLM judge.

## Run

```bash
cd saas-infra-agent
poetry run python -m evals.run_build_evals                 # all cases once
poetry run python -m evals.run_build_evals --only no-architecture
poetry run python -m evals.run_build_evals --runs 3        # pass rate over repeats
poetry run python -m evals.run_build_evals --judge         # + LLM fidelity judge
```

Requires `OPENAI_API_KEY` (loaded from `.env`). GitHub env tokens are stripped
for the run so nothing is ever pushed. If `terraform` is on PATH, generated
Terraform is actually `init -backend=false && validate`d; otherwise that check
is skipped, not failed.

Outputs land in `evals/results/<timestamp>/`: one workspace per run (kept for
inspection of the generated artifacts) and a `report.json` with per-check
detail and the full tool-call sequence.

## What each case pins down

| Case | System-prompt promise under test |
|---|---|
| `terraform-happy-path` | read_tasks first → DAG plan → approval → files only under `artifacts/`, four `.tf` files, no hardcoded region/secrets, tags, `terraform validate` |
| `docker-target` | Respects a docker-compose deployment target: Dockerfile + parseable compose, no stray Terraform/k8s |
| `no-architecture` | With no `architecture.md`, refuses to invent a stack, writes nothing, points at the DESIGN agent |
| `plan-revision` | A "change this" reply re-plans and re-requests approval before any write; the revision shows up in artifacts |
| `resume-stored-plan` | A seeded incomplete plan in the task store is resumed: no re-approval, remaining files generated, all tasks completed |

## How grading works

- **Behavior checks** replay the captured tool-call sequence: ordering
  (`read_tasks` first, `write_tasks` before `request_plan_approval` before any
  `write_file`), approval counts, and write-path hygiene.
- **Artifact checks** inspect the workspace: expected files, YAML/terraform
  validity, secret-pattern and region-literal scans.
- Checks are **required** (gate the case) or **advisory** (reported only —
  e.g. tagging, avoiding `write_todos`). Checks that can't run (no terraform
  binary) are **skipped** and never fail a case.
- `--judge` adds an LLM grader scoring semantic fidelity of the artifacts
  against the architecture doc (advisory, threshold 0.7).

Because the agent is stochastic, treat a single failure as a signal, not a
verdict — rerun with `--runs 3` and look at the pass rate and `report.json`.

## Adding a case

1. Drop a fixture architecture doc in `evals/fixtures/`.
2. Add an `EvalCase` to [cases.py](cases.py): query, scripted interrupt
   replies, optional `seed_tasks` (pre-populates the task store to simulate an
   interrupted build), and a list of checks from [checks.py](checks.py).
3. New graders go in `checks.py` — take `(run, workspace)`, return
   `CheckResult`; use `required=False` for style-level expectations.
