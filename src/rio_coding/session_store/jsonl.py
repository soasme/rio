"""JSONL serialization for the execution-state journal."""

from __future__ import annotations

import json

from pydantic import TypeAdapter, ValidationError

from rio_coding.session_store.entries import SessionEntry

_SESSION_ENTRY_ADAPTER: TypeAdapter[SessionEntry] = TypeAdapter(SessionEntry)


class SessionJsonlError(ValueError):
    """Raised when a session JSONL line cannot be decoded."""


def entry_to_json_line(entry: SessionEntry) -> str:
    """Serialize one session entry as a single JSONL line."""
    return _SESSION_ENTRY_ADAPTER.dump_json(entry, exclude_none=True).decode() + "\n"


def entry_from_json_line(line: str, *, line_number: int | None = None) -> SessionEntry:
    """Deserialize one session entry."""
    location = f" on line {line_number}" if line_number is not None else ""
    try:
        payload = json.loads(line)
        return _SESSION_ENTRY_ADAPTER.validate_python(payload)
    except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
        raise SessionJsonlError(f"Invalid session entry{location}: {exc}") from exc


def entries_from_json_lines(lines: list[str]) -> list[SessionEntry]:
    """Deserialize non-empty JSONL lines in order."""
    entries: list[SessionEntry] = []
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        entries.append(entry_from_json_line(line, line_number=index))
    return entries
