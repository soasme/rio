"""The `%%edit` cell magic: exact text replacement in one file, from a notebook cell.

Load it in a kernel with `%load_ext rio.coding.edit_magic`. A cell names the
file and holds one or more search/replace blocks::

    %%edit calc.py
    <<<<<<< SEARCH
        return a - b
    =======
        return a + b
    >>>>>>> REPLACE

The rules are the `edit` tool's: every search text must match exactly once in
the original file, blocks must not overlap, and nothing is written unless
every block applies. The cell prints a unified diff of the change.
"""

from __future__ import annotations

from pathlib import Path

from rio.coding.tools import ToolInputError, edit_file, generate_unified_patch

SEARCH = "<<<<<<< SEARCH"
DIVIDER = "======="
REPLACE = ">>>>>>> REPLACE"


def parse_edit_blocks(body: str) -> list[dict[str, str]]:
    """Return the cell's search/replace blocks as `oldText`/`newText` edits."""
    edits: list[dict[str, str]] = []
    lines = body.split("\n")
    index = 0
    while index < len(lines):
        if lines[index].rstrip() != SEARCH:
            if lines[index].strip():
                raise ToolInputError(f"expected `{SEARCH}`, got {lines[index]!r}")
            index += 1
            continue
        old, index = _until(lines, index + 1, DIVIDER)
        new, index = _until(lines, index, REPLACE)
        edits.append({"oldText": old, "newText": new})
    if not edits:
        raise ToolInputError(f"no `{SEARCH}` ... `{REPLACE}` blocks")
    return edits


def run_edit(path: str, body: str) -> str:
    """Apply the cell's blocks to `path` and return the unified diff."""
    target = Path(path).expanduser()
    old, new = edit_file(target, parse_edit_blocks(body))
    return generate_unified_patch(str(target), old, new)


def _until(lines: list[str], start: int, marker: str) -> tuple[str, int]:
    for index in range(start, len(lines)):
        if lines[index].rstrip() == marker:
            return "\n".join(lines[start:index]), index + 1
    raise ToolInputError(f"missing `{marker}`")


class EditError(Exception):
    """A `%%edit` cell that could not be applied. Nothing was written."""

    def _render_traceback_(self) -> list[str]:
        # IPython shows this instead of a traceback that repeats the whole cell.
        return [f"EditError: {self}"]


def load_ipython_extension(ipython) -> None:  # noqa: ANN001 - IPython's own hook signature
    from IPython.core.inputtransformer2 import classic_prompt, ipython_prompt

    # IPython strips `>>> ` and `... ` prompts from pasted cells, which would
    # rewrite search and replace text (doctests, the `>>>>>>> REPLACE` marker).
    manager = ipython.input_transformer_manager
    manager.cleanup_transforms = [
        transform
        for transform in manager.cleanup_transforms
        if transform not in (classic_prompt, ipython_prompt)
    ]

    def edit(line: str, cell: str) -> None:
        path = line.strip()
        if not path:
            raise EditError("usage: %%edit PATH, then SEARCH/REPLACE blocks")
        try:
            print(run_edit(path, cell), end="")
        except (ToolInputError, OSError) as exc:
            raise EditError(str(exc)) from None

    ipython.register_magic_function(edit, magic_kind="cell", magic_name="edit")
