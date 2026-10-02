"""Durable notebook journal for rio coding sessions.

Each committed step persists the JSON Patch it made to the notebook, so a
session resumes by replaying the patches on its branch.
"""

# ruff: noqa: F401 - this module intentionally defines the public facade

from rio.coding.session_store.entries import (
    NOTEBOOK_ENTRY_TYPES,
    BaseSessionEntry,
    BranchSummaryEntry,
    CustomEntry,
    LabelEntry,
    LeafEntry,
    ModelChangeEntry,
    ReasoningEntry,
    SessionEntry,
    SessionInfoEntry,
    StateResetEntry,
    StepEntry,
    ThinkingLevelChangeEntry,
    TurnEntry,
    ValidationFailureEntry,
    current_timestamp,
    new_entry_id,
)
from rio.coding.session_store.jsonl import (
    SessionJsonlError,
    entries_from_json_lines,
    entry_from_json_line,
    entry_to_json_line,
)
from rio.coding.session_store.storage import (
    InMemorySessionStorage,
    JsonlSessionStorage,
    SessionStorage,
)
from rio.coding.session_store.tree import (
    SessionTreeError,
    checkpoints,
    entries_by_id,
    latest_leaf_id,
    notebook_at_entry,
    path_to_entry,
    resume_notebook,
)

__all__ = [name for name in globals() if not name.startswith("_")]
