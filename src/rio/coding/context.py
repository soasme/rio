"""Project instruction loading."""

from __future__ import annotations

from pathlib import Path

from rio.coding.system_prompt import ProjectContextFile


def load_agents_md(cwd: Path, path: Path | None = None) -> ProjectContextFile | None:
    """Load ``path``, or ``cwd/AGENTS.md`` when present, as project instructions."""
    if path is None:
        path = cwd / "AGENTS.md"
        if not path.is_file():
            return None
    path = path.expanduser().resolve()
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"Cannot read AGENTS.md: {exc}") from exc
    return ProjectContextFile(path=str(path), content=content)
