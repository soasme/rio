"""Shapes of the journal projection. Documentation only: records stay plain dicts."""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict

NoteRole = Literal["cancel", "resolution", "conclusion"]
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
    "worker_registered",
    "execution_output",
    "output_truncated",
    "execution_finished",
    "worker_cleanup",
    "timer_registered",
    "timer_fired",
    "control_handled",
    "validator_started",
    "validator_registered",
    "validator_finished",
    "validator_interrupted",
    "validator_result",
    "run_ended",
]


class Cell(TypedDict):
    # Note keys below; other kinds carry the fields of their CellKind instead.
    id: int
    previous_id: int | None
    kind: str  # Caller-provided CellKind.name, or the built-in "note".
    text: NotRequired[str]
    role: NotRequired[NoteRole]
    target_cell_id: NotRequired[int]
    evidence_refs: NotRequired[list[str]]
    result: NotRequired[RunResult]


class Usage(TypedDict):
    rounds: int
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


class WorkerIdentity(TypedDict):
    # pid and birth are added once the gated supervisor is spawned.
    token: str
    pid: NotRequired[int]
    birth: NotRequired[float]


class ExecutionResult(TypedDict):
    type: ResultType
    cell_id: int
    evidence_refs: list[str]
    exit_code: NotRequired[int | None]
    reason: NotRequired[str]
    worker_stopped: NotRequired[bool]


class Execution(TypedDict):
    cell_id: int
    phase: Phase
    output: str
    stderr: str
    truncated: bool
    accepted: int  # journal seq of patch_accepted
    worker: NotRequired[WorkerIdentity]
    started: NotRequired[float]
    finished: NotRequired[float]
    result: NotRequired[ExecutionResult]
    uncertain: NotRequired[bool]
    worker_stopped: NotRequired[bool]


class RuntimeCell(Execution):
    # Execution as shown to the model: worker removed, resolution note id added.
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
    event: int


class Turn(TypedDict):
    id: str
    state: State
    delivered: list[str]
    system: str
    tool_schema: dict
    model_config: dict
    attempts: int
    retry_at: float
    error: str | None
    receipt: Receipt | None
    response: dict | None  # AssistantMessage JSON
    ops: NotRequired[list]  # set on acceptance


class Control(TypedDict):
    cell: Cell
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
    cleanup_error: str | None


class Meta(TypedDict):
    state: State
    cwd: str
    limits: dict  # asdict(Limits)
    external_wakeup: NotRequired[bool]
    validators: list[list[str]]
    model_config: dict
    instructions: str
    terminal: Terminal | None
    pause: str | None  # inbox message id of the failure that paused dispatch
    invalid: int
    usage: Usage
    idle: NotRequired[int]


class ValidatorResult(TypedDict):
    exit: int | None
    output: str


class ValidatorRecord(ValidatorResult):
    command: list[str]


class ValidationWorker(TypedDict):
    worker: WorkerIdentity
    result: ValidatorResult | None


class Projection(TypedDict):
    # Store.data for a Session; collections are keyed by str ids.
    meta: Meta
    cells: dict[str, Cell]
    executions: dict[str, Execution]
    inbox: dict[str, InboxMessage]
    turns: dict[str, Turn]
    controls: dict[str, Control]  # keyed by note Cell id
    timers: dict[str, Timer]
    resolutions: dict[str, int]  # target Cell id -> resolution note id
    validator_results: dict[str, ValidatorRecord]  # keyed by validator index
    metrics: NotRequired[dict[str, float | int]]
    validation_workers: NotRequired[dict[str, ValidationWorker]]  # first validator run adds it


class JournalEvent(TypedDict):
    seq: int
    type: EventKind
    timestamp: float
    changes: dict  # partial Projection merged per collection by reduce_event
