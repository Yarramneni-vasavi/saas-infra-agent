from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel


class SkillMeta(BaseModel):
    name: str
    description: str | None = None
    path: str


def _parse_frontmatter(skill_md: str) -> dict | None:
    if not skill_md.startswith("---"):
        return None
    parts = skill_md.split("---", 2)
    if len(parts) < 3:
        return None
    _, fm, _ = parts
    meta = yaml.safe_load(fm) or {}
    return meta if isinstance(meta, dict) else None


def skills_root() -> Path:
    # `saas_infra_agent/skills`
    return Path(__file__).resolve().parents[1] / "skills"


def list_skills(root: Path | None = None) -> list[SkillMeta]:
    root = root or skills_root()
    out: list[SkillMeta] = []
    if not root.exists():
        return out

    for p in root.rglob("SKILL.md"):
        try:
            txt = p.read_text(encoding="utf-8")
        except Exception:
            continue
        meta = _parse_frontmatter(txt) or {}
        name = meta.get("name") or p.parent.name
        if not isinstance(name, str) or not name.strip():
            continue
        desc = meta.get("description")
        out.append(
            SkillMeta(
                name=name.strip(),
                description=desc.strip() if isinstance(desc, str) else None,
                path=str(p),
            )
        )

    out.sort(key=lambda s: (s.name.lower(), s.path.lower()))
    return out


def read_skill_md(name: str, root: Path | None = None) -> str | None:
    root = root or skills_root()
    name = name.strip()
    if not name:
        return None

    for meta in list_skills(root):
        if meta.name == name:
            return Path(meta.path).read_text(encoding="utf-8")
    return None

