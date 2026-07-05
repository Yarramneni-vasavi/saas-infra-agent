"""Standalone Build agent for isolated testing.

Give it an existing arch.md and an output directory; it reads the plan,
generates Terraform + Docker files, and writes them to the output directory.
No dependency on the rest of the saas_cli package - just this file.

Install deps:
    pip install langchain-openai langgraph pydantic

Usage:
    export OPENAI_API_KEY=sk-...
    python build_agent_standalone.py --arch-md ./arch.md --output-dir ./infra_out

    # or point at a different model / pass the key explicitly:
    python build_agent_standalone.py \\
        --arch-md ./arch.md \\
        --output-dir ./infra_out \\
        --model gpt-4o \\
        --api-key sk-...
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from langchain_openai import ChatOpenAI
from langchain_core.tools import StructuredTool
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel, Field

# ---- Tool: read-only arch.md -----------------------------------------------

def make_read_arch_md_tool(arch_md_path: Path) -> StructuredTool:
    def read_arch_md() -> str:
        """Read the current contents of arch.md."""
        if not arch_md_path.exists():
            return "No arch.md exists yet at this path. This is a fresh project."
        return arch_md_path.read_text()

    return StructuredTool.from_function(
        func=read_arch_md,
        name="read_arch_md",
        description=read_arch_md.__doc__ or "",
    )


# ---- Tool: sandboxed write_file for IaC output -----------------------------

_ALLOWED_SUFFIXES = {".tf", ".tfvars", ".yaml", ".yml", ".json", ".sh", ".md", ".env.example"}
_ALLOWED_BARE_NAMES = {"Dockerfile", ".dockerignore", ".gitignore"}
_MAX_CONTENT_CHARS = 20_000


class WriteFileInput(BaseModel):
    relative_path: str = Field(
        description="Path relative to the output root, e.g. 'infra/main.tf' or 'Dockerfile'."
    )
    content: str = Field(description="Full file contents to write.")


def _within_root(root: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _is_allowed_name(path: Path) -> bool:
    return path.name in _ALLOWED_BARE_NAMES or path.suffix in _ALLOWED_SUFFIXES


def make_write_file_tool(output_root: Path) -> StructuredTool:
    def write_file(relative_path: str, content: str) -> str:
        """Write a Terraform, Docker, or config file to the output directory."""
        target = (output_root / relative_path).resolve()

        if not _within_root(output_root, target):
            return "Refused: path is outside the output root."
        if not _is_allowed_name(target):
            return (
                f"Refused: '{target.name}' isn't a recognized infra config file type. "
                "Use .tf, .tfvars, .yml/.yaml, .json, .sh, Dockerfile, .dockerignore, "
                ".gitignore, .env.example, or .md."
            )
        if len(content) > _MAX_CONTENT_CHARS:
            return f"Refused: content is {len(content)} chars, over the {_MAX_CONTENT_CHARS} limit."

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return f"Wrote {len(content)} chars to {relative_path}"

    return StructuredTool.from_function(
        func=write_file,
        name="write_file",
        description=write_file.__doc__ or "",
        args_schema=WriteFileInput,
    )


# ---- Agent ------------------------------------------------------------------

SYSTEM_PROMPT = """You are the Build agent for an infrastructure-recommendation SaaS.

You turn an already-agreed plan into runnable Infrastructure as Code. You do not
decide the stack - that's the Plan agent's job.

Steps:
1. Call read_arch_md to get the current recommended stack, cost, and requirements.
2. If arch.md says no plan exists yet, stop and tell the user a plan is needed first -
   do not invent a stack yourself.
3. Translate the recommended stack into IaC, split sensibly:
   - infra/main.tf: cloud resources (compute, storage, networking, managed DB/vector DB)
   - infra/variables.tf: inputs like region, instance size, environment - use variables
     for anything that should be tunable, don't hardcode values that vary by deploy
   - infra/outputs.tf: useful outputs (endpoints, connection strings/ARNs)
   - Dockerfile: for the application/service layer, if the stack implies one
   - docker-compose.yml: for local dev / multi-service orchestration, if useful
4. Call write_file once per file. Use sensible defaults so `terraform plan` and
   `docker compose up` would work out of the box, but don't fabricate real
   credentials - use variables or placeholder env vars for secrets.
5. Your final reply should be a short list of the files you generated and the two
   commands to apply them (terraform init/plan/apply, docker compose up) - not the
   full file contents again.
"""


def run_build_agent(arch_md_path: Path, output_dir: Path, model_name: str, api_key: str) -> str:
    model = ChatOpenAI(model=model_name, api_key=api_key)
    tools = [
        make_read_arch_md_tool(arch_md_path),
        make_write_file_tool(output_dir),
    ]
    agent = create_react_agent(model, tools, prompt=SYSTEM_PROMPT)

    final_content = ""
    try:
        # Stream instead of invoke so each model/tool step prints as it
        # happens - if the run stops early or errors out, you can see
        # exactly which step it got to instead of just an empty result.
        for step in agent.stream(
            {"messages": [{"role": "user", "content": "Generate the infrastructure for the current plan."}]},
            config={"recursion_limit": 50},
            stream_mode="updates",
        ):
            for node_name, node_output in step.items():
                messages = node_output.get("messages", [])
                for msg in messages:
                    msg_type = type(msg).__name__
                    tool_calls = getattr(msg, "tool_calls", None)
                    if tool_calls:
                        for tc in tool_calls:
                            print(f"  [{node_name}] tool call: {tc['name']}({tc['args']})")
                    content = getattr(msg, "content", None)
                    if content and msg_type != "HumanMessage":
                        text = content if isinstance(content, str) else str(content)
                        print(f"  [{node_name}] {msg_type}: {text[:300]}")
                    if messages:
                        last = messages[-1]
                        c = getattr(last, "content", "")
                        if isinstance(c, list):
                            c = "\n".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in c)
                        final_content = str(c) or final_content
    except Exception as e:
        print(f"\n!!! Agent run raised an exception: {e!r}")
        print("(Any files written before the error are still on disk - check the output directory.)")
        raise

    return final_content


# ---- CLI ---------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Standalone Build agent test runner")
    parser.add_argument("--arch-md", required=True, help="Path to an existing arch.md to build from")
    parser.add_argument("--output-dir", default="./infra_out", help="Where to write generated IaC files")
    parser.add_argument("--model", default="gpt-4o", help="Model string to use")
    parser.add_argument("--api-key", default=None, help="OpenAI API key (default: $OPENAI_API_KEY)")
    args = parser.parse_args()

    api_key = args.api_key or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("Error: no API key. Pass --api-key or set OPENAI_API_KEY.", file=sys.stderr)
        sys.exit(1)

    arch_md_path = Path(args.arch_md)
    if not arch_md_path.exists():
        print(f"Error: {arch_md_path} does not exist.", file=sys.stderr)
        sys.exit(1)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Reading plan from: {arch_md_path}")
    print(f"Writing IaC to:    {output_dir}\n")

    summary = None
    try:
        summary = run_build_agent(arch_md_path, output_dir, args.model, api_key)
    except Exception:
        print("\n(Run did not finish cleanly - see exception above.)")

    if summary:
        print("\n--- Agent summary ---")
        print(summary)

    print("\n--- Files written ---")
    written = [p for p in sorted(output_dir.rglob("*")) if p.is_file()]
    if not written:
        print("  (none)")
    for p in written:
        print(f"  {p.relative_to(output_dir)}")

    if summary is None:
        sys.exit(1)


if __name__ == "__main__":
    main()