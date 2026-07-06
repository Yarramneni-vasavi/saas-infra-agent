from __future__ import annotations

from pathlib import Path

import yaml
from langchain.tools import tool

from saas_infra_agent.agent.skills_loader import discover_skill_dirs
from saas_infra_agent.observability.logger import get_logger

logger = get_logger(__name__)

# saas_infra_agent/skills — the packaged skills library.
SKILLS_ROOT = Path(__file__).resolve().parents[2] / "skills"

_MAX_FILE_CHARS = 50_000


def _parse_frontmatter(skill_md: str) -> dict:
    if not skill_md.startswith("---"):
        return {}
    parts = skill_md.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return {}
    return meta if isinstance(meta, dict) else {}


def _skill_index() -> dict[str, Path]:
    """Map skill name -> skill directory. First discovery wins on duplicates."""
    index: dict[str, Path] = {}
    for skill_dir in discover_skill_dirs(SKILLS_ROOT):
        index.setdefault(skill_dir.name, skill_dir.root_dir)
    return index


def _skill_description(skill_root: Path) -> str:
    try:
        meta = _parse_frontmatter((skill_root / "SKILL.md").read_text(encoding="utf-8"))
    except OSError:
        return ""
    description = meta.get("description")
    if not isinstance(description, str):
        return ""
    # Some skills carry multi-paragraph trigger text; keep the listing scannable.
    first_line = description.strip().splitlines()[0]
    return first_line[:300] + ("..." if len(first_line) > 300 else "")


@tool
def list_skills() -> str:
    """
    List every available skill as 'name — description'.

    Call this first to see which skills exist, then use load_skill on the
    relevant ones before generating any artifacts.
    """
    logger.info("Tool called: list_skills")
    index = _skill_index()
    if not index:
        return f"No skills found under {SKILLS_ROOT}."
    lines = [f"- {name} — {_skill_description(root) or '(no description)'}" for name, root in sorted(index.items())]
    return "Available skills:\n" + "\n".join(lines)


@tool
def load_skill(name: str) -> str:
    """
    Load a skill's full SKILL.md instructions by name (as shown by list_skills).

    The reply also lists the skill's bundled reference files, which can be read
    with read_skill_file.
    """
    logger.info(f"Tool called: load_skill name={name!r}")
    root = _skill_index().get(name)
    if root is None:
        return f"Unknown skill {name!r}. Call list_skills to see valid names."

    body = (root / "SKILL.md").read_text(encoding="utf-8")
    extras = sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and p.name != "SKILL.md"
    )
    if extras:
        listing = "\n".join(f"- {rel}" for rel in extras)
        body += f"\n\n---\nReference files in this skill (use read_skill_file('{name}', <path>)):\n{listing}"
    return body


@tool
def read_skill_file(name: str, relative_path: str) -> str:
    """
    Read a reference file bundled with a skill, e.g. read_skill_file('terraform-module-library', 'references/aws-modules.md').

    Safety: only allows relative paths inside the named skill's directory.
    """
    logger.info(f"Tool called: read_skill_file name={name!r} path={relative_path!r}")
    root = _skill_index().get(name)
    if root is None:
        return f"Unknown skill {name!r}. Call list_skills to see valid names."

    candidate = Path(relative_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        return "Refused: path must be relative and must not contain '..'."

    target = (root / candidate).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return "Refused: path escapes the skill directory."
    if not target.is_file():
        return f"No file {relative_path!r} in skill {name!r}. load_skill lists the available files."

    text = target.read_text(encoding="utf-8", errors="replace")
    if len(text) > _MAX_FILE_CHARS:
        text = text[:_MAX_FILE_CHARS] + "\n... (truncated)"
    return text
