"""The prompt a step sends: the system prompt and the notebook.

Each request is the system prompt plus one user message holding the whole
notebook as JSON. The model answers with one `skill_step` call: a JSON Patch
against that notebook, and an optional reply that ends the run.
"""

from __future__ import annotations

from rio.agent.notebook import Notebook, notebook_tokens, render_notebook, stale_cells
from rio.agent.spec import STEP_TOOL_NAME, HarnessSpec
from rio.ai.messages import AgentMessage, UserMessage


def system_prompt(spec: HarnessSpec, limit_tokens: int) -> str:
    """Return the spec's instructions, then the context protocol: a step procedure
    plus the terms it uses."""
    return (
        spec.instructions + "\n\n## Context protocol\n\n"
        "Your context is one Jupyter notebook N (nbformat v4 JSON), sent in full each step.\n\n"
        "Step:\n"
        "1. Review every cell of N. Keep a cell only while you still need it. Otherwise "
        "remove it, or replace it with a markdown cell summarizing what matters: decisions, "
        f"file paths, exact values.{_archive_note(spec)}\n"
        f"2. Call `{STEP_TOOL_NAME}` with `patch` (RFC 6902 JSON Patch on N, including the "
        "review's edits) and optional `reply`.\n"
        "3. N' = patch(N). Rejected, and you retry, if N' is not a valid notebook, a run cell "
        "reads a name only a stale cell defined, or N' is over L and not smaller than N.\n"
        "4. Run cells of N' run in order until one fails; outputs go into N'. Other cells keep "
        "their outputs and never rerun.\n"
        "5. A non-blank `reply` is appended to N' and ends the run; set it when the task is "
        "done or blocked. Else N' is the next N.\n\n"
        "Terms:\n"
        f"- L: ~{limit_tokens} tokens; each request shows N's size. Keep N under L with the "
        "step 1 review.\n"
        "- Code cell: Python run by IPython. All cells share one live kernel, so variables "
        "persist across steps. Shell: `!cmd` or `%%bash`. Files: Python.\n"
        "- Markdown cell: a note. A cell with `metadata.rio.role` is a user message or your "
        "reply.\n"
        "- New cell: needs only `cell_type` and `source`, e.g. "
        '`{"op": "add", "path": "/cells/-", "value": {"cell_type": "code", "source": "..."}}`.\n'
        "- Run cell: a code cell that is new, whose `source` changed, or whose stamp was "
        "removed. Editing only outputs runs nothing.\n"
        "- `N.metadata.rio.kernel`: current kernel `id` and `running`.\n"
        "- Stamp: a cell that ran has `metadata.rio.kernel` (id of the kernel that ran it) and "
        "`metadata.rio.defines` (names it bound).\n"
        "- Stale cell: stamp is not the current kernel id. After resume, rewind, or new "
        "session the kernel is new and empty; stale cells' variables, open files, and "
        "subprocesses are gone. Each request lists stale cells. Leaving a cell stale is fine. "
        "Using a name only a stale cell defined raises `StaleVariableError`. To use it, in "
        "the same patch remove that cell's stamp before its readers "
        '(`{"op": "remove", "path": "/cells/<i>/metadata/rio/kernel"}`), or define the name '
        "again."
    )


def _archive_note(spec: HarnessSpec) -> str:
    if spec.archive is None:
        return ""
    return (
        f" A cell whose id leaves N is saved to `{spec.archive}/<id>.json` (source and "
        "outputs). When you may need it again, link that path in the summary and read the "
        "file from a code cell instead of keeping the cell."
    )


def build_messages(
    notebook: Notebook, *, limit_tokens: int, error_note: str | None = None
) -> list[AgentMessage]:
    """Return the request messages for `notebook`, plus a transient correction."""
    text = (
        f"```json\n{render_notebook(notebook)}\n```\n"
        f"[notebook: ~{notebook_tokens(notebook)}/{limit_tokens} tokens]"
    )
    stale = stale_cells(notebook)
    if stale:
        text += (
            f"\n[stale cells {stale}: they ran in an older kernel. Run one again before "
            "reading its variables.]"
        )
    if error_note:
        text += f"\n\nRejected reply: {error_note}\nRetry with a corrected reply."
    return [UserMessage(content=text)]
