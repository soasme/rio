"""Skill instruction assembly for rio coding skills.

Produces the fixed instructions handed to `rio.agent.HarnessSpec.instructions`:
guidelines, project context, and skills. The runtime appends the notebook
protocol (see `rio.agent.prompt`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

from rio.coding.skills import Skill


@dataclass(frozen=True, slots=True)
class ProjectContextFile:
    """A project instruction file included in the skill instructions."""

    path: str
    content: str


@dataclass(frozen=True, slots=True)
class PromptSection:
    """A free-form section appended to the skill instructions."""

    title: str | None
    body: str


@dataclass(frozen=True, slots=True)
class BuildSystemPromptOptions:
    """Options used to build rio's skill instructions."""

    cwd: Path
    skills: Sequence[Skill] = ()
    custom_prompt: str | None = None
    append_system_prompt: str | None = None
    context_files: Sequence[ProjectContextFile] = ()
    current_date: date | None = None
    extra_guidelines: Sequence[str] = field(default_factory=tuple)
    extra_sections: Sequence[PromptSection] = field(default_factory=tuple)


EDIT_MAGIC_HELP = """To change part of a file, use a `%%edit` cell. Each SEARCH text must \
match the original file exactly once; blocks must not overlap; nothing is written unless \
every block applies. The cell prints a diff.

```
%%edit path/to/file.py
<<<<<<< SEARCH
exact old text
=======
new text
>>>>>>> REPLACE
```"""


def build_skill_instructions(options: BuildSystemPromptOptions) -> str:
    """Build the deterministic skill instructions for a rio coding skill."""
    current_date = options.current_date or date.today()
    cwd = _format_path(options.cwd)
    append_parts = [options.append_system_prompt] if options.append_system_prompt else []
    append_parts.extend(format_prompt_section(section) for section in options.extra_sections)
    append_section = "".join(f"\n\n{part}" for part in append_parts)

    if options.custom_prompt is not None:
        prompt = options.custom_prompt
        prompt += append_section
        prompt += format_project_context(options.context_files)
        prompt += format_skills_for_prompt(options.skills)
        prompt += f"\nCurrent date: {current_date.isoformat()}"
        prompt += f"\nCurrent working directory: {cwd}"
        return prompt

    prompt = (
        "You are an expert coding assistant operating inside rio, a coding agent whose "
        "context is a Jupyter notebook it manages itself. You help users by reading files, "
        "executing commands, editing code, and writing new files from notebook cells."
        f"\n\nGuidelines:\n{format_guidelines(options.extra_guidelines)}"
        f"\n\n{EDIT_MAGIC_HELP}"
    )

    prompt += append_section
    prompt += format_project_context(options.context_files)
    prompt += format_skills_for_prompt(options.skills)
    prompt += f"\nCurrent date: {current_date.isoformat()}"
    prompt += f"\nCurrent working directory: {cwd}"
    return prompt


# Ported call sites still refer to the conversational-era name.
build_system_prompt = build_skill_instructions


def format_prompt_section(section: PromptSection) -> str:
    """Render one optional-title free-form prompt section."""
    if section.title is None:
        return section.body
    return f"## {section.title}\n\n{section.body}"


def collect_prompt_guidelines(extra_guidelines: Sequence[str] = ()) -> list[str]:
    """Collect and de-duplicate skill-instruction guidelines."""
    guidelines: list[str] = []
    seen: set[str] = set()
    for value in (
        *extra_guidelines,
        "Inspect relevant files and project instructions before editing",
        "Make focused changes that preserve the project's architecture and style",
        "Do not overwrite or discard unrelated user changes",
        "Use the project's documented commands and package manager",
        "Run relevant tests, formatting, linting, and type checks after changes",
        "Report checks honestly; never claim a command passed unless you ran it",
        "Ask before destructive operations or materially ambiguous design choices",
        "Be concise in your responses",
        "Show file paths clearly when working with files",
    ):
        normalized = value.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            guidelines.append(normalized)
    return guidelines


def format_guidelines(extra_guidelines: Sequence[str] = ()) -> str:
    """Format prompt guidelines as markdown bullets."""
    return "\n".join(f"- {guideline}" for guideline in collect_prompt_guidelines(extra_guidelines))


def format_project_context(context_files: Sequence[ProjectContextFile]) -> str:
    """Format project context files using an XML-like wrapper."""
    if not context_files:
        return ""

    lines = [
        "\n\n<project_context>",
        "",
        "Project-specific instructions and guidelines:",
        "",
    ]
    for context_file in context_files:
        lines.append(f'<project_instructions path="{escape(context_file.path)}">')
        lines.append(context_file.content)
        lines.append("</project_instructions>")
        lines.append("")
    lines.append("</project_context>")
    return "\n".join(lines)


def format_skills_for_prompt(skills: Sequence[Skill]) -> str:
    """Format skills for inclusion in the skill instructions using an XML style.

    Skills with ``disable_model_invocation`` set are excluded from the prompt;
    they remain invocable explicitly via ``/skill:<name>``.
    """
    visible_skills = [skill for skill in skills if not skill.disable_model_invocation]
    if not visible_skills:
        return ""

    lines = [
        "\n\nThe following skills provide specialized instructions for specific tasks.",
        "Read the full skill file when the task matches its description.",
        "When a skill file references a relative path, resolve it against the skill directory "
        "(parent of SKILL.md / dirname of the path) and use that absolute path in tool commands.",
        "",
        "<available_skills>",
    ]
    for skill in sorted(visible_skills, key=lambda item: item.name):
        description = skill.description or "No description"
        lines.extend(
            [
                "  <skill>",
                f"    <name>{escape(skill.name)}</name>",
                f"    <description>{escape(description)}</description>",
                f"    <location>{escape(str(skill.path))}</location>",
                "  </skill>",
            ]
        )
    lines.append("</available_skills>")
    return "\n".join(lines)


def _format_path(path: Path) -> str:
    return str(path).replace("\\", "/")
