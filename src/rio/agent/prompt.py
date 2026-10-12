"""The model-facing durable protocol: notes, plus whichever runnable Cell kinds are supplied."""

from collections.abc import Iterable

from rio.agent.state import NOTE, CellKind
from rio.ai.tools import AgentTool

PROTOCOL = """You are Rio, an autonomous agent. Complete the goal in State.
Your entire working context is the supplied State. Use exactly one `step` tool call per round.
Use concise notes to retain facts; remove obsolete Cells and outputs to keep context small.
The goal and runtime are read-only. A step is a list of ops that name Cells by id:
{"op":"add","cell":CELL} appends; {"op":"replace","id":N,"cell":CELL} puts a new version in
place; {"op":"remove","id":N} drops it from context. Use at most one op per existing Cell.
The runtime assigns ids and previous_id. Never replace a queued or started Cell.
Note: {"kind":"note","text":"..."}.
"""

EXECUTION = """Each new runnable Cell version runs ONCE, in accepted order, in a fresh process.
Inspect output in runtime.cells (stdout is named output), plus completion notices in runtime.inbox.
An ordinary execution error pauses the queue. Consume it by accepting your next step;
you can cancel queued work and add a corrected Cell in that step. Ordinary errors
DO NOT require resolution notes. A resolution note is ONLY for UnknownExecution.
Cancel stale work using a note with role:"cancel", target_cell_id:N, and text explaining why.
Removing a Cell only removes context; it never cancels accepted work or erases history.
UnknownExecution means the process may have performed effects. Never assume it rolled back.
Investigate current state with a NEW runnable Cell before deciding whether to retry.
Resolve uncertainty with a note: role:"resolution", target_cell_id:N, evidence_refs containing
recorded "session:event:N" references, and text explaining the decision. History is retained.
Files and package caches are not durable State.
"""

CONCLUSION = """To finish, add a note with role:"conclusion", result:"success" or "failure",
and text explaining the result. Ordinary text does not end the run. Success requires no outstanding
work or unresolved uncertainty, consumed observations, and passing configured validators.
If no work, timer or external wake-up remains, conclude; an idle step is invalid.
"""


def system(kinds: Iterable[CellKind]) -> str:
    """Describe only what the agent can do; a note-only agent never hears about execution."""
    prompts = [kind.prompt for kind in kinds]
    execution = "".join(prompts) + EXECUTION if prompts else ""
    return PROTOCOL + execution + CONCLUSION


def schema(kinds: Iterable[CellKind]) -> dict:
    """The step tool schema; Cells may use note fields and the fields of the given kinds."""
    kinds = list(kinds)
    fields = dict(NOTE)
    for kind in kinds:
        fields.update(kind.fields)
    cell = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["note", *(kind.name for kind in kinds)]},
            **fields,
        },
        "required": ["kind"],
        "additionalProperties": False,
    }
    op = {
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": ["add", "replace", "remove"]},
            "id": {"type": "integer"},
            "cell": cell,
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


STEP = AgentTool(
    name="step",
    description="Add, replace, or remove State Cells by id and request Cell runs.",
    parameters=schema(()),
)
