"""Append-only session entry models for a notebook journal.

The context is a Jupyter notebook. A `state_reset` entry holds a whole
notebook; every step holds the JSON Patch from the notebook before it to the
one after. The notebook at any entry is the last reset on its branch with the
later step patches applied, so it is derived from the journal and never kept
anywhere else.
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


class TurnEntry(BaseSessionEntry):
    """A user turn: the task that seeded a run of steps."""

    type: Literal["turn"] = "turn"
    observation: str


class StepEntry(BaseSessionEntry):
    """One committed step, as the JSON Patch that turns the previous notebook into this one.

    The patch covers everything that changed: the model's edits, the outputs
    of the cells that ran, and the user and reply cells the runtime added.
    """

    type: Literal["step"] = "step"
    step: int
    patch: list[dict[str, JSONValue]] = Field(default_factory=list)
    cells: list[int] = Field(default_factory=list)
    reply: str | None = None

    @property
    def terminated(self) -> bool:
        return self.reply is not None


class StateResetEntry(BaseSessionEntry):
    """The notebook was replaced wholesale: a new session, a rewind, or a fork."""

    type: Literal["state_reset"] = "state_reset"
    notebook: dict[str, JSONValue] = Field(default_factory=dict)
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

#: Entry types that change the notebook, and so can be branched from.
NOTEBOOK_ENTRY_TYPES = frozenset({"step", "state_reset"})
