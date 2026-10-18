"""Validated records exposed by the journal and session APIs."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ControlType = Literal["cancel", "resolution", "conclusion"]
RunResult = Literal["success", "failure"]
Phase = Literal["queued", "started", "finished"]
ResultType = Literal["success", "error", "cancelled", "UnknownExecution"]
EventKind = Literal[
    "session_created",
    "inbox_received",
    "turn_prepared",
    "model_attempt",
    "model_response",
    "patch_rejected",
    "provider_failed",
    "patch_accepted",
    "work_requested",
    "invalid_idle",
    "execution_started",
    "execution_output",
    "output_truncated",
    "execution_finished",
    "timer_registered",
    "timer_fired",
    "control_handled",
    "validator_result",
    "owner_record",
    "run_ended",
]


class RecordModel(BaseModel):
    """Strict records; JSON aliases preserve the existing journal format."""

    model_config = ConfigDict(strict=True, extra="forbid", populate_by_name=True)


class Cell(RecordModel):
    model_config = ConfigDict(extra="allow")
    # Model memory. A note has text; other kinds carry the fields of their CellKind.
    id: int
    previous_id: int | None
    kind: str  # Caller-provided CellKind.name, or the built-in "note".
    title: str | None = None  # Brief model-written summary for console display.
    text: str | None = None


class ControlRequest(RecordModel):
    # A model action from a control op; required fields per type are in state.CONTROLS.
    type: ControlType
    reason: str
    target_cell_id: int | None = None
    evidence_refs: list[str] | None = None
    result: RunResult | None = None


class Usage(RecordModel):
    turns: int
    attempts: int
    tokens: int
    unknown_usage: int


class Observation(RecordModel):
    # Inbox message as delivered to the model.
    id: str
    kind: str  # execution, timer, control_rejected, invalid_idle, context, or external
    payload: dict[str, Any]
    seq: int


class InboxMessage(Observation):
    consumed: bool


class ExecutionResult(RecordModel):
    model_config = ConfigDict(extra="allow")
    # Owners may add their own fields.
    type: ResultType
    cell_id: int
    evidence_refs: list[str]
    reason: str | None = None


class Execution(RecordModel):
    cell_id: int
    phase: Phase
    output: dict[str, str]  # text by owner-named stream
    truncated: bool
    accepted: int  # journal seq of patch_accepted
    started: float | None = None
    finished: float | None = None
    result: ExecutionResult | None = None
    uncertain: bool | None = None


class RuntimeCell(Execution):
    # Execution as shown to the model, with the resolution control id added.
    resolved_by: int | None = None
    # Leading UTF-8 bytes left out per stream to fit context; its output is the tail.
    omitted: dict[str, int] | None = None


class Runtime(RecordModel):
    next_cell_id: int
    cells: dict[str, RuntimeCell]
    inbox: list[Observation]
    usage: Usage  # Meta.usage as of a prepared turn; zeros between turns


class State(RecordModel):
    # Model-facing State; Cells are its only model-written part.
    schema_version: str = Field(alias="schema")
    id: str
    revision: int
    goal: str
    cells: list[Cell]
    runtime: Runtime


class Receipt(RecordModel):
    turn_id: str
    revision: int
    cell_ids: list[int]
    control_ids: list[int]
    event: int


class Turn(RecordModel):
    id: str
    state: State
    delivered: list[str]
    system: str
    tool_schema: dict[str, Any]
    attempts: int
    retry_at: float
    error: str | None  # last rejection, shown to the model on the next attempt
    receipt: Receipt | None
    response: dict[str, Any] | None  # AssistantMessage JSON
    ops: list | str | None = None  # set on acceptance
    provider_failures: int | None = None  # set by provider_failed; never shown to the model
    provider_error: str | None = None  # set by provider_failed: the last provider error


class Control(RecordModel):
    control: ControlRequest
    handled: bool
    outcome: str | None = None  # "accepted" or a rejection reason


class Timer(RecordModel):
    id: str
    deadline: float
    payload: dict[str, Any]
    fired: bool


class Terminal(RecordModel):
    result: RunResult
    reason: str
    uncertainty: list[int]
    cleanup_error: str | None  # why the owner could not stop active work


class Meta(RecordModel):
    state: State
    limits: dict  # asdict(Limits)
    external_wakeup: bool
    validators: list  # owner-defined specs, opaque to the core
    owner_config: dict  # owner-defined settings, opaque to the core
    provider_model_config: dict[str, Any] = Field(alias="model_config")
    instructions: str
    terminal: Terminal | None
    pause: str | None  # inbox message id of the failure that paused dispatch
    usage: Usage
    idle: int  # consecutive invalid idle decisions


class ValidatorResult(RecordModel):
    model_config = ConfigDict(extra="allow")
    # Owners may add their own fields.
    passed: bool
    output: str


class Projection(RecordModel):
    model_config = ConfigDict(extra="allow")
    # Store.projection for a Session; collections are keyed by str ids.
    meta: Meta
    cells: dict[str, Cell]
    executions: dict[str, Execution]
    inbox: dict[str, InboxMessage]
    turns: dict[str, Turn]
    controls: dict[str, Control]  # keyed by control id: 1, 2, ... in accepted op order
    timers: dict[str, Timer]
    resolutions: dict[str, int]  # target Cell id -> resolution control id
    validator_results: dict[str, ValidatorResult]  # keyed by "<conclusion control id>:<index>"
    metrics: dict[str, float | int] | None = None
    # Other keys are owner collections written by Session.record.


class JournalEvent(RecordModel):
    seq: int
    type: EventKind
    timestamp: float
    changes: dict[str, Any]  # records, turn field updates, or output chunks folded by reduce_event


class HistoryRecord(RecordModel):
    """Read-only history uses kind for the event type."""

    seq: int
    kind: str
    changes: dict[str, Any]
