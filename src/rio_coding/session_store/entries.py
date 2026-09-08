"""Append-only session entry models for a SKILL.state execution journal.

tau's session log was a transcript: an append-only list of conversation
messages that had to be replayed to reconstruct what the agent knew. rio's is
a *state journal* instead. Every committed step writes both the merge patch
that was applied and the full execution state that resulted, so resuming a
session is a single read of the newest snapshot rather than a replay of
everything that came before.

That also makes branching cheap. A branch point is any entry that carries a
state snapshot; rewinding to it means adopting that snapshot verbatim. There
is no history to rewrite because the model never saw any history.
"""

from __future__ import annotations

from time import time
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from rio_ai.types import JSONValue


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
    """One committed SKILL.state step.

    ``state_delta`` is the RFC 7396 merge patch the model proposed and the
    runtime accepted; ``state`` is the full execution state that resulted.
    Storing both means the journal is auditable *and* resumable without replay.

    The model's reasoning is deliberately absent *from this entry*. This is the
    entry a resume reads, so anything stored here is one step away from
    reaching a prompt and rebuilding the unbounded history the design exists to
    avoid. Reasoning is journaled separately, as `ReasoningEntry`.
    """

    type: Literal["step"] = "step"
    step: int
    state_delta: dict[str, JSONValue] = Field(default_factory=dict)
    state: dict[str, JSONValue] = Field(default_factory=dict)
    action: ActionRecord
    observation: str | None = None
    observation_truncated: bool = False
    terminated: bool = False


class StateResetEntry(BaseSessionEntry):
    """The execution state was replaced wholesale: a new session, or a rewind."""

    type: Literal["state_reset"] = "state_reset"
    state: dict[str, JSONValue] = Field(default_factory=dict)
    reason: str | None = None
    restored_from_entry_id: str | None = None


class ValidationFailureEntry(BaseSessionEntry):
    """A rejected step proposal, kept for diagnostics.

    Rejections never reach the execution state, so they are not part of the
    resumable chain -- they record that the rollback-retry cycle fired.
    """

    type: Literal["validation_failure"] = "validation_failure"
    step: int
    attempt: int
    error: str


class ReasoningEntry(BaseSessionEntry):
    """The model's reasoning for a step, kept for diagnostics.

    Never read back into a prompt and never part of the resumable chain: it
    carries no state snapshot, so a resume or a branch cannot land on it. It is
    a leaf note beside the step it explains, not a link in the chain.
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
    """A human-readable summary of the state diff along an abandoned branch."""

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

#: Entry types that carry a full execution-state snapshot, and so can be
#: resumed from or branched at without replaying anything earlier.
#:
#: "reasoning" is left out deliberately, not by oversight. That omission is
#: what makes `entry_state()` return None for a `ReasoningEntry`, so resume and
#: branching cannot see one. Do not "fix" it by adding the type here.
SNAPSHOT_ENTRY_TYPES = frozenset({"turn", "step", "state_reset"})


def entry_state(entry: SessionEntry) -> dict[str, JSONValue] | None:
    """Return the execution state an entry captured, or None if it captured none."""
    if entry.type in SNAPSHOT_ENTRY_TYPES:
        return dict(entry.state)  # type: ignore[union-attr]
    return None
