"""Session tree traversal and notebook recovery.

The journal is a tree: each entry names its parent, so alternate branches can
coexist in one file. The notebook at an entry is rebuilt by walking its branch:
start from the newest `state_reset` snapshot and apply each later step patch.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence

import jsonpatch

from rio.agent import Notebook, new_notebook
from rio.coding.session_store.entries import (
    NOTEBOOK_ENTRY_TYPES,
    SessionEntry,
    StateResetEntry,
    StepEntry,
)


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


def notebook_at_entry(entries: Sequence[SessionEntry], entry_id: str) -> Notebook:
    """Return the notebook in force at `entry_id`."""
    path = path_to_entry(entries, entry_id)
    start = 0
    notebook = new_notebook()
    for index in range(len(path) - 1, -1, -1):
        entry = path[index]
        if isinstance(entry, StateResetEntry):
            notebook, start = copy.deepcopy(entry.notebook), index + 1
            break
    for entry in path[start:]:
        if isinstance(entry, StepEntry):
            notebook = jsonpatch.apply_patch(notebook, entry.patch)
    return notebook


def resume_notebook(entries: Sequence[SessionEntry]) -> Notebook:
    """Return the notebook a resumed session should start from."""
    leaf_id = latest_leaf_id(entries)
    return notebook_at_entry(entries, leaf_id) if leaf_id is not None else new_notebook()


def checkpoints(entries: Sequence[SessionEntry]) -> list[SessionEntry]:
    """Return every entry that can be branched from, oldest first."""
    return [entry for entry in entries if entry.type in NOTEBOOK_ENTRY_TYPES]
