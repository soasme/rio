"""The prompt a step sends: instructions, the notebook protocol, and the notebook.

Each request is the system prompt plus one user message holding the whole
notebook as JSON. The model answers with one `skill_step` call: a JSON Patch
against that notebook, and an optional reply that ends the run.
"""

from __future__ import annotations

from collections.abc import Mapping

from rio.agent.notebook import Notebook, notebook_tokens, render_notebook
from rio.ai.messages import AgentMessage, UserMessage
from rio.ai.tools import AgentTool, AgentToolResult, ToolCancellationToken, ToolUpdateCallback
from rio.ai.types import JSONObject, JSONValue

STEP_TOOL_NAME = "skill_step"


async def _not_executed(
    tool_call_id: str,
    arguments: Mapping[str, JSONValue],
    signal: ToolCancellationToken | None = None,
    on_update: ToolUpdateCallback | None = None,
) -> AgentToolResult:
    raise RuntimeError(f"{STEP_TOOL_NAME!r} is applied by the notebook loop, never executed")


def skill_step_tool() -> AgentTool:
    """The one tool the model calls each step."""
    parameters: JSONObject = {
        "type": "object",
        "properties": {
            "patch": {
                "type": "array",
                "description": (
                    "RFC 6902 JSON Patch operations applied to the notebook. Code cells "
                    "the patch adds or whose source it changes are run."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "op": {
                            "type": "string",
                            "enum": ["add", "remove", "replace", "move", "copy", "test"],
                        },
                        "path": {"type": "string"},
                        "from": {"type": "string"},
                        "value": {},
                    },
                    "required": ["op", "path"],
                },
            },
            "reply": {
                "type": "string",
                "description": "Your answer to the user. Setting it ends the run.",
            },
        },
        "required": ["patch"],
    }
    return AgentTool(
        name=STEP_TOOL_NAME,
        label="Skill Step",
        description="Patch the notebook; its changed code cells run.",
        parameters=parameters,
        execute_fn=_not_executed,
    )


def notebook_protocol(limit_tokens: int) -> str:
    """Return the system-prompt section that teaches the model to work in its notebook."""
    return (
        "## Your context is a notebook\n\n"
        "Your whole context is one Jupyter notebook (nbformat v4 JSON), sent in full every "
        f"step. Each reply calls `{STEP_TOOL_NAME}` with a JSON Patch (RFC 6902) against it.\n\n"
        "- Add or edit code cells to act. Cells are Python run by IPython: use `!cmd` or "
        "`%%bash` for shell commands, and Python for reading and writing files.\n"
        "- After the patch, the code cells it added or changed run in order, and their "
        "outputs appear in the notebook. A cell that fails stops the ones after it. Other "
        "cells keep their outputs and never run again.\n"
        "- All cells share one live kernel, so variables build up across steps. Editing a "
        "cell's source runs it again; editing only its outputs does not. After a resume or "
        "rewind the kernel starts empty.\n"
        "- A new cell needs only `cell_type` and `source`, e.g. "
        '`{"op": "add", "path": "/cells/-", "value": {"cell_type": "code", "source": "..."}}`.\n'
        "- Markdown cells are notes. Cells with `metadata.rio.role` are user messages and "
        "your replies.\n"
        "- Manage your context: remove stale outputs and cells, and keep notes of decisions, "
        "file paths, and exact values. "
        f"The notebook must stay under about {limit_tokens} tokens; each request shows its size.\n"
        "- Set `reply` to answer the user and end the run, once the task is done or blocked."
    )


def build_messages(
    notebook: Notebook, *, limit_tokens: int, error_note: str | None = None
) -> list[AgentMessage]:
    """Return the request messages for `notebook`, plus a transient correction."""
    text = (
        f"```json\n{render_notebook(notebook)}\n```\n"
        f"[notebook: ~{notebook_tokens(notebook)}/{limit_tokens} tokens]"
    )
    if error_note:
        text += f"\n\nRejected reply: {error_note}\nRetry with a corrected reply."
    return [UserMessage(content=text)]
