"""Append-only session entry models for a CLM context journal.

Every step writes the full context that resulted, as `state["context"]`, so
resuming a session is a single read of the newest snapshot rather than a
replay. A branch point is any entry that carries a snapshot; rewinding to it
means adopting that context verbatim.
"""

from __future__ import annotations

from time import time
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from rio.ai.types import JSONValue


def new_entry_id() -> str:
    """Return a unique session entry id."""
    return uuid4().hex


def current_timestamp() -> float:
    """Return the current Unix timestamp."""
    return time()


class BaseSessionEntry(BaseModel):
    """Common fields shared by all append-only session entries."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_entry_id)
    parent_id: str | None = None
    timestamp: float = Field(default_factory=current_timestamp)


class ActionRecord(BaseModel):
    """The single action a step chose to execute."""

    model_config = ConfigDict(extra="forbid")

    name: str
    arguments: dict[str, JSONValue] = Field(default_factory=dict)


class TurnEntry(BaseSessionEntry):
    """A user turn: the observation that seeded a run of steps."""

    type: Literal["turn"] = "turn"
    observation: str
    state: dict[str, JSONValue] = Field(default_factory=dict)


class StepEntry(BaseSessionEntry):
    """One committed CLM step: its action, observation, and the resulting context."""

    type: Literal["step"] = "step"
    step: int
    state: dict[str, JSONValue] = Field(default_factory=dict)
    action: ActionRecord
    observation: str | None = None
    observation_truncated: bool = False
    terminated: bool = False


class StateResetEntry(BaseSessionEntry):
    """The context was replaced wholesale: a new session, or a rewind."""

    type: Literal["state_reset"] = "state_reset"
    state: dict[str, JSONValue] = Field(default_factory=dict)
    reason: str | None = None
    restored_from_entry_id: str | None = None


class ValidationFailureEntry(BaseSessionEntry):
    """A reply with no usable action, kept for diagnostics. It never reaches the context."""

    type: Literal["validation_failure"] = "validation_failure"
    step: int
    attempt: int
    error: str


class ReasoningEntry(BaseSessionEntry):
    """The model's reasoning for a step, kept for diagnostics.

    A leaf note beside the step it explains: it carries no snapshot, so a
    resume or a branch cannot land on it.
    """

    type: Literal["reasoning"] = "reasoning"
    step: int
    reasoning: str
    truncated: bool = False


class ModelChangeEntry(BaseSessionEntry):
    """A model selection change entry."""

    type: Literal["model_change"] = "model_change"
    model: str
    provider: str | None = None


class ThinkingLevelChangeEntry(BaseSessionEntry):
    """A thinking/reasoning level change entry."""

    type: Literal["thinking_level_change"] = "thinking_level_change"
    thinking_level: str | None = None


class BranchSummaryEntry(BaseSessionEntry):
    """A human-readable summary of an abandoned branch."""

    type: Literal["branch_summary"] = "branch_summary"
    summary: str
    branch_root_id: str | None = None


class LabelEntry(BaseSessionEntry):
    """A human-readable session label entry."""

    type: Literal["label"] = "label"
    label: str


class LeafEntry(BaseSessionEntry):
    """The active branch leaf pointer entry."""

    type: Literal["leaf"] = "leaf"
    entry_id: str | None = None


class SessionInfoEntry(BaseSessionEntry):
    """Basic session metadata entry."""

    type: Literal["session_info"] = "session_info"
    created_at: float = Field(default_factory=current_timestamp)
    cwd: str | None = None
    title: str | None = None
    skill: str | None = None


class CustomEntry(BaseSessionEntry):
    """Extension/application-owned session data."""

    type: Literal["custom"] = "custom"
    namespace: str
    data: dict[str, JSONValue] = Field(default_factory=dict)


type SessionEntry = Annotated[
    TurnEntry
    | StepEntry
    | StateResetEntry
    | ValidationFailureEntry
    | ReasoningEntry
    | ModelChangeEntry
    | ThinkingLevelChangeEntry
    | BranchSummaryEntry
    | LabelEntry
    | LeafEntry
    | SessionInfoEntry
    | CustomEntry,
    Field(discriminator="type"),
]

#: Entry types that carry a full context snapshot, and so can be resumed from
#: or branched at without replaying anything earlier.
SNAPSHOT_ENTRY_TYPES = frozenset({"turn", "step", "state_reset"})


def entry_state(entry: SessionEntry) -> dict[str, JSONValue] | None:
    """Return the snapshot an entry captured, or None if it captured none."""
    if entry.type in SNAPSHOT_ENTRY_TYPES:
        return dict(entry.state)  # type: ignore[union-attr]
    return None


def entry_context(entry: SessionEntry) -> list[dict[str, JSONValue]] | None:
    """Return the context an entry captured, or None if it captured none."""
    state = entry_state(entry)
    if state is None:
        return None
    context = state.get("context")
    if not isinstance(context, list):
        return []
    return [
        {"role": str(item.get("role", "user")), "text": str(item.get("text", ""))}
        for item in context
        if isinstance(item, dict)
    ]
