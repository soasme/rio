from __future__ import annotations

import contextlib
import io
import itertools

from rio.agent import HarnessSpec, Notebook
from rio.ai import AssistantDoneEvent, AssistantMessage, TextContent, ToolCall
from rio.ai.types import JSONObject

_call_ids = itertools.count()


def add_code(source: str) -> JSONObject:
    """A patch operation that appends a code cell."""
    return {"op": "add", "path": "/cells/-", "value": {"cell_type": "code", "source": source}}


def add_markdown(source: str) -> JSONObject:
    """A patch operation that appends a markdown cell."""
    return {"op": "add", "path": "/cells/-", "value": {"cell_type": "markdown", "source": source}}


def step_response(*, reasoning: str = "", patch: list | None = None, reply: str | None = None):
    """Build one FakeProvider stream: reasoning text and a single `skill_step` call."""
    content = []
    if reasoning:
        content.append(TextContent(text=reasoning))
    arguments: JSONObject = {"patch": list(patch or [])}
    if reply is not None:
        arguments["reply"] = reply
    content.append(ToolCall(id=f"call-{next(_call_ids)}", name="skill_step", arguments=arguments))
    message = AssistantMessage(content=content, stop_reason="toolUse")
    return [AssistantDoneEvent(reason="toolUse", message=message)]


async def fake_executor(notebook: Notebook, changed: list[int]) -> Notebook:
    """Run changed code cells in-process with `exec`, replaying earlier cells first."""
    namespace: dict = {}
    for index, cell in enumerate(notebook["cells"][: max(changed, default=-1) + 1]):
        if cell["cell_type"] != "code":
            continue
        stdout = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout):
                exec(cell["source"], namespace)
            outputs = [{"output_type": "stream", "name": "stdout", "text": stdout.getvalue()}]
        except Exception as exc:
            outputs = [
                {
                    "output_type": "error",
                    "ename": type(exc).__name__,
                    "evalue": str(exc),
                    "traceback": [],
                }
            ]
        if index in changed:
            cell["outputs"] = outputs
            cell["execution_count"] = index + 1
    return notebook


def make_skill(**overrides) -> HarnessSpec:
    defaults = dict(name="demo", instructions="You are the demo skill.", executor=fake_executor)
    defaults.update(overrides)
    return HarnessSpec(**defaults)
