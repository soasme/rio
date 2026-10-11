"""The model-facing durable protocol: notes, plus whichever runnable Cell kinds are supplied."""

from collections.abc import Iterable

from rio.agent.state import CellKind
from rio.ai.tools import AgentTool

PROTOCOL = """You are Rio, an autonomous agent. Complete the goal in State.
Your entire working context is the supplied State. Use exactly one `step` tool call per round.
Use concise notes to retain facts; remove obsolete Cells and outputs to keep context small.
The goal and runtime are read-only. The only edits are RFC 6902 tests, append at /cells/-,
replace a whole /cells/N object, and remove /cells/N in descending index order.
N is the Cell's 0-based position in the cells array, NOT its id.
Begin every patch with {"op":"test","path":"/revision","value":REVISION}.
Cells have increasing integer IDs allocated from runtime.next_cell_id in patch order.
A new Cell has previous_id:null. A replacement gets a NEW id and previous_id equal to the
old Cell's id (never null), at the same array position. Never replace a queued or started Cell.
Replace or remove only Cells that existed before this patch, each at most once.
Notes: {"id":N,"previous_id":null,"kind":"note","text":"..."}.
"""

EXECUTION = """Each new runnable Cell version runs ONCE, in accepted order, in a fresh process.
Inspect output in runtime.cells (stdout is named output), plus completion notices in runtime.inbox.
An ordinary execution error pauses the queue. Consume it by accepting your next patch;
you can cancel queued work and add a corrected Cell in that patch. Ordinary errors
DO NOT require resolution notes. A resolution note is ONLY for UnknownExecution.
Cancel stale work using a note with role:"cancel", target_cell_id:N, and text explaining why.
Removing a Cell only removes context; it never cancels accepted work or erases history.
UnknownExecution means the process may have performed effects. Never assume it rolled back.
Investigate current state with a NEW runnable Cell before deciding whether to retry.
Resolve uncertainty with a note: role:"resolution", target_cell_id:N, evidence_refs containing
recorded "session:event:N" references, and text explaining the decision. History is retained.
Python processes can use `from rio.agent.api import timer, records`. timer(request_id,
unix_deadline, payload) durably registers one timer generation; it does not suspend or rerun
the process. records(after=0) retrieves journal events as data.
Files and package caches are not durable State.
"""

CONCLUSION = """To finish, append a note with role:"conclusion", result:"success" or "failure",
and text explaining the result. Ordinary text does not end the run. Success requires no outstanding
work or unresolved uncertainty, consumed observations, and passing configured validators.
If no work, timer or external wake-up remains, conclude; an idle patch is invalid.
"""


def system(kinds: Iterable[CellKind]) -> str:
    """Describe only what the agent can do; a note-only agent never hears about execution."""
    prompts = [kind.prompt for kind in kinds]
    execution = "".join(prompts) + EXECUTION if prompts else ""
    return PROTOCOL + execution + CONCLUSION


SCHEMA = {
    "type": "object",
    "properties": {
        "patch": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": ["test", "add", "replace", "remove"]},
                    "path": {"type": "string"},
                    "value": {},
                },
                "required": ["op", "path"],
            },
        }
    },
    "required": ["patch"],
    "additionalProperties": False,
}


async def _unavailable(*args, **kwargs):
    raise RuntimeError("step is applied transactionally by the owner")


STEP = AgentTool(
    name="step",
    label="Step",
    description="Commit a State patch and request Cell runs.",
    parameters=SCHEMA,
    execute_fn=_unavailable,
)
