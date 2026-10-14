"""Shapes of the journal projection. Documentation only: records stay plain dicts."""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict

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


class Cell(TypedDict):
    # Model memory. A note has text; other kinds carry the fields of their CellKind.
    id: int
    previous_id: int | None
    kind: str  # Caller-provided CellKind.name, or the built-in "note".
    text: NotRequired[str]


class ControlRequest(TypedDict):
    # A model action from a control op; required fields per type are in state.CONTROLS.
    type: ControlType
    reason: str
    target_cell_id: NotRequired[int]
    evidence_refs: NotRequired[list[str]]
    result: NotRequired[RunResult]


class Usage(TypedDict):
    turns: int
    attempts: int
    tokens: int
    unknown_usage: int


class Observation(TypedDict):
    # Inbox message as delivered to the model.
    id: str
    kind: str  # execution, timer, control_rejected, invalid_idle, or any external kind
    payload: dict
    seq: int


class InboxMessage(Observation):
    consumed: bool


class ExecutionResult(TypedDict):
    # Owners may add their own fields.
    type: ResultType
    cell_id: int
    evidence_refs: list[str]
    reason: NotRequired[str]


class Execution(TypedDict):
    cell_id: int
    phase: Phase
    output: dict[str, str]  # text by owner-named stream
    truncated: bool
    accepted: int  # journal seq of patch_accepted
    started: NotRequired[float]
    finished: NotRequired[float]
    result: NotRequired[ExecutionResult]
    uncertain: NotRequired[bool]


class RuntimeCell(Execution):
    # Execution as shown to the model, with the resolution control id added.
    resolved_by: NotRequired[int]


class Runtime(TypedDict):
    next_cell_id: int
    cells: dict[str, RuntimeCell]
    inbox: list[Observation]
    usage: dict[str, int]  # {} until the first turn, then a Usage copy


class State(TypedDict):
    # Model-facing State; Cells are its only model-written part.
    schema: str
    id: str
    revision: int
    goal: str
    cells: list[Cell]
    runtime: Runtime


class Receipt(TypedDict):
    turn_id: str
    revision: int
    cell_ids: list[int]
    control_ids: list[int]
    event: int


class Turn(TypedDict):
    id: str
    state: State
    delivered: list[str]
    system: str
    tool_schema: dict
    attempts: int
    retry_at: float
    error: str | None
    receipt: Receipt | None
    response: dict | None  # AssistantMessage JSON
    ops: NotRequired[list]  # set on acceptance


class Control(TypedDict):
    control: ControlRequest
    handled: bool
    outcome: NotRequired[str]  # "accepted" or a rejection reason


class Timer(TypedDict):
    id: str
    deadline: float
    payload: dict
    fired: bool


class Terminal(TypedDict):
    result: RunResult
    reason: str
    uncertainty: list[int]
    cleanup_error: str | None  # why the owner could not stop active work


class Meta(TypedDict):
    state: State
    limits: dict  # asdict(Limits)
    external_wakeup: bool
    validators: list  # owner-defined specs, opaque to the core
    owner_config: dict  # owner-defined settings, opaque to the core
    model_config: dict
    instructions: str
    terminal: Terminal | None
    pause: str | None  # inbox message id of the failure that paused dispatch
    usage: Usage
    idle: NotRequired[int]


class ValidatorResult(TypedDict):
    # Owners may add their own fields.
    passed: bool
    output: str


class Projection(TypedDict):
    # Store.data for a Session; collections are keyed by str ids.
    meta: Meta
    cells: dict[str, Cell]
    executions: dict[str, Execution]
    inbox: dict[str, InboxMessage]
    turns: dict[str, Turn]
    controls: dict[str, Control]  # keyed by control id: 1, 2, ... in accepted op order
    timers: dict[str, Timer]
    resolutions: dict[str, int]  # target Cell id -> resolution control id
    validator_results: dict[str, ValidatorResult]  # keyed by "<conclusion control id>:<index>"
    metrics: NotRequired[dict[str, float | int]]
    # Other keys are owner collections written by Session.record.


class JournalEvent(TypedDict):
    seq: int
    type: EventKind
    timestamp: float
    changes: dict  # records, turn field updates, or output chunks folded by reduce_event
