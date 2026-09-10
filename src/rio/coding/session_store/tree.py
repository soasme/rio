"""Session tree traversal and state-checkpoint recovery.

The journal is a tree: each entry names its parent, so alternate branches can
coexist in one file. Where a transcript-based agent has to walk a branch and
replay every message on it, a state journal only has to find the newest entry
on the branch that carries a snapshot -- everything before it is already folded
into that snapshot.
"""

from __future__ import annotations

from collections.abc import Sequence

from rio.ai.types import JSONValue
from rio.coding.session_store.entries import SessionEntry, entry_state


class SessionTreeError(ValueError):
    """Raised when session entries do not form a valid traversable tree."""


def entries_by_id(entries: Sequence[SessionEntry]) -> dict[str, SessionEntry]:
    """Return entries keyed by id, rejecting duplicates."""
    result: dict[str, SessionEntry] = {}
    for entry in entries:
        if entry.id in result:
            raise SessionTreeError(f"Duplicate session entry id: {entry.id}")
        result[entry.id] = entry
    return result


def path_to_entry(entries: Sequence[SessionEntry], leaf_id: str) -> list[SessionEntry]:
    """Return the root-to-leaf path for `leaf_id`."""
    by_id = entries_by_id(entries)
    path: list[SessionEntry] = []
    seen: set[str] = set()
    current_id: str | None = leaf_id

    while current_id is not None:
        if current_id in seen:
            raise SessionTreeError(f"Cycle detected at session entry: {current_id}")
        seen.add(current_id)
        entry = by_id.get(current_id)
        if entry is None:
            raise SessionTreeError(f"Missing session entry: {current_id}")
        path.append(entry)
        current_id = entry.parent_id

    path.reverse()
    return path


def latest_leaf_id(entries: Sequence[SessionEntry]) -> str | None:
    """Return the active branch leaf: the newest explicit pointer, else the last entry."""
    for entry in reversed(entries):
        if entry.type == "leaf":
            return entry.entry_id  # type: ignore[union-attr]
    return entries[-1].id if entries else None


def state_at_entry(
    entries: Sequence[SessionEntry],
    leaf_id: str,
) -> tuple[dict[str, JSONValue], SessionEntry | None]:
    """Return the execution state in force at `leaf_id`, and the entry that carried it.

    Walks the root-to-leaf path backwards to the newest snapshot. No replay is
    involved: a snapshot is the complete state, because every accepted step
    persisted the state it produced rather than the events that produced it.
    """
    path = path_to_entry(entries, leaf_id)
    for entry in reversed(path):
        state = entry_state(entry)
        if state is not None:
            return state, entry
    return {}, None


def resume_state(entries: Sequence[SessionEntry]) -> dict[str, JSONValue]:
    """Return the execution state a resumed session should start from."""
    leaf_id = latest_leaf_id(entries)
    if leaf_id is None:
        return {}
    state, _entry = state_at_entry(entries, leaf_id)
    return state


def checkpoints(entries: Sequence[SessionEntry]) -> list[SessionEntry]:
    """Return every entry that can be branched from, oldest first."""
    return [entry for entry in entries if entry_state(entry) is not None]
