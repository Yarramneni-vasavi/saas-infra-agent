"""Optional LLM judge: does the generated IaC faithfully implement the plan?

Deterministic checks catch structure and hygiene; the judge grades semantic
fidelity (right services, sizing, constraints honored). Uses the project's
own LLM factory, so it costs extra tokens — enabled with `--judge`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage

_JUDGE_SYSTEM = """You grade Infrastructure-as-Code output against an
architecture document. Score how faithfully the artifacts implement the
document: right services, right sizing, constraints honored (variables vs
hardcoding, tagging, no fabricated secrets), nothing significant missing,
nothing significant invented.

Return ONLY compact JSON: {"score": 0.0-1.0, "verdict": "pass"|"fail",
"issues": ["<short issue>", ...]}. "pass" means score >= 0.7."""

MAX_ARTIFACT_CHARS = 40_000


@dataclass
class JudgeResult:
    score: float
    verdict: str
    issues: list[str]
    error: str | None = None


def _collect_artifacts(workspace: Path) -> str:
    artifacts = workspace / "artifacts"
    parts = []
    for f in sorted(artifacts.rglob("*")):
        if f.is_file():
            parts.append(f"--- {f.relative_to(workspace)} ---\n{f.read_text(errors='ignore')}")
    return "\n\n".join(parts)[:MAX_ARTIFACT_CHARS]


def judge_fidelity(workspace: Path) -> JudgeResult:
    from saas_infra_agent.llm.factory import get_llm

    arch = None
    for name in ("architecture.md", "arch.md"):
        if (workspace / name).is_file():
            arch = (workspace / name).read_text()
            break
    if arch is None:
        return JudgeResult(0.0, "fail", [], error="no architecture doc in workspace")

    artifacts = _collect_artifacts(workspace)
    if not artifacts:
        return JudgeResult(0.0, "fail", ["no artifacts generated"])

    try:
        resp = get_llm().invoke([
            SystemMessage(content=_JUDGE_SYSTEM),
            HumanMessage(content=f"# Architecture document\n{arch}\n\n# Generated artifacts\n{artifacts}"),
        ])
        text = resp.content.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
        data = json.loads(text)
        return JudgeResult(
            score=float(data.get("score", 0.0)),
            verdict=data.get("verdict", "fail"),
            issues=list(data.get("issues", [])),
        )
    except Exception as exc:
        return JudgeResult(0.0, "fail", [], error=f"{type(exc).__name__}: {exc}")
