"""Prompt text shared by the CLI, extensions, and MCP clients."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from xml.sax.saxutils import escape


@dataclass(frozen=True, slots=True)
class ProjectContextFile:
    """A project instruction file included in the prompt."""

    path: str
    content: str


@dataclass(frozen=True, slots=True)
class PromptSection:
    """A free-form, optionally titled prompt section."""

    title: str | None
    body: str


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
