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
  read-only at /skills/. Writes are permission-limited to configured output paths.

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
from .tools.terraform_validate import terraform_validate
from .tools.terminal_tools import run_command, run_in_directory

logger = get_logger(__name__)

SKILLS_ROOT = Path(__file__).resolve().parents[1] / "skills"

# SkillsMiddleware discovery is flat (<source>/<skill-name>/SKILL.md), so each
# nested group in the library is its own source.
SKILL_SOURCES = [
    "/skills/",
    "/skills/workloads/",
    "/skills/aws-agent-skills/skills/",
]

def _build_system_prompt_compact(pdr_paths_hint: str) -> str:
    return f"""You are the BUILD agent for a SaaS infrastructure assistant.

You turn an approved architecture plan into runnable Infrastructure as Code.
You do NOT decide the stack; that is the DESIGN agent's job.

Plan + approval:
- Always call read_tasks first.
- If a stored plan exists with incomplete tasks: resume it (no re-planning).
- Otherwise write a new DAG plan with write_tasks, then call request_plan_approval.
- Before approval: do not write artifacts and do not run commands.

In request_plan_approval, explicitly mention you will run local validation:
`terraform init -backend=false` and `terraform validate`, and fix errors until
validation succeeds.

Workflow:
1. Read {pdr_paths_hint}. If it doesn't exist, stop and ask for DESIGN first.
2. Load only relevant skills before writing any files.
3. Generate minimal runnable artifacts (Terraform under /infra unless told otherwise).
   - Pin versions for every Terraform Registry module you use (add `version =`).
   - Keep module inputs consistent with the pinned major version (avoid deprecated/renamed args).
   - Keep provider/Terraform version constraints compatible with the modules you selected.
4. Validate Terraform (no apply):
   - Call terraform_validate.
   - If it reports errors, fix the Terraform files and call terraform_validate again.
   - Repeat until validate passes or you hit an external blocker.
5. Reply with a short summary: files generated and how to run locally.

GitHub publishing is handled by a separate PUBLISH step. Tell the user to run
`/publish owner/repo` after the build if they want a PR created.
  """


BUILD_SYSTEM_PROMPT = _build_system_prompt_compact("/pdr.md")


def _build_cfg() -> dict:
    agent_cfg = dict(config.get("agent") or {})
    agent_cfg.update(agent_cfg.get("build") or {})
    return agent_cfg


def create_build_agent():
    agent_cfg = _build_cfg()
    artifact_dir = str(agent_cfg.get("artifact_dir", ".") or ".").strip()
    artifact_dir_norm = artifact_dir.strip().strip("/")

    backend = CompositeBackend(
        default=FilesystemBackend(root_dir=Path.cwd(), virtual_mode=True),
        routes={"/skills/": FilesystemBackend(root_dir=SKILLS_ROOT, virtual_mode=True)},
    )

    if artifact_dir_norm in {"", "."}:
        write_allow_paths = [
            "/infra/**",
            "/k8s/**",
            "/Dockerfile",
            "/docker-compose.yml",
            "/compose.yaml",
            "/.env.example",
        ]
    else:
        write_allow_paths = [f"/{artifact_dir_norm}/**"]

    permissions = [
        # Secrets and internals stay out of the model's context.
        FilesystemPermission(
            operations=["read", "write"],
            paths=["/.env", "/**/.env", "/.git/**", "/.memory/**"],
            mode="deny",
        ),
        # Writes only under the allowed output paths (first match wins).
        FilesystemPermission(operations=["write"], paths=write_allow_paths, mode="allow"),
        FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
    ]

    agent = create_deep_agent(
        model=get_llm(),
        tools=[run_command, run_in_directory, request_plan_approval, terraform_validate, write_tasks, read_tasks],
        system_prompt=_build_system_prompt_compact(
            "/pdr.md"
            if artifact_dir_norm in {"", "."}
            else f"/pdr.md (or /{artifact_dir_norm}/pdr.md)"
        ),
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
