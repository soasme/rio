"""Step admission for immutable, independent Cells addressed by id, plus controls."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass

from pydantic import ValidationError

from rio.agent.shapes import Cell, ControlRequest, Execution, Runtime, State, Usage
from rio.agent.store import encode
from rio.truncate import DEFAULT_MAX_OUTPUT_BYTES


class InvalidPatch(ValueError):
    pass


def zero_usage() -> Usage:
    return Usage.model_validate({"turns": 0, "attempts": 0, "tokens": 0, "unknown_usage": 0})


@dataclass(frozen=True)
class Limits:
    turns: int = 100
    tokens: int = 1_000_000
    execution_seconds: float = 600
    invalid_attempts: int = 3
    provider_attempts: int = 8
    context_bytes: int = 204_800
    output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    pending_cells: int = 32
    inbox_messages: int = 256
    storage_bytes: int = 256_000_000
    flush_seconds: float = 0.1

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.context_bytes < 4096:
            raise ValueError("context_bytes must be at least 4096")
        if self.output_bytes > self.context_bytes // 4:
            raise ValueError("Reserve context for observations: output_bytes <= context_bytes / 4")


@dataclass(frozen=True)
class CellKind:
    """A Cell kind. The built-in note never runs; callers add runnable kinds to act.

    ``fields`` maps each field besides ``kind`` to its JSON schema for the step tool.
    ``validate`` raises InvalidPatch for a malformed Cell body. The Owner decides how a
    Cell version runs.
    """

    name: str
    fields: Mapping[str, dict]
    prompt: str
    validate: Callable[[dict], None]


def _note(cell: dict) -> None:
    if not isinstance(cell.get("text"), str) or not cell["text"].strip():
        raise InvalidPatch("Notes require nonblank text")


NOTE = CellKind(
    name="note",
    fields={"text": {"type": "string"}},
    prompt='Note: {"kind":"note","text":"..."}.\n',
    validate=_note,
)


@dataclass(frozen=True)
class ControlKind:
    """A control type. Session admits it and the Owner handles it, both keyed by name.

    ``fields`` are required besides type and reason; a target_cell_id must name a
    recorded execution. The Owner handles lower ``order`` first.
    """

    order: int
    fields: tuple[str, ...] = ()


# Keep in step with shapes.ControlType.
CONTROLS = {
    "cancel": ControlKind(order=0, fields=("target_cell_id",)),
    "resolution": ControlKind(order=1, fields=("target_cell_id", "evidence_refs")),
    "conclusion": ControlKind(order=1, fields=("result",)),
}

# Control fields besides type: the step tool's JSON schema and the runtime check.
CONTROL_FIELDS: dict[str, tuple[dict, Callable[[object], bool]]] = {
    "target_cell_id": (
        {"type": "integer", "description": "id of a Cell with a recorded execution"},
        lambda value: type(value) is int,
    ),
    "evidence_refs": (
        {
            "type": "array",
            "items": {"type": "string"},
            "description": 'nonempty list of recorded "session:event:N" references',
        },
        lambda value: isinstance(value, list) and bool(value),
    ),
    "result": (
        {"type": "string", "enum": ["success", "failure"], "description": "success or failure"},
        lambda value: value in ("success", "failure"),
    ),
    "reason": (
        {"type": "string", "description": "nonblank text explaining why"},
        lambda value: isinstance(value, str) and bool(value.strip()),
    ),
}


def validate_cell(cell: object, kinds: Mapping[str, CellKind]) -> None:
    """Check a model-written Cell body; the runtime adds id and previous_id."""
    if not isinstance(cell, dict):
        raise InvalidPatch("cell must be an object")
    if {"id", "previous_id"} & set(cell):
        raise InvalidPatch("Omit id and previous_id from cell; the runtime assigns them")
    name = cell.get("kind")
    if not isinstance(name, str) or name not in kinds:
        raise InvalidPatch(f"Cell kind must be one of: {', '.join(kinds)}")
    kinds[name].validate(cell)
    if unknown := set(cell) - {"kind", *kinds[name].fields}:
        raise InvalidPatch(f"Unknown Cell fields: {sorted(unknown)}")


def validate_control(control: object) -> None:
    """Check a model-written control against its type in CONTROLS."""
    if not isinstance(control, dict):
        raise InvalidPatch("control must be an object")
    name = control.get("type")
    if not isinstance(name, str) or name not in CONTROLS:
        raise InvalidPatch(f"Control type must be one of: {', '.join(CONTROLS)}")
    fields = (*CONTROLS[name].fields, "reason")
    for field in fields:
        spec, check = CONTROL_FIELDS[field]
        if not check(control.get(field)):
            raise InvalidPatch(f"{name} control requires {field}: {spec['description']}")
    if unknown := set(control) - {"type", *fields}:
        raise InvalidPatch(f"{name} control takes {', '.join(fields)}; remove {sorted(unknown)}")


OPS = {
    "add": {"op", "cell"},
    "replace": {"op", "id", "cell"},
    "remove": {"op", "id"},
    "control": {"op", "control"},
}
USAGE = (
    'Use {"op":"add","cell":{...}}, {"op":"replace","id":N,"cell":{...}}, {"op":"remove","id":N}, '
    'or {"op":"control","control":{"type":...}}'
)


def present(value: dict) -> dict:
    """Drop null fields, which strict-schema providers send for omitted ones."""
    return {key: item for key, item in value.items() if item is not None}


def admit(
    state: State,
    ops: object,
    executions: dict[str, Execution],
    limit: int,
    kinds: Mapping[str, CellKind],
) -> tuple[State, list[Cell], list[ControlRequest]]:
    """Apply id-addressed ops; the runtime assigns ids, lineage, and positions.

    New versions append, and replaced or removed Cells turn stale in place, so the
    prompt prefix through the last unchanged Cell stays byte-identical. Stale Cells
    are compacted away once they outweigh live ones or the Cells exceed `limit`.
    Controls are returned in op order and never enter State.
    """
    if isinstance(ops, str):
        # Some models send the array JSON-encoded inside a string.
        with contextlib.suppress(ValueError):
            ops = json.loads(ops)
    if not isinstance(ops, list):
        raise InvalidPatch(f"ops must be an array. {USAGE}")
    next_id = state.runtime.next_cell_id
    ids = [cell.id for cell in state.cells if cell.id not in state.stale]
    retired: set[int] = set()
    new: list[Cell] = []
    controls: list[ControlRequest] = []
    for index, operation in enumerate(ops):
        operation = present(operation) if isinstance(operation, dict) else None
        if (
            operation is None
            or not isinstance(operation.get("op"), str)
            or operation["op"] not in OPS
            or set(operation) != OPS[operation["op"]]
        ):
            raise InvalidPatch(f"Invalid ops[{index}]. {USAGE}")
        if operation["op"] == "control":
            body = present(operation["control"]) if isinstance(operation["control"], dict) else None
            try:
                validate_control(body)
                request = ControlRequest.model_validate(body)
            except (InvalidPatch, ValidationError) as exc:
                raise InvalidPatch(f"ops[{index}]: {exc}") from None
            controls.append(request)
            continue
        previous = None
        if operation["op"] != "add":
            cell_id = operation["id"]
            if type(cell_id) is not int or cell_id not in ids:
                raise InvalidPatch(
                    f"ops[{index}]: Cell {cell_id} is not in cells (ids: {ids}). "
                    "Replace or remove a live Cell; new Cells get ids on acceptance"
                )
            if cell_id in retired:
                raise InvalidPatch(
                    f"ops[{index}]: Cell {cell_id} already has an op in this step; "
                    "use one op per Cell"
                )
            retired.add(cell_id)
            if operation["op"] == "remove":
                continue
            if (execution := executions.get(str(cell_id))) and execution.phase in (
                "queued",
                "started",
            ):
                raise InvalidPatch(
                    f"ops[{index}]: Cell {cell_id} is queued or running; add a new Cell, "
                    "or cancel it with a cancel control"
                )
            previous = cell_id
        body = present(operation["cell"]) if isinstance(operation["cell"], dict) else None
        try:
            validate_cell(body, kinds)
        except InvalidPatch as exc:
            raise InvalidPatch(f"ops[{index}]: {exc}") from None
        assert body is not None
        new.append(Cell.model_validate({"id": next_id + len(new), "previous_id": previous, **body}))
    candidate = state.model_copy(deep=True)
    stale = sorted({*state.stale, *retired})
    cells = [*candidate.cells, *new]
    live = [cell for cell in cells if cell.id not in stale]
    candidate.cells, candidate.stale = live, []
    candidate.revision += 1
    candidate.runtime = Runtime(
        next_cell_id=next_id + len(new), cells={}, inbox=[], usage=zero_usage()
    )
    context = {**candidate.model_dump(by_alias=True, exclude_unset=True), "runtime": {}}
    if len(encode(context).encode()) > limit:
        raise InvalidPatch("State exceeds context budget; remove Cells before adding work")
    # Keep stale Cells in place until they outweigh live ones or overflow the budget.
    stale_bytes = len(encode([cell for cell in cells if cell.id in stale]).encode())
    if len(encode(cells).encode()) <= limit and stale_bytes <= len(encode(live).encode()):
        candidate.cells, candidate.stale = cells, stale
    return candidate, new, controls
