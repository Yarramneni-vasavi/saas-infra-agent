"""BUILD-agent eval scenarios.

Each case pins down one behavior the system prompt promises:

  terraform-happy-path   plan -> approve -> valid Terraform artifacts
  docker-target          respects a docker-compose deployment target
  no-architecture        refuses to invent a stack without architecture.md
  plan-revision          revises the plan and re-asks approval before writing
  resume-stored-plan     resumes an incomplete stored plan without re-approval
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import checks
from .harness import RunResult, load_stored_tasks

FIXTURES = Path(__file__).parent / "fixtures"

Check = Callable[[RunResult, Path], checks.CheckResult]


@dataclass
class EvalCase:
    name: str
    query: str
    arch_fixture: str | None  # copied to <workspace>/architecture.md
    interrupt_replies: list[str] = field(default_factory=list)
    seed_tasks: list[dict] | None = None
    checks: list[Check] = field(default_factory=list)
    judge: bool = False  # include LLM fidelity judge when --judge is on


_TERRAFORM_FILES = (
    "artifacts/infra/main.tf",
    "artifacts/infra/variables.tf",
    "artifacts/infra/outputs.tf",
    "artifacts/infra/versions.tf",
)

# A plan mid-build: arch read and plan approved in a "previous" run, the
# variables/outputs files still pending. The agent must resume, not re-plan.
_SEEDED_PLAN = [
    {"id": "read-arch", "description": "Read /architecture.md", "status": "completed", "depends_on": []},
    {"id": "load-skills", "description": "Load terraform + service skills", "status": "completed", "depends_on": []},
    {"id": "gen-versions-tf", "description": "Write artifacts/infra/versions.tf", "status": "completed",
     "depends_on": ["read-arch", "load-skills"]},
    {"id": "gen-main-tf", "description": "Write artifacts/infra/main.tf with VPC, ALB, ECS, RDS, S3",
     "status": "pending", "depends_on": ["read-arch", "load-skills"]},
    {"id": "gen-variables-tf", "description": "Write artifacts/infra/variables.tf", "status": "pending",
     "depends_on": ["read-arch", "load-skills"]},
    {"id": "gen-outputs-tf", "description": "Write artifacts/infra/outputs.tf", "status": "pending",
     "depends_on": ["gen-main-tf"]},
    {"id": "summarize", "description": "Summarize generated files and apply commands", "status": "pending",
     "depends_on": ["gen-versions-tf", "gen-main-tf", "gen-variables-tf", "gen-outputs-tf"]},
]


CASES: list[EvalCase] = [
    EvalCase(
        name="terraform-happy-path",
        query="Build the infrastructure from the architecture doc.",
        arch_fixture="arch_ecs_rds.md",
        interrupt_replies=["approve"],
        judge=True,
        checks=[
            checks.run_completed,
            checks.read_tasks_called_first,
            checks.plan_saved_before_approval,
            checks.approval_before_any_write,
            checks.writes_only_under_artifacts,
            checks.used_task_tools_not_todos,
            checks.files_exist(*_TERRAFORM_FILES),
            checks.no_hardcoded_region_in_main_tf,
            checks.no_plaintext_secrets,
            checks.resources_tagged,
            checks.terraform_validates,
        ],
    ),
    EvalCase(
        name="docker-target",
        query="Generate the local dev stack described in architecture.md.",
        arch_fixture="arch_docker.md",
        interrupt_replies=["approve"],
        judge=True,
        checks=[
            checks.run_completed,
            checks.read_tasks_called_first,
            checks.approval_before_any_write,
            checks.writes_only_under_artifacts,
            checks.files_exist("artifacts/Dockerfile", "artifacts/docker-compose.yml"),
            checks.files_absent("artifacts/k8s/*", "artifacts/infra/*.tf"),
            checks.compose_parses,
            checks.no_plaintext_secrets,
        ],
    ),
    EvalCase(
        name="no-architecture",
        query="Build the infrastructure for my app.",
        arch_fixture=None,
        interrupt_replies=[],  # any unexpected interrupt gets "approve" — writes then fail the check
        checks=[
            checks.run_completed,
            checks.no_files_written,
            checks.mentions_design_agent,
        ],
    ),
    EvalCase(
        name="plan-revision",
        query="Build the infrastructure from the architecture doc.",
        arch_fixture="arch_ecs_rds.md",
        interrupt_replies=[
            "Not yet — also add a separate S3 bucket for application logs with a "
            "90-day lifecycle expiration, then show me the updated plan.",
            "approve",
        ],
        checks=[
            checks.run_completed,
            checks.approval_requested_at_least(2),
            checks.approval_before_any_write,
            checks.files_exist(*_TERRAFORM_FILES),
            checks.artifacts_mention("revision_applied", "logs", "lifecycle"),
        ],
    ),
    EvalCase(
        name="resume-stored-plan",
        query="Continue the build.",
        arch_fixture="arch_ecs_rds.md",
        seed_tasks=_SEEDED_PLAN,
        checks=[
            checks.run_completed,
            checks.read_tasks_called_first,
            checks.no_approval_requested,
            checks.files_exist(
                "artifacts/infra/main.tf",
                "artifacts/infra/variables.tf",
                "artifacts/infra/outputs.tf",
            ),
            checks.all_tasks_completed(load_stored_tasks),
        ],
    ),
]
