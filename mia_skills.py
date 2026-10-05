"""Mia's skills: guides she reads when a job needs them, kept in mia_skills/<name>/SKILL.md.

Her planning prompt lists each skill's name, where it is and what it's for (index()). Her
sessions have no tools to open files, so she asks for one by name ({"load_skills": [...]})
and ChatHub sends her its text (load()) before she plans.
"""

from __future__ import annotations

import re
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent / "mia_skills"
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
FRONT = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.S)
MAX_CHARS = 30000


def _read(path: Path) -> tuple[dict, str]:
    """A SKILL.md's frontmatter fields and its body."""
    text = path.read_text(encoding="utf-8")[:MAX_CHARS]
    match = FRONT.match(text)
    if not match:
        return {}, text.strip()
    fields = {}
    for line in match[1].splitlines():
        key, _, value = line.partition(":")
        if value:
            fields[key.strip()] = value.strip()
    return fields, match[2].strip()


def index() -> list[dict]:
    """[{"name", "description", "path"}] for every skill, by name."""
    skills = []
    for path in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        try:
            fields, _ = _read(path)
        except OSError:
            continue
        name = fields.get("name") or path.parent.name
        if NAME.match(name) and fields.get("description"):
            skills.append({"name": name, "description": fields["description"], "path": str(path)})
    return skills


def load(name: str) -> str:
    """A skill's text, or "" when there's no skill by that name."""
    for skill in index():
        if skill["name"] == name:
            try:
                return _read(Path(skill["path"]))[1]
            except OSError:
                return ""
    return ""


def prompt_index() -> str:
    """The skills section of Mia's planning prompt."""
    skills = index()
    if not skills:
        return ""
    lines = "\n".join(f"- {s['name']} ({s['path']}): {s['description']}" for s in skills)
    return (f"\n\nSkills: guides for jobs that need more than this prompt, kept in {SKILLS_DIR}. "
            f"Available:\n{lines}\n"
            "When the request matches a skill's description, first reply with only "
            "{\"load_skills\": [\"its name\", ...]} and nothing else. You'll get its text, then reply with your "
            "plan, following it.")
