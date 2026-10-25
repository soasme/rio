"""The model-facing durable protocol: Cells as memory, controls as actions."""

from collections.abc import Iterable

from rio.agent.shapes import State
from rio.agent.state import CONTROL_FIELDS, CONTROLS, CellKind
from rio.agent.store import encode
from rio.ai.messages import TextContent, UserMessage
from rio.ai.tools import AgentTool

PROTOCOL = """You are Rio, an autonomous agent. Complete the goal in State.
Your entire working context is the supplied State. Use exactly one `step` tool call per response.
State arrives as JSON lines: schema, id and goal; then each Cell and settled execution in
context order; then stale, revision and runtime. Cells are your memory. Use concise notes to
retain facts; remove obsolete Cells and outputs to keep context small. The goal and runtime
are read-only.
A step is a list of ops.
Cell ops name Cells by id: {"op":"add","cell":CELL} appends; {"op":"replace","id":N,"cell":CELL}
appends a new version of Cell N; {"op":"remove","id":N} drops it from context. Use at most one op
per live Cell. The runtime assigns ids and previous_id. Never replace a queued or started Cell.
Cells listed in stale were replaced or removed: ignore them and their executions; they
cannot take ops and are compacted away later.
Controls are actions, not Cells: {"op":"control","control":CONTROL}. They never enter State.
"""

EXECUTION = """Each new runnable Cell version runs ONCE, in accepted order.
A finished execution appears once, as an {"execution":{...}} line after its Cell; queued,
running and unresolved ones are in runtime.cells. Completion notices are in runtime.inbox.
To fit context, a stream may show only its tail: the record's omitted counts the
leading bytes left out per stream. The execution_finished event in result.evidence_refs
keeps the recorded output. Remove Cells you no longer need; a "context" observation
names Cells whose runtime records did not fit.
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


def state_messages(state: State) -> list[UserMessage]:
    """State as cacheable blocks: the header, each Cell and settled execution, then the
    per-turn fields.

    Cells and settled executions only append between compactions, so each turn's prefix
    through its last unchanged block matches the previous request.
    """
    header = encode({"schema": state.schema_version, "id": state.id, "goal": state.goal})
    # A settled execution follows the Cells that existed when it settled.
    entries = [((cell.id, 1), encode(cell)) for cell in state.cells] + [
        ((settled.next_cell_id, 0), encode({"execution": settled.cell}))
        for settled in state.settled
    ]
    entries.sort(key=lambda entry: entry[0])
    blocks = [TextContent(text=text) for text in (header, *(text for _, text in entries))]
    tail = encode({"stale": state.stale, "revision": state.revision, "runtime": state.runtime})
    return [UserMessage(content=blocks, cache=True), UserMessage(content=tail)]


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
    fields: dict[str, dict] = {}
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
