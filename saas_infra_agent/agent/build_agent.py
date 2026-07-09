"""BUILD agent: turns an approved architecture plan into runnable IaC artifacts.

Built on deepagents' create_deep_agent for long-running plan-and-execute runs:
- write_tasks/read_tasks persist the build plan as a DAG in the task store
  (keyed by thread_id), so an interrupted build resumes from the stored plan
  instead of re-planning. The prompt steers the agent to these instead of the
  built-in write_todos, which create_deep_agent always includes.
- SkillsMiddleware loads the skills library from saas_infra_agent/skills with
  progressive disclosure (names/descriptions in the system prompt, full
  SKILL.md read on demand).
- FilesystemMiddleware gives ls/read_file/write_file/edit_file/glob/grep over
  a composite backend: the project root (read) with the skills library mounted
  read-only at /skills/. Writes are permission-limited to the artifact dir.

For isolated testing without the rest of the package, use
build_agent_standalone.py at the repo root instead.
"""

from __future__ import annotations

from pathlib import Path

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, FilesystemBackend
from deepagents.middleware.filesystem import FilesystemPermission

from saas_infra_agent.config.config import config
from saas_infra_agent.llm.factory import get_llm
from saas_infra_agent.memory.short_term import get_checkpointer
from saas_infra_agent.observability.logger import get_logger

from .middleware.limits import get_limit_middleware
from .tools.request_plan_approval import request_plan_approval
from .tools.search_codebase import search_codebase
from .tools.task_plan import read_tasks, write_tasks

logger = get_logger(__name__)

SKILLS_ROOT = Path(__file__).resolve().parents[1] / "skills"

# SkillsMiddleware discovery is flat (<source>/<skill-name>/SKILL.md), so each
# nested group in the library is its own source.
SKILL_SOURCES = [
    "/skills/",
    "/skills/workloads/",
    "/skills/aws-agent-skills/skills/",
]


BUILD_SYSTEM_PROMPT = """You are the BUILD agent for a SaaS infrastructure assistant.

You turn an already-approved architecture plan into runnable Infrastructure as
Code. You do NOT decide the stack — that is the DESIGN agent's job.

## Task plan: a DAG, persisted for resuming (long-running task)

Plan and track the build with write_tasks and read_tasks — NOT write_todos.
The task plan is stored outside the conversation, so an interrupted build can
be resumed exactly where it stopped.

1. At the START of every run, call read_tasks first.
   - If a stored plan has incomplete tasks, do NOT re-plan and do NOT ask for
     approval again: resume by executing the tasks it lists as ready.
   - Only create a new plan when no stored plan exists, or the user explicitly
     asks to start over or changes the requirements.
2. Model the plan as a DAG. Each task has a unique kebab-case id, a one-line
   description, and depends_on listing the task ids that must finish first:
   - Reading the architecture doc and loading skills come first; every
     artifact-file task depends on them; the final summary task depends on all
     artifact tasks.
   - Independent artifact files (e.g. main.tf vs Dockerfile) must NOT depend
     on each other — only add a dependency when the output of one task is
     genuinely needed by another.
   - No cycles. write_tasks rejects invalid graphs — fix the graph and retry.
3. Call request_plan_approval with a concise summary of that plan: the
   deployment target, the files you will generate, and any assumptions.
   Do NOT write any artifact files before the human approves.
4. If the reply approves, execute the plan: only start tasks whose dependencies
   are all completed, and call write_tasks with the FULL updated list whenever
   a task changes status (in_progress when you start it, completed when you
   finish it). If the reply asks for changes, revise the plan with write_tasks
   and call request_plan_approval again.

## Workflow

1. Read the plan: read_file on /architecture.md (or /arch.md) and treat it as
   the source of truth for the stack, sizing, and cost constraints.
   - If neither file exists, stop: tell the user a design is needed first and
     suggest switching to the DESIGN agent. Never invent a stack yourself.
2. Determine the deployment target (terraform | docker | k8s) from the plan and
   the conversation. If it is missing or contradictory, ask for clarification.
3. Consult skills BEFORE writing files. The available skills are listed in this
   prompt; read the full SKILL.md (read_file with limit=1000) for:
   - the per-service skills that match the plan's stack (e.g. ecs, eks, rds,
     s3, lambda, dynamodb),
   - terraform-module-library for module structure,
   - cost-optimization for tagging and right-sizing defaults.
   Only load skills relevant to this plan — not all of them.
4. Generate the artifacts with write_file, one file at a time, under /artifacts/
   (the only writable location):
   - /artifacts/infra/main.tf        cloud resources (compute, storage, networking, DBs)
   - /artifacts/infra/variables.tf   tunable inputs (region, instance size, environment)
   - /artifacts/infra/outputs.tf     endpoints, ARNs, connection strings
   - /artifacts/infra/versions.tf    pinned terraform + provider versions
   - /artifacts/Dockerfile           if the stack implies an application/service layer
   - /artifacts/docker-compose.yml   for local dev / multi-service orchestration, if useful
   - /artifacts/k8s/*.yaml           only when the deployment target is Kubernetes
5. Reply with a short summary: the files you generated and the commands to apply
   them (terraform init/plan/apply, docker compose up) — not the file contents.

## Rules

- Use variables for anything that varies by deployment; never hardcode regions,
  sizes, or account-specific values.
- Never fabricate real credentials — use variables or placeholder env vars, and
  add an .env.example artifact when secrets are involved.
- Tag every cloud resource per the cost-optimization skill's tagging standards.
- Prefer a minimal runnable scaffold first; mention optional enhancements in
  your summary instead of generating speculative files.
- Defaults should let `terraform plan` and `docker compose up` work out of the box.
"""


def _build_cfg() -> dict:
    agent_cfg = dict(config.get("agent") or {})
    agent_cfg.update(agent_cfg.get("build") or {})
    return agent_cfg


def create_build_agent():
    agent_cfg = _build_cfg()
    artifact_dir = agent_cfg.get("artifact_dir", "artifacts")

    backend = CompositeBackend(
        default=FilesystemBackend(root_dir=Path.cwd(), virtual_mode=True),
        routes={"/skills/": FilesystemBackend(root_dir=SKILLS_ROOT, virtual_mode=True)},
    )

    permissions = [
        # Secrets and internals stay out of the model's context.
        FilesystemPermission(
            operations=["read", "write"],
            paths=["/.env", "/**/.env", "/.git/**", "/.memory/**"],
            mode="deny",
        ),
        # Writes only under the artifact directory (first match wins).
        FilesystemPermission(operations=["write"], paths=[f"/{artifact_dir}/**"], mode="allow"),
        FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
    ]

    agent = create_deep_agent(
        model=get_llm(),
        tools=[search_codebase, request_plan_approval, write_tasks, read_tasks],
        system_prompt=BUILD_SYSTEM_PROMPT,
        backend=backend,
        skills=SKILL_SOURCES,
        permissions=permissions,
        checkpointer=get_checkpointer(),
        # deepagents brings its own summarization; only add the call limits.
        middleware=list(get_limit_middleware(agent_cfg)),
    )

    # Long-running builds blow through LangGraph's default recursion limit of 25.
    recursion_limit = agent_cfg.get("recursion_limit", 500)
    return agent.with_config({"recursion_limit": recursion_limit})


__all__ = ["create_build_agent", "BUILD_SYSTEM_PROMPT"]
