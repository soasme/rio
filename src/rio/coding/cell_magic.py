"""The `%cell` line magic: read back a cell the notebook no longer shows.

The session journal records every step's patch, so every version of every cell
is in it. `%cell ID` replays the active branch and prints the cell's last
version: source, then outputs. A cell still in the notebook under that id (a
summary written in place) is skipped in favour of its earlier content.

A session's kernel loads it with the code `journal_startup` returns, which
binds the journal the kernel reads.
"""

from __future__ import annotations

import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor

import jsonpatch

from rio.agent import new_notebook
from rio.ai.types import JSONObject
from rio.coding.session_store import (
    JsonlSessionStorage,
    SessionEntry,
    SessionStorage,
    SqliteSessionStorage,
    StateResetEntry,
    StepEntry,
    latest_leaf_id,
    path_to_entry,
)


def journal_startup(storage: SessionStorage) -> str:
    """Return kernel startup code that loads `%cell` reading `storage`."""
    bind = ""
    if isinstance(storage, (JsonlSessionStorage, SqliteSessionStorage)):
        kind = type(storage).__name__
        bind = (
            f"\nfrom rio.coding.session_store import {kind} as _rio_journal"
            f"\nget_ipython()._rio_journal = _rio_journal({str(storage.path)!r})"
            "\ndel _rio_journal"
        )
    return "%load_ext rio.coding.cell_magic" + bind


def recall(entries: list[SessionEntry], cell_id: str) -> JSONObject:
    """Return the last version of `cell_id` on the active branch, before any summary
    that took its place under the same id."""
    leaf = latest_leaf_id(entries)
    versions: list[JSONObject] = []
    notebook = new_notebook()
    for entry in path_to_entry(entries, leaf) if leaf is not None else []:
        if isinstance(entry, StateResetEntry):
            notebook = copy.deepcopy(entry.notebook)
        elif isinstance(entry, StepEntry):
            notebook = jsonpatch.apply_patch(notebook, entry.patch)
        else:
            continue
        for cell in notebook["cells"]:  # type: ignore[union-attr]
            if cell["id"] == cell_id:  # type: ignore[index]
                versions.append(cell)  # type: ignore[arg-type]
    if not versions:
        raise KeyError(cell_id)
    current = next(
        (cell for cell in notebook["cells"] if cell["id"] == cell_id),  # type: ignore[union-attr,index]
        None,
    )
    if current is not None:
        earlier = [cell for cell in versions if _content(cell) != _content(current)]
        versions = earlier or versions
    return versions[-1]


def render_cell(cell: JSONObject) -> str:
    """Return a cell's source, then the text of its outputs."""
    parts = [f"[{cell['cell_type']} cell {cell['id']}]", _text(cell.get("source", ""))]
    for output in cell.get("outputs", []):  # type: ignore[union-attr]
        kind = output["output_type"]  # type: ignore[index]
        if kind == "stream":
            parts.append(_text(output["text"]))  # type: ignore[index]
        elif kind == "error":
            parts.append(f"{output['ename']}: {output['evalue']}")  # type: ignore[index]
        else:
            parts.append(_text(output.get("data", {}).get("text/plain", "")))  # type: ignore[union-attr]
    return "\n".join(part.rstrip("\n") for part in parts if part)


def _content(cell: JSONObject) -> tuple[object, str]:
    return cell["cell_type"], _text(cell.get("source", ""))


def _text(value: object) -> str:
    return "".join(value) if isinstance(value, list) else str(value)


class CellError(Exception):
    """A `%cell` lookup that found nothing."""

    def _render_traceback_(self) -> list[str]:
        return [f"CellError: {self}"]


def load_ipython_extension(ipython) -> None:  # noqa: ANN001 - IPython's own hook signature
    def cell(line: str) -> None:
        cell_id = line.strip()
        storage = getattr(ipython, "_rio_journal", None)
        if not cell_id:
            raise CellError("usage: %cell ID")
        if storage is None:
            raise CellError("this session keeps no journal")
        # The kernel's own event loop is running; read on a thread with a fresh one.
        with ThreadPoolExecutor(max_workers=1) as pool:
            entries = pool.submit(asyncio.run, storage.read_all()).result()
        try:
            print(render_cell(recall(entries, cell_id)))
        except KeyError:
            raise CellError(f"no cell {cell_id} in this session") from None

    ipython.register_magic_function(cell, magic_kind="line", magic_name="cell")
