"""Restricted JSON Patch admission for immutable, independent Cells."""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

import jsonpatch
from jsonpointer import JsonPointerException

from rio.agent.store import encode
from rio.truncate import DEFAULT_MAX_OUTPUT_BYTES


class InvalidPatch(ValueError):
    pass


@dataclass(frozen=True)
class Limits:
    rounds: int = 100
    tokens: int = 1_000_000
    execution_seconds: float = 600
    invalid_attempts: int = 3
    context_bytes: int = 204_800
    output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    pending_cells: int = 32
    inbox_messages: int = 256
    storage_bytes: int = 256_000_000
    flush_seconds: float = 0.1
    external_wakeup: bool = False

    def __post_init__(self):
        for name, value in asdict(self).items():
            if name != "external_wakeup" and value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.context_bytes < 4096 or self.output_bytes > self.context_bytes // 4:
            raise ValueError("Reserve context for observations: output <= context / 4")


@dataclass(frozen=True)
class CellKind:
    """A runnable Cell kind. Notes are built in and never run; callers add kinds to act.

    ``validate`` raises InvalidPatch for a malformed Cell. ``command`` returns the argv that
    runs one Cell version, and may write files into the given scratch directory.
    """

    name: str
    fields: frozenset[str]
    prompt: str
    validate: Callable[[dict], None]
    command: Callable[[dict, Path], list[str]]


def validate_cell(cell: object, kinds: Mapping[str, CellKind]) -> None:
    if not isinstance(cell, dict):
        raise InvalidPatch("Replace a whole Cell object")
    if type(cell.get("id")) is not int or cell["id"] < 1:
        raise InvalidPatch("Cell id must be a positive integer")
    if "previous_id" not in cell or (
        cell["previous_id"] is not None and type(cell["previous_id"]) is not int
    ):
        raise InvalidPatch("previous_id must be an integer or null")
    common = {"id", "previous_id", "kind"}
    name = cell.get("kind")
    if name == "note":
        allowed = common | {"text", "role", "target_cell_id", "evidence_refs", "result"}
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
        allowed = common | kinds[name].fields
        kinds[name].validate(cell)
    else:
        raise InvalidPatch(f"Cell kind must be one of: {', '.join(['note', *kinds])}")
    if set(cell) - allowed:
        raise InvalidPatch(f"Unknown Cell fields: {sorted(set(cell) - allowed)}")


def located(cells: list[dict], message: str, cell_id: object) -> str:
    """Append where Cell id cell_id lives, so a retry can address it correctly."""
    for position, cell in enumerate(cells):
        if cell["id"] == cell_id:
            return f"{message}. Cell id {cell_id} is at /cells/{position}"
    return message


def index_error(cells: list[dict], index: int) -> str:
    """Explain that /cells/N is an array position, and where Cell id N lives if it exists."""
    positions = f"0..{len(cells) - 1}" if cells else "none; append at /cells/-"
    message = (
        f"Cell index {index} out of range: /cells/N is the array position, not the Cell id "
        f"(valid positions: {positions})"
    )
    return located(cells, message, index)


def stale_error(cells: list[dict], index: int, previous_id: object) -> str:
    """Name the predecessor /cells/N requires, and where the given previous_id lives."""
    old_id = cells[index]["id"]
    message = (
        f"Stale predecessor: /cells/{index} holds Cell id {old_id}, so its "
        f"replacement needs previous_id: {old_id} (got {previous_id})"
    )
    if previous_id is None:
        return f"{message}. To add a new Cell instead, append at /cells/-"
    return located(cells, message, previous_id)


def admit(
    state: dict, patch: object, executions: dict, limit: int, kinds: Mapping[str, CellKind]
) -> tuple[dict, list[dict]]:
    if not isinstance(patch, list):
        raise InvalidPatch("patch must be an array")
    if not patch or patch[0] != {"op": "test", "path": "/revision", "value": state["revision"]}:
        raise InvalidPatch("First operation must test the current /revision")
    candidate = copy.deepcopy(state)
    new = []
    last_removed = len(state["cells"])
    for operation in patch:
        if not isinstance(operation, dict):
            raise InvalidPatch("Patch operations must be objects")
        op, path = operation.get("op"), operation.get("path", "")
        if not isinstance(path, str):
            raise InvalidPatch("Patch path must be a JSON pointer string")
        if op == "test":
            pass
        elif op == "add" and path == "/cells/-":
            cell = operation.get("value")
            validate_cell(cell, kinds)
            if cell["previous_id"] is not None:
                raise InvalidPatch("New lineages require previous_id: null")
            new.append(cell)
        elif (
            op in ("replace", "remove")
            and isinstance(path, str)
            and re.fullmatch(r"/cells/(0|[1-9][0-9]*)", path)
        ):
            index = int(path.rsplit("/", 1)[1])
            if index >= len(candidate["cells"]):
                raise InvalidPatch(index_error(candidate["cells"], index))
            old = candidate["cells"][index]
            if old["id"] >= state["runtime"]["next_cell_id"]:
                raise InvalidPatch(
                    f"/cells/{index} holds Cell id {old['id']}, added earlier in this patch; "
                    "edit or drop that operation instead of changing the Cell again"
                )
            if op == "remove":
                if index >= last_removed:
                    raise InvalidPatch("Remove cells in descending index order")
                last_removed = index
            else:
                cell = operation.get("value")
                validate_cell(cell, kinds)
                if cell["previous_id"] != old["id"]:
                    raise InvalidPatch(stale_error(candidate["cells"], index, cell["previous_id"]))
                if executions.get(str(old["id"]), {}).get("phase") in ("queued", "started"):
                    raise InvalidPatch("Cannot replace a queued or running Cell")
                new.append(cell)
        else:
            raise InvalidPatch("Only test, append, whole-Cell replacement and removal are allowed")
        if op in ("add", "replace") and cell["id"] != (
            state["runtime"]["next_cell_id"] + len(new) - 1
        ):
            raise InvalidPatch("Allocate consecutive IDs in operation order")
        try:
            candidate = jsonpatch.apply_patch(candidate, [operation])
        except (
            jsonpatch.JsonPatchException,
            JsonPointerException,
            KeyError,
            IndexError,
            TypeError,
        ) as exc:
            raise InvalidPatch(str(exc)) from exc
    candidate["runtime"]["inbox"] = []
    candidate["revision"] += 1
    candidate["runtime"]["next_cell_id"] += len(new)
    selected = {str(cell["id"]) for cell in candidate["cells"]}
    candidate["runtime"]["cells"] = {
        key: value
        for key, value in candidate["runtime"]["cells"].items()
        if key in selected or value.get("phase") != "finished" or value.get("uncertain")
    }
    context = {**candidate, "runtime": {}}
    if len(encode(context).encode()) > limit:
        raise InvalidPatch("State exceeds context budget; remove Cells before adding work")
    return candidate, new
