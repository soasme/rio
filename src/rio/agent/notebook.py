"""The context as a Jupyter notebook.

The context is an nbformat v4 notebook, kept as plain JSON. Each step the model
sends an RFC 6902 JSON Patch against it. The runtime applies the patch, checks
that the result is still a valid notebook, and runs the code cells the patch
added or changed. Their outputs land in the notebook the model sees next.

Only changed cells run, in one kernel that lives across steps, so variables
build up and no cell runs twice by accident. Other cells keep their outputs.
"""

from __future__ import annotations

import copy
import json
import re
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

import jsonpatch
import nbformat
from nbclient import NotebookClient

from rio.ai.types import JSONObject, JSONValue

type Notebook = JSONObject
type NotebookExecutor = Callable[[Notebook, list[int]], Awaitable[Notebook]]

CHARS_PER_TOKEN = 4
#: The largest text a single output keeps; the rest is cut with a note.
DEFAULT_OUTPUT_CHARS = 8_000
#: How long one cell may run before the kernel is interrupted.
DEFAULT_TIMEOUT_SECONDS = 600
KERNEL_NAME = "python3"
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


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


class KernelExecutor:
    """Run changed cells in one live kernel, so variables build up across steps.

    The kernel starts on first use in `cwd` and lives until `shutdown()`. Only
    the cells passed in run; earlier cells never run again. Cells run in
    notebook order, and the first one that fails stops the rest. `startup` is
    code run once when the kernel starts, such as `%load_ext` for magics.
    """

    def __init__(
        self,
        cwd: Path | None = None,
        *,
        startup: str = "",
        timeout_seconds: int | None = DEFAULT_TIMEOUT_SECONDS,
        output_chars: int = DEFAULT_OUTPUT_CHARS,
    ) -> None:
        self.cwd = cwd
        self.startup = startup
        self.timeout_seconds = timeout_seconds
        self.output_chars = output_chars
        self._client: NotebookClient | None = None
        self._sockets: tempfile.TemporaryDirectory | None = None

    async def __call__(self, notebook: Notebook, changed: list[int]) -> Notebook:
        result = copy.deepcopy(notebook)
        if not changed:
            return result
        client = await self._started()
        failed = False
        for index in changed:
            cell = result["cells"][index]  # type: ignore[index]
            if failed:
                cell["outputs"], cell["execution_count"] = [], None
                continue
            node = await _execute(client, cell)
            outputs = [_clean_output(dict(item), self.output_chars) for item in node.outputs]
            cell["outputs"] = outputs
            cell["execution_count"] = node.get("execution_count")
            failed = any(item["output_type"] == "error" for item in cell["outputs"])
        return result

    @property
    def is_running(self) -> bool:
        return self._client is not None

    async def shutdown(self) -> None:
        """Stop the kernel. The next call starts a fresh one with no variables."""
        client, self._client = self._client, None
        if client is not None:
            client.kc.stop_channels()
            await client.km.shutdown_kernel(now=True)
        if self._sockets is not None:
            self._sockets.cleanup()
            self._sockets = None

    async def _started(self) -> NotebookClient:
        if self._client is not None:
            return self._client
        from jupyter_client.manager import AsyncKernelManager

        # Local sockets in a private directory: no open TCP ports, and no
        # socket files in the project.
        self._sockets = tempfile.TemporaryDirectory(prefix="rio-kernel-")
        manager = AsyncKernelManager(
            kernel_name=KERNEL_NAME,
            transport="ipc",
            ip=str(Path(self._sockets.name) / "kernel"),
        )
        await manager.start_kernel(cwd=str(self.cwd) if self.cwd is not None else None)
        client = NotebookClient(
            nbformat.v4.new_notebook(),
            km=manager,
            kernel_name=KERNEL_NAME,
            allow_errors=True,
            timeout=self.timeout_seconds,
            interrupt_on_timeout=True,
        )
        client.kc = manager.client()
        client.kc.start_channels()
        await client.kc.wait_for_ready(timeout=60)
        self._client = client
        if self.startup:
            setup = await _execute(client, nbformat.v4.new_code_cell(self.startup), history=False)
            errors = [item for item in setup.outputs if item["output_type"] == "error"]
            if errors:
                await self.shutdown()
                raise RuntimeError(f"kernel startup failed: {errors[0]['evalue']}")
        return client


async def _execute(client: NotebookClient, cell: JSONObject, *, history: bool = True):
    """Run one cell. nbclient writes the result into its own notebook, so give it one."""
    node = nbformat.from_dict(cell)
    client.nb = nbformat.v4.new_notebook(cells=[node])
    return await client.async_execute_cell(node, 0, store_history=history)


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
