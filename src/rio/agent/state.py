"""Restricted JSON Patch admission for immutable, independent Cells."""

from __future__ import annotations

import copy
import re
import tomllib
from dataclasses import asdict, dataclass

import jsonpatch
from jsonpointer import JsonPointerException

from rio.agent.store import encode


class InvalidPatch(ValueError):
    pass


@dataclass(frozen=True)
class Limits:
    rounds: int = 100
    tokens: int = 1_000_000
    execution_seconds: float = 600
    invalid_attempts: int = 3
    context_bytes: int = 160_000
    output_bytes: int = 16_000
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


def script_metadata(source: str) -> dict:
    blocks = re.findall(r"(?m)^# /// script\n((?:#(?: .*|)\n)*)# ///\s*$", source)
    if len(blocks) != 1:
        raise InvalidPatch("Code requires one PEP 723 script block with dependencies")
    try:
        metadata = tomllib.loads("\n".join(line[2:] for line in blocks[0].splitlines()))
    except tomllib.TOMLDecodeError as exc:
        raise InvalidPatch(str(exc)) from exc
    if not isinstance(metadata.get("dependencies"), list) or not all(
        isinstance(item, str) for item in metadata["dependencies"]
    ):
        raise InvalidPatch("PEP 723 dependencies must be an array of strings")
    if "requires-python" in metadata and not isinstance(metadata["requires-python"], str):
        raise InvalidPatch("requires-python must be a string")
    return metadata


def validate_cell(cell: object) -> None:
    if not isinstance(cell, dict):
        raise InvalidPatch("Replace a whole Cell object")
    if type(cell.get("id")) is not int or cell["id"] < 1:
        raise InvalidPatch("Cell id must be a positive integer")
    if "previous_id" not in cell or (
        cell["previous_id"] is not None and type(cell["previous_id"]) is not int
    ):
        raise InvalidPatch("previous_id must be an integer or null")
    common = {"id", "previous_id", "kind"}
    if cell.get("kind") == "code":
        allowed = common | {"runtime", "source"}
        if cell.get("runtime") != "python" or not isinstance(cell.get("source"), str):
            raise InvalidPatch("Code requires runtime: python and a source string")
        script_metadata(cell["source"])
    elif cell.get("kind") == "note":
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
    else:
        raise InvalidPatch("Cell kind must be note or code")
    if set(cell) - allowed:
        raise InvalidPatch(f"Unknown Cell fields: {sorted(set(cell) - allowed)}")


def admit(state: dict, patch: object, executions: dict, limit: int) -> tuple[dict, list[dict]]:
    if not isinstance(patch, list):
        raise InvalidPatch("patch must be an array")
    if not patch or patch[0] != {"op": "test", "path": "/revision", "value": state["revision"]}:
        raise InvalidPatch("First operation must test the current /revision")
    candidate = copy.deepcopy(state)
    new = []
    touched = set()
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
            validate_cell(cell)
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
                raise InvalidPatch("Cell index out of range")
            old = candidate["cells"][index]
            if old["id"] in touched or old["id"] >= state["runtime"]["next_cell_id"]:
                raise InvalidPatch("Only one operation per existing Cell per patch")
            touched.add(old["id"])
            if op == "remove":
                if index >= last_removed:
                    raise InvalidPatch("Remove cells in descending index order")
                last_removed = index
            else:
                cell = operation.get("value")
                validate_cell(cell)
                if cell["previous_id"] != old["id"]:
                    raise InvalidPatch("Stale predecessor")
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
