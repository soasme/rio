"""Spec definitions: the harness a run uses and the step call the model makes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError, field_validator

from rio.agent.notebook import NotebookExecutor
from rio.ai.tools import AgentTool, AgentToolResult, ToolCancellationToken, ToolUpdateCallback
from rio.ai.types import JSONObject, JSONValue


@dataclass(frozen=True, slots=True)
class HarnessSpec:
    name: str
    instructions: str
    #: Runs the changed cells of a patched notebook, e.g. a `KernelExecutor`.
    #: The caller owns its lifetime.
    executor: NotebookExecutor


STEP_TOOL_NAME = "skill_step"

STEP_TOOL_PARAMETERS: JSONObject = {
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
            "description": (
                "Your answer to the user. A non-blank reply ends the run; omit it to keep working."
            ),
        },
    },
    "required": ["patch"],
}


class StepArgs(BaseModel):
    """The parsed arguments of a `skill_step` call."""

    #: Checked further by `apply_patch`.
    patch: list[JSONObject]
    #: `None` unless the model sent a non-blank string; a blank reply is no answer.
    reply: str | None = None

    @field_validator("reply", mode="before")
    @classmethod
    def _blank_is_none(cls, value: object) -> str | None:
        return value if isinstance(value, str) and value.strip() else None

    @classmethod
    def parse(cls, arguments: Mapping[str, JSONValue]) -> StepArgs:
        """Return the arguments, or raise `ValueError` naming each invalid field."""
        try:
            return cls.model_validate(arguments)
        except ValidationError as exc:
            problems = "; ".join(
                f"`{'.'.join(map(str, e['loc']))}`: {e['msg']}" for e in exc.errors()
            )
            raise ValueError(problems) from None


async def _not_executed(
    tool_call_id: str,
    arguments: Mapping[str, JSONValue],
    signal: ToolCancellationToken | None = None,
    on_update: ToolUpdateCallback | None = None,
) -> AgentToolResult:
    raise RuntimeError(f"{STEP_TOOL_NAME!r} is applied by the context loop, never executed")


#: The one tool the model calls each step.
STEP_TOOL = AgentTool(
    name=STEP_TOOL_NAME,
    label="Skill Step",
    description="Patch the notebook; its changed code cells run.",
    parameters=STEP_TOOL_PARAMETERS,
    execute_fn=_not_executed,
)
