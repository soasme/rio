"""The context as a Jupyter notebook.

The context is an nbformat v4 notebook, kept as plain JSON. Each step the model
sends an RFC 6902 JSON Patch against it. The runtime applies the patch, checks
that the result is still a valid notebook, and runs the code cells the patch
added or changed. Their outputs land in the notebook the model sees next.

Only changed cells get new outputs. To give them the variables earlier cells
defined, the executor replays every cell before the last changed one in a fresh
kernel, then keeps the old outputs of the replayed cells.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import re
import tempfile
import uuid
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

import jsonpatch
import nbformat

from rio.ai.types import JSONObject, JSONValue

type Notebook = JSONObject
type NotebookExecutor = Callable[[Notebook, list[int]], Awaitable[Notebook]]

CHARS_PER_TOKEN = 4
#: The largest text a single output keeps; the rest is cut with a note.
DEFAULT_OUTPUT_CHARS = 8_000
KERNEL_NAME = "python3"
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_PAPERMILL_ERROR_TAG = "papermill-error-cell-tag"


class NotebookError(ValueError):
    """A patch that cannot be applied, or that leaves an invalid notebook."""


def new_notebook() -> Notebook:
    return {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "kernelspec": {"name": KERNEL_NAME, "display_name": "Python 3", "language": "python"},
            "language_info": {"name": "python"},
        },
        "cells": [],
    }


def markdown_cell(text: str, *, role: str | None = None) -> JSONObject:
    """Return a markdown cell. `role` marks text the runtime wrote for a user or a reply."""
    metadata: JSONObject = {"rio": {"role": role}} if role else {}
    return {"id": _new_id(), "cell_type": "markdown", "metadata": metadata, "source": text}


def append_cell(notebook: Notebook, cell: JSONObject) -> Notebook:
    result = copy.deepcopy(notebook)
    result["cells"].append(cell)  # type: ignore[union-attr]
    return result


def apply_patch(notebook: Notebook, patch: JSONValue) -> Notebook:
    """Apply `patch` and return the new notebook. Does not change `notebook`.

    Cells the patch adds may omit `id`, `metadata`, `outputs`, and
    `execution_count`; they are filled in. Raises `NotebookError` when the
    patch fails or the result is not a valid nbformat v4 notebook.
    """
    if not isinstance(patch, list):
        raise NotebookError(f"patch must be a JSON array of operations, got {type(patch).__name__}")
    try:
        # Copy the patch too: values it adds become cells that are filled in below.
        result = jsonpatch.apply_patch(notebook, copy.deepcopy(patch))
    except (jsonpatch.JsonPatchException, jsonpatch.JsonPointerException, TypeError) as exc:
        raise NotebookError(f"patch failed: {exc}") from exc
    if not isinstance(result, dict) or not isinstance(result.get("cells"), list):
        raise NotebookError("the patched document has no `cells` array")
    if result.get("nbformat") != 4:
        raise NotebookError("invalid notebook: `nbformat` must be 4")
    for cell in result["cells"]:
        if isinstance(cell, dict):
            _fill_cell(cell)
    ids = [cell.get("id") for cell in result["cells"] if isinstance(cell, dict)]
    if len(ids) != len(set(ids)):
        raise NotebookError("cell ids must be unique")
    try:
        nbformat.validate(nbformat.from_dict(result))
    except nbformat.ValidationError as exc:
        raise NotebookError(f"invalid notebook: {exc.message}") from exc
    return result


def diff(before: Notebook, after: Notebook) -> list[JSONObject]:
    """Return the JSON Patch that turns `before` into `after`."""
    return list(jsonpatch.make_patch(before, after))


def changed_cells(before: Notebook, after: Notebook) -> list[int]:
    """Return the indices of code cells in `after` that are new or whose source changed."""
    sources = {cell["id"]: cell["source"] for cell in before["cells"]}  # type: ignore[union-attr]
    return [
        index
        for index, cell in enumerate(after["cells"])  # type: ignore[arg-type]
        if cell["cell_type"] == "code" and sources.get(cell["id"]) != cell["source"]
    ]


def render_notebook(notebook: Notebook) -> str:
    return json.dumps(notebook, indent=1, ensure_ascii=False)


def notebook_tokens(notebook: Notebook) -> int:
    return (len(render_notebook(notebook)) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def error_output(exc: BaseException) -> JSONObject:
    return {
        "output_type": "error",
        "ename": type(exc).__name__,
        "evalue": str(exc),
        "traceback": [],
    }


class PapermillExecutor:
    """Run changed cells with papermill in a fresh kernel started in `cwd`."""

    def __init__(
        self,
        cwd: Path | None = None,
        *,
        timeout_seconds: int | None = None,
        output_chars: int = DEFAULT_OUTPUT_CHARS,
    ) -> None:
        self.cwd = cwd
        self.timeout_seconds = timeout_seconds
        self.output_chars = output_chars

    async def __call__(self, notebook: Notebook, changed: list[int]) -> Notebook:
        if not changed:
            return notebook
        executed = await asyncio.to_thread(self._execute, notebook, max(changed) + 1)
        return merge_outputs(notebook, executed, changed, output_chars=self.output_chars)

    def _execute(self, notebook: Notebook, count: int) -> list[JSONObject]:
        import papermill

        ipc_manager = _ipc_kernel_manager()

        run = copy.deepcopy(notebook)
        run["cells"] = run["cells"][:count]  # type: ignore[index]
        with tempfile.TemporaryDirectory(prefix="rio-nb-") as directory:
            output = Path(directory) / "out.ipynb"
            # The failing cell's outputs are in the written notebook.
            with contextlib.suppress(papermill.PapermillExecutionError):
                papermill.execute_notebook(
                    nbformat.from_dict(run),
                    str(output),
                    kernel_name=KERNEL_NAME,
                    cwd=str(self.cwd) if self.cwd is not None else None,
                    progress_bar=False,
                    execution_timeout=self.timeout_seconds,
                    kernel_manager_class=ipc_manager,
                )
            cells = json.loads(output.read_text(encoding="utf-8"))["cells"]
        return [
            cell for cell in cells if _PAPERMILL_ERROR_TAG not in cell["metadata"].get("tags", [])
        ]


def _ipc_kernel_manager() -> type:
    from jupyter_client.manager import AsyncKernelManager

    class IpcKernelManager(AsyncKernelManager):
        """Talk to the kernel over local sockets instead of unencrypted TCP."""

        def __init__(self, **kwargs: object) -> None:
            super().__init__(transport="ipc", **kwargs)

    return IpcKernelManager


def merge_outputs(
    notebook: Notebook,
    executed: Sequence[JSONObject],
    changed: list[int],
    *,
    output_chars: int = DEFAULT_OUTPUT_CHARS,
) -> Notebook:
    """Copy outputs of changed cells, and of replayed cells that failed, into `notebook`.

    `executed` lines up with the first cells of `notebook`.
    """
    result = copy.deepcopy(notebook)
    cells = result["cells"]
    for index, ran in enumerate(executed):
        status = ran.get("metadata", {}).get("papermill", {}).get("status")  # type: ignore[union-attr]
        if index not in changed and status != "failed":
            continue
        cell = cells[index]  # type: ignore[index]
        if cell["cell_type"] != "code":
            continue
        cell["outputs"] = [_clean_output(item, output_chars) for item in ran.get("outputs", [])]  # type: ignore[union-attr]
        cell["execution_count"] = ran.get("execution_count")
    return result


def _clean_output(output: JSONObject, limit: int) -> JSONObject:
    """Strip terminal colors, drop binary data, and cut long text."""
    output = dict(output)
    kind = output.get("output_type")
    if kind == "stream":
        output["text"] = _cut(_joined(output.get("text", "")), limit)
    elif kind == "error":
        output["traceback"] = [_ANSI_RE.sub("", line) for line in output.get("traceback", [])]  # type: ignore[union-attr]
    elif kind in ("execute_result", "display_data"):
        data = output.get("data", {})
        output["data"] = {
            mime: _cut(_joined(value), limit) if isinstance(value, str | list) else value
            for mime, value in data.items()  # type: ignore[union-attr]
            if mime.startswith("text/") or mime.endswith("json")
        }
        output["metadata"] = {}
    return output


def _fill_cell(cell: dict) -> None:
    cell.setdefault("id", _new_id())
    cell.setdefault("metadata", {})
    if isinstance(cell.get("source"), list):
        cell["source"] = "".join(str(part) for part in cell["source"])
    cell.setdefault("source", "")
    if cell.get("cell_type") == "code":
        cell.setdefault("outputs", [])
        cell.setdefault("execution_count", None)


def _joined(value: JSONValue) -> str:
    return "".join(str(part) for part in value) if isinstance(value, list) else str(value)


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[{len(text) - limit} more characters cut by the runtime]"


def _new_id() -> str:
    return uuid.uuid4().hex[:8]
