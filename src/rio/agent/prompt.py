"""The model-facing durable protocol: Cells as memory, controls as actions."""

from collections.abc import Iterable

from rio.agent.state import CONTROL_FIELDS, CONTROLS, CellKind
from rio.ai.tools import AgentTool

PROTOCOL = """You are Rio, an autonomous agent. Complete the goal in State.
Your entire working context is the supplied State. Use exactly one `step` tool call per response.
Cells are your memory. Use concise notes to retain facts; remove obsolete Cells and outputs
to keep context small. The goal and runtime are read-only. A step is a list of ops.
Cell ops name Cells by id: {"op":"add","cell":CELL} appends; {"op":"replace","id":N,"cell":CELL}
puts a new version in place; {"op":"remove","id":N} drops it from context. Use at most one op
per existing Cell. The runtime assigns ids and previous_id. Never replace a queued or started Cell.
Controls are actions, not Cells: {"op":"control","control":CONTROL}. They never enter State.
"""

EXECUTION = """Each new runnable Cell version runs ONCE, in accepted order.
Inspect output streams in runtime.cells, plus completion notices in runtime.inbox.
An ordinary execution error pauses the queue. Consume it by accepting your next step;
you can cancel queued work and add a corrected Cell in that step. Ordinary errors
DO NOT require resolution. Resolution is ONLY for UnknownExecution.
Cancel stale work: {"type":"cancel","target_cell_id":N,"reason":"..."}.
Removing a Cell only removes context; it never cancels accepted work or erases history.
UnknownExecution means the Cell may have performed effects. Never assume it rolled back.
Investigate current state with a NEW runnable Cell before deciding whether to retry.
Resolve uncertainty: {"type":"resolution","target_cell_id":N,"evidence_refs":["session:event:N"],
"reason":"..."}, citing recorded observations. History is retained.
"""

CONCLUSION = """To finish: {"type":"conclusion","result":"success" or "failure","reason":"..."}.
Ordinary text does not end the run. Success requires no outstanding work or unresolved
uncertainty, consumed observations, and passing configured validators.
If no work, timer or external wake-up remains, conclude; an idle step is invalid.
"""


def system(kinds: Iterable[CellKind]) -> str:
    """Describe only what the agent can do; a note-only agent never hears about execution."""
    kinds = list(kinds)
    runnable = any(kind.name != "note" for kind in kinds)
    return (
        PROTOCOL
        + "".join(kind.prompt for kind in kinds)
        + (EXECUTION if runnable else "")
        + CONCLUSION
    )


def schema(kinds: Iterable[CellKind]) -> dict:
    """The step tool schema for the given Cell kinds, plus controls.

    Each object is flat with an enum discriminator, since some providers reject oneOf;
    the runtime checks the exact fields of each kind and control type.
    """
    kinds = list(kinds)
    fields = {}
    for kind in kinds:
        fields.update(kind.fields)
    cell = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": [kind.name for kind in kinds]},
            **fields,
        },
        "required": ["kind"],
        "additionalProperties": False,
    }
    control = {
        "type": "object",
        "description": "Required fields by type: "
        + "; ".join(
            f"{name}: {', '.join((*kind.fields, 'reason'))}" for name, kind in CONTROLS.items()
        ),
        "properties": {
            "type": {"type": "string", "enum": list(CONTROLS)},
            **{field: spec for field, (spec, _) in CONTROL_FIELDS.items()},
        },
        "required": ["type", "reason"],
        "additionalProperties": False,
    }
    op = {
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": ["add", "replace", "remove", "control"]},
            "id": {"type": "integer"},
            "cell": cell,
            "control": control,
        },
        "required": ["op"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {"ops": {"type": "array", "items": op}},
        "required": ["ops"],
        "additionalProperties": False,
    }


def step_tool(parameters: dict) -> AgentTool:
    """The step tool for a turn's saved schema."""
    return AgentTool(
        name="step",
        description="Edit State Cells by id, request Cell runs, and issue controls.",
        parameters=parameters,
    )
