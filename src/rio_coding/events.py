"""Coding-session events consumed by frontends and SDK users.

A coding session emits two kinds of event. The first kind comes straight from
`rio_agent`: the SKILL.state step lifecycle (`StepStartEvent`,
`StateUpdateEvent`, `ActionStartEvent`, ...). The second kind, defined here, is
about the session rather than the run -- queued input, model changes, journal
writes, retries.

tau had a third kind, compaction events, because its transcript grew until it
had to be summarized. rio has no transcript to compact, so those events have no
counterpart here.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from rio_agent.events import SkillEvent
from rio_ai.messages import WireModel
from rio_ai.types import JSONValue
from rio_coding.session_store.entries import SessionEntry


class SessionRunEndEvent(WireModel):
    """One `prompt()`/`continue_()` run finished.

    Carries the execution state the run settled on -- the session's entire
    memory of what happened, since nothing else survives a step.
    """

    type: Literal["run_end"] = "run_end"
    steps: int = 0
    state: dict[str, JSONValue] = Field(default_factory=dict)
    answer: str | None = None
    will_retry: bool = False


class AgentSettledEvent(WireModel):
    """The session is idle: no run in flight and no queued input."""

    type: Literal["agent_settled"] = "agent_settled"


class QueueUpdateEvent(WireModel):
    """The steering / follow-up input queues changed."""

    type: Literal["queue_update"] = "queue_update"
    steering: tuple[str, ...] = ()
    follow_up: tuple[str, ...] = ()


class EntryAppendedEvent(WireModel):
    """An entry was appended to the execution-state journal."""

    type: Literal["entry_appended"] = "entry_appended"
    entry: SessionEntry


class StateRestoredEvent(WireModel):
    """The execution state was replaced from a checkpoint.

    This is rio's form of branching. Restoring is a whole-state swap rather
    than a transcript rewrite, because a checkpoint already holds everything
    the model would be told.
    """

    type: Literal["state_restored"] = "state_restored"
    state: dict[str, JSONValue] = Field(default_factory=dict)
    entry_id: str | None = None
    reason: str | None = None


class SessionInfoChangedEvent(WireModel):
    """The session's display name changed."""

    type: Literal["session_info_changed"] = "session_info_changed"
    name: str | None = None


class ThinkingLevelChangedEvent(WireModel):
    """The reasoning-effort level changed."""

    type: Literal["thinking_level_changed"] = "thinking_level_changed"
    level: str


class AutoRetryStartEvent(WireModel):
    """A transient provider failure is about to be retried."""

    type: Literal["auto_retry_start"] = "auto_retry_start"
    attempt: int
    max_attempts: int
    delay_ms: int
    error_message: str


class AutoRetryEndEvent(WireModel):
    """An automatic retry sequence finished."""

    type: Literal["auto_retry_end"] = "auto_retry_end"
    success: bool
    attempt: int
    final_error: str | None = None


type SessionOwnEvent = Annotated[
    SessionRunEndEvent
    | AgentSettledEvent
    | QueueUpdateEvent
    | EntryAppendedEvent
    | StateRestoredEvent
    | SessionInfoChangedEvent
    | ThinkingLevelChangedEvent
    | AutoRetryStartEvent
    | AutoRetryEndEvent,
    Field(discriminator="type"),
]

type CodingSessionEvent = SkillEvent | SessionOwnEvent
