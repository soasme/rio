"""Durable execution-state journal for rio coding sessions.

A rio session log records *state*, not conversation. Each committed step
persists the merge patch that was applied and the execution state it produced,
so a session resumes by reading one snapshot instead of replaying a transcript.
"""

# ruff: noqa: F401 - this module intentionally defines the public facade

from rio_coding.session_store.entries import (
    SNAPSHOT_ENTRY_TYPES,
    ActionRecord,
    BaseSessionEntry,
    BranchSummaryEntry,
    CustomEntry,
    LabelEntry,
    LeafEntry,
    ModelChangeEntry,
    SessionEntry,
    SessionInfoEntry,
    StateResetEntry,
    StepEntry,
    ThinkingLevelChangeEntry,
    TurnEntry,
    ValidationFailureEntry,
    current_timestamp,
    entry_state,
    new_entry_id,
)
from rio_coding.session_store.jsonl import (
    SessionJsonlError,
    entries_from_json_lines,
    entry_from_json_line,
    entry_to_json_line,
)
from rio_coding.session_store.storage import (
    InMemorySessionStorage,
    JsonlSessionStorage,
    SessionStorage,
)
from rio_coding.session_store.tree import (
    SessionTreeError,
    checkpoints,
    entries_by_id,
    latest_leaf_id,
    path_to_entry,
    resume_state,
    state_at_entry,
)

__all__ = [name for name in globals() if not name.startswith("_")]
