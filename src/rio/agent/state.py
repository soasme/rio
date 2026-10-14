"""Step admission for immutable, independent Cells addressed by id."""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass

from rio.agent.shapes import Cell, Execution, State
from rio.agent.store import encode
from rio.truncate import DEFAULT_MAX_OUTPUT_BYTES


class InvalidPatch(ValueError):
    pass


@dataclass(frozen=True)
class Limits:
    turns: int = 100
    tokens: int = 1_000_000
    execution_seconds: float = 600
    invalid_attempts: int = 3
    context_bytes: int = 204_800
    output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    pending_cells: int = 32
    inbox_messages: int = 256
    storage_bytes: int = 256_000_000
    flush_seconds: float = 0.1

    def __post_init__(self):
        for name, value in asdict(self).items():
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.context_bytes < 4096 or self.output_bytes > self.context_bytes // 4:
            raise ValueError("Reserve context for observations: output <= context / 4")


@dataclass(frozen=True)
class CellKind:
    """A runnable Cell kind. Notes are built in and never run; callers add kinds to act.

    ``fields`` maps each field besides ``kind`` to its JSON schema for the step tool.
    ``validate`` raises InvalidPatch for a malformed Cell body. The Owner decides how a
    Cell version runs.
    """

    name: str
    fields: Mapping[str, dict]
    prompt: str
    validate: Callable[[dict], None]


# Note fields besides kind, as JSON schemas for the step tool.
NOTE = {
    "text": {"type": "string"},
    "role": {"type": "string", "enum": ["cancel", "resolution", "conclusion"]},
    "target_cell_id": {"type": "integer"},
    "evidence_refs": {"type": "array", "items": {"type": "string"}},
    "result": {"type": "string", "enum": ["success", "failure"]},
}


def validate_cell(cell: object, kinds: Mapping[str, CellKind]) -> None:
    """Check a model-written Cell body; the runtime adds id and previous_id."""
    if not isinstance(cell, dict):
        raise InvalidPatch("cell must be an object")
    if {"id", "previous_id"} & set(cell):
        raise InvalidPatch("Omit id and previous_id from cell; the runtime assigns them")
    name = cell.get("kind")
    if name == "note":
        allowed = {"kind", *NOTE}
        if not isinstance(cell.get("text"), str) or not cell["text"].strip():
            raise InvalidPatch("Notes require nonblank text")
        role = cell.get("role")
        if role not in (None, "cancel", "resolution", "conclusion"):
            raise InvalidPatch("Unknown note role")
        if role in ("cancel", "resolution") and type(cell.get("target_cell_id")) is not int:
            raise InvalidPatch("Control note requires target_cell_id")
        if role == "resolution" and (
            not isinstance(cell.get("evidence_refs"), list) or not cell["evidence_refs"]
        ):
            raise InvalidPatch("Resolution requires recorded evidence_refs")
        if role == "conclusion" and cell.get("result") not in ("success", "failure"):
            raise InvalidPatch("Conclusion requires result: success or failure")
    elif isinstance(name, str) and name in kinds:
        allowed = {"kind", *kinds[name].fields}
        kinds[name].validate(cell)
    else:
        raise InvalidPatch(f"Cell kind must be one of: {', '.join(['note', *kinds])}")
    if set(cell) - allowed:
        raise InvalidPatch(f"Unknown Cell fields: {sorted(set(cell) - allowed)}")


OPS = {"add": {"op", "cell"}, "replace": {"op", "id", "cell"}, "remove": {"op", "id"}}
USAGE = (
    'Use {"op":"add","cell":{...}}, {"op":"replace","id":N,"cell":{...}}, or {"op":"remove","id":N}'
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
) -> tuple[State, list[Cell]]:
    """Apply id-addressed ops; the runtime assigns ids, lineage, and positions."""
    if not isinstance(ops, list):
        raise InvalidPatch(f"ops must be an array. {USAGE}")
    next_id = state["runtime"]["next_cell_id"]
    ids = [cell["id"] for cell in state["cells"]]
    replaced: dict[int, Cell] = {}
    removed: set[int] = set()
    new: list[Cell] = []
    for index, operation in enumerate(ops):
        operation = present(operation) if isinstance(operation, dict) else None
        if (
            operation is None
            or operation.get("op") not in ("add", "replace", "remove")
            or set(operation) != OPS[operation["op"]]
        ):
            raise InvalidPatch(f"Invalid ops[{index}]. {USAGE}")
        previous = None
        if operation["op"] != "add":
            cell_id = operation["id"]
            if type(cell_id) is not int or cell_id not in ids:
                raise InvalidPatch(
                    f"ops[{index}]: Cell {cell_id} is not in cells (ids: {ids}). "
                    "Replace or remove an existing Cell; new Cells get ids on acceptance"
                )
            if cell_id in replaced or cell_id in removed:
                raise InvalidPatch(
                    f"ops[{index}]: Cell {cell_id} already has an op in this step; "
                    "use one op per Cell"
                )
            if operation["op"] == "remove":
                removed.add(cell_id)
                continue
            if executions.get(str(cell_id), {}).get("phase") in ("queued", "started"):
                raise InvalidPatch(
                    f"ops[{index}]: Cell {cell_id} is queued or running; add a new Cell, "
                    "or cancel it with a cancel note"
                )
            previous = cell_id
        body = present(operation["cell"]) if isinstance(operation["cell"], dict) else None
        try:
            validate_cell(body, kinds)
        except InvalidPatch as exc:
            raise InvalidPatch(f"ops[{index}]: {exc}") from None
        cell = {"id": next_id + len(new), "previous_id": previous, **body}
        if previous is not None:
            replaced[previous] = cell
        new.append(cell)
    cells = [replaced.get(c["id"], c) for c in state["cells"] if c["id"] not in removed]
    cells += [cell for cell in new if cell["previous_id"] is None]
    candidate = copy.deepcopy({**state, "cells": cells})
    candidate["revision"] += 1
    candidate["runtime"] = {
        "next_cell_id": next_id + len(new),
        "cells": {},
        "inbox": [],
        "usage": {},
    }
    context = {**candidate, "runtime": {}}
    if len(encode(context).encode()) > limit:
        raise InvalidPatch("State exceeds context budget; remove Cells before adding work")
    return candidate, new
