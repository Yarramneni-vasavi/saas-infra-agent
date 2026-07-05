from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class SkillDir:
    name: str
    root_dir: Path


def _as_file_data(content: str, encoding: str = "utf-8") -> dict[str, str]:
    # DeepAgents backends expect a FileData dict with at least: {"content": ..., "encoding": ...}
    # (timestamps are optional).
    return {"content": content, "encoding": encoding}


def _parse_skill_name(skill_md: str) -> str | None:
    # Expected format:
    # ---
    # name: foo
    # description: ...
    # ---
    if not skill_md.startswith("---"):
        return None
    parts = skill_md.split("---", 2)
    if len(parts) < 3:
        return None
    _, frontmatter, _ = parts
    meta = yaml.safe_load(frontmatter) or {}
    name = meta.get("name")
    return name if isinstance(name, str) and name.strip() else None


def discover_skill_dirs(local_skills_root: Path) -> list[SkillDir]:
    """Find all skill directories under `local_skills_root` by locating `SKILL.md` files."""
    skill_dirs: list[SkillDir] = []
    if not local_skills_root.exists():
        return skill_dirs

    for skill_md_path in local_skills_root.rglob("SKILL.md"):
        try:
            skill_md = skill_md_path.read_text(encoding="utf-8")
        except Exception:
            continue

        name = _parse_skill_name(skill_md) or skill_md_path.parent.name
        skill_dirs.append(SkillDir(name=name, root_dir=skill_md_path.parent))

    # Stable ordering (helps determinism)
    skill_dirs.sort(key=lambda s: (s.name, str(s.root_dir).lower()))
    return skill_dirs


def build_skill_files(
    *,
    local_skills_root: Path,
    backend_skills_prefix: str = "/skills/project",
    max_file_bytes: int = 2_000_000,
) -> dict[str, dict[str, str]]:
    """Materialize local skills into a DeepAgents-compatible `files` mapping.

    DeepAgents skill sources typically look like:
      /skills/project/<skill-name>/SKILL.md
      /skills/project/<skill-name>/references/...

    We map each discovered skill directory to `/skills/project/<skill-name>/...`.
    """
    prefix = backend_skills_prefix.rstrip("/")
    files: dict[str, dict[str, str]] = {}
    for skill_dir in discover_skill_dirs(local_skills_root):
        for path in skill_dir.root_dir.rglob("*"):
            if not path.is_file():
                continue
            try:
                data = path.read_bytes()
            except Exception:
                continue
            if len(data) > max_file_bytes:
                continue
            rel = path.relative_to(skill_dir.root_dir).as_posix()
            backend_path = f"{prefix}/{skill_dir.name}/{rel}"
            files[backend_path] = _as_file_data(data.decode("utf-8", errors="replace"))
    return files
