# SaaS Infra Agent

An AI agent platform that turns a plain-language requirement ("we need a RAG
pipeline for 10,000 daily users with sub-2-second latency") into a designed,
provisioned, and monitored cloud stack.

Three agents, coordinated by a router ([agent/orchestrator.py](saas_infra_agent/agent/orchestrator.py)):

| Agent   | Job                                                                                  |
|---------|--------------------------------------------------------------------------------------|
| DESIGN  | Clarifies requirements, recommends a stack + cost, writes `pdr.md`           |
| BUILD   | Turns the approved `pdr.md` into IaC (Terraform, Dockerfile, compose, k8s)   |
| MONITOR | Metrics, token usage, cost analysis, optimization recommendations                     |

The BUILD agent is a long-running [deepagents](https://pypi.org/project/deepagents/)
deep agent: it plans the build with a todo list, loads the skills library
([saas_infra_agent/skills/](saas_infra_agent/skills/)) via progressive disclosure
(per-service AWS skills, `terraform-module-library`, `cost-optimization`,
workload patterns), and writes artifacts through sandboxed filesystem tools —
writes are permission-limited to the `artifacts/` directory, and `.env`/`.git`
are unreadable to the model.

## Setup

Requires Python 3.11+ and [Poetry](https://python-poetry.org/).

```bash
cd saas-infra-agent
poetry install
```

> The project needs `langchain >= 1.3.11` (pulled in by `poetry install`).
> Running against an older globally-installed langchain will fail at agent
> creation inside the middleware.

### Environment

Copy the example env file and fill in your keys:

```bash
cp .env.example .env
```

| Variable                        | Needed for                                            |
|---------------------------------|-------------------------------------------------------|
| `OPENAI_API_KEY`                | Required — all agents (models set in `saas_infra_agent/config.yaml`) |
| `TAVILY_API_KEY`                | MONITOR agent web search                              |
| `QDRANT_URL`, `QDRANT_API_KEY`  | `search_codebase` tool (Qdrant cloud)                 |
| `LANGSMITH_API_KEY`             | Optional tracing                                      |

Never commit `.env` — keep real keys out of `.env.example` too.

## Run

```bash
poetry run saas-cli
```

It's a REPL. The router picks an agent from your message, or force one with a
prefix:

```
> we need a RAG pipeline for 10,000 daily users    ← routes to DESIGN
> /build generate the infra                        ← forces BUILD
> /monitor what's our token spend?                 ← forces MONITOR
```

Session commands: `/new`, `/switch <id>`, `/session`, `/exit`.

### Typical flow

1. Describe the project — the DESIGN agent asks clarifying questions and, once
   approved, saves `pdr.md`.
2. Say "build it" — the BUILD agent reads `pdr.md`, loads the relevant
   skills, and writes IaC into the `artifacts/` directory.
3. Apply the output: `terraform init/plan/apply`, `docker compose up`.

## Project layout

```
saas-infra-agent/
├── architecture.md            # Output of the DESIGN agent
├── artifacts/                 # Output of the BUILD agent (e.g. infra/ Terraform)
├── .memory/                   # SQLite memory.db, session + orchestrator state
└── saas_infra_agent/
    ├── main.py                # CLI entry point (poetry run saas-cli)
    ├── config.yaml            # LLM models, memory, limits, MCP, vector store
    ├── config/                # config.yaml loader
    ├── llm/                   # LLM factory (models per agent from config.yaml)
    ├── agent/
    │   ├── orchestrator.py    # Router: design | build | monitor
    │   ├── safetygate.py      # Per-turn safety gate (regex pre-filter + LLM)
    │   ├── design_agent.py    # Interrupt-driven requirements workflow
    │   ├── design_flow.py     # LangGraph state machine for the design flow
    │   ├── build_agent.py     # Plan → IaC deep agent (todos, skills, sandboxed fs)
    │   ├── agents.py          # AgentKind + monitor agent + get_agent()
    │   ├── skills_catalog.py  # Skill discovery/catalog for progressive disclosure
    │   ├── skills_loader.py   # Loads SKILL.md content on demand
    │   ├── middleware/        # Model/tool call limits
    │   └── tools/             # read/write/search/plan-approval tools
    ├── skills/                # Skills library (SKILL.md per skill)
    │   ├── workloads/         # rag, microservices, monolith, serverless-api, ...
    │   ├── aws-agent-skills/  # per-AWS-service guidance (iam, ecs, lambda, ...)
    │   ├── terraform-module-library/
    │   └── cost-optimization/
    ├── memory/                # SQLite checkpointer, sessions, task store (build DAG)
    ├── context/               # Codebase indexing + retrieval (Qdrant hybrid / graph)
    ├── mcp/                   # MCP server registry + sync tool loading
    └── observability/         # Logging setup
```
