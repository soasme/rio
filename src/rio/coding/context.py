"""Project instructions for coding sessions."""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape


def project_context(cwd: Path, path: Path | None = None) -> str:
    """Return ``path``, or ``cwd/AGENTS.md`` when present, wrapped as prompt instructions."""
    if path is None:
        path = cwd / "AGENTS.md"
        if not path.is_file():
            return ""
    path = path.expanduser().resolve()
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"Cannot read AGENTS.md: {exc}") from exc
    return "\n".join(
        [
            "\n\n<project_context>",
            "",
            "Project-specific instructions and guidelines:",
            "",
            f'<project_instructions path="{escape(str(path))}">',
            content,
            "</project_instructions>",
            "",
            "</project_context>",
        ]
    )
