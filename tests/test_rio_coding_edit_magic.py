"""Tests for the `%%edit` cell magic."""

from __future__ import annotations

import pytest

from conftest import add_code
from rio.coding.agent import KernelExecutor, apply_patch, new_notebook
from rio.coding.edit_magic import parse_edit_blocks, run_edit
from rio.coding.session import KERNEL_STARTUP
from rio.coding.tools import ToolInputError

BLOCK = "<<<<<<< SEARCH\n{old}\n=======\n{new}\n>>>>>>> REPLACE\n"


def test_blocks_parse_into_edits():
    body = BLOCK.format(old="a", new="b") + "\n" + BLOCK.format(old="c\nd", new="")

    assert parse_edit_blocks(body) == [
        {"oldText": "a", "newText": "b"},
        {"oldText": "c\nd", "newText": ""},
    ]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("", "no `<<<<<<< SEARCH`"),
        ("stray text\n", "expected"),
        ("<<<<<<< SEARCH\na\n", "missing `=======`"),
        ("<<<<<<< SEARCH\na\n=======\nb\n", "missing `>>>>>>> REPLACE`"),
    ],
)
def test_malformed_blocks_are_rejected(body, message):
    with pytest.raises(ToolInputError, match=message):
        parse_edit_blocks(body)


def test_run_edit_writes_the_file_and_returns_a_diff(tmp_path):
    path = tmp_path / "calc.py"
    path.write_text("def add(a, b):\n    return a - b\n")

    diff = run_edit(str(path), BLOCK.format(old="    return a - b", new="    return a + b"))

    assert path.read_text() == "def add(a, b):\n    return a + b\n"
    assert "-    return a - b\n+    return a + b" in diff


def test_run_edit_needs_a_unique_match_and_writes_nothing_otherwise(tmp_path):
    path = tmp_path / "x.py"
    path.write_text("a\na\n")

    with pytest.raises(ToolInputError, match="2 occurrences"):
        run_edit(str(path), BLOCK.format(old="a", new="b"))
    assert path.read_text() == "a\na\n"


@pytest.mark.asyncio
async def test_the_magic_runs_in_a_session_kernel(tmp_path):
    (tmp_path / "calc.py").write_text(">>> x = 1\n")
    kernel = KernelExecutor(tmp_path, startup=KERNEL_STARTUP)
    good = "%%edit calc.py\n" + BLOCK.format(old=">>> x = 1", new=">>> x = 2")
    bad = "%%edit calc.py\n" + BLOCK.format(old="missing", new="y")
    notebook = apply_patch(new_notebook(), [add_code(good), add_code(bad)])
    try:
        ran = await kernel(notebook, [0])
        failed = await kernel(notebook, [1])
    finally:
        await kernel.shutdown()

    assert (tmp_path / "calc.py").read_text() == ">>> x = 2\n"
    assert "+>>> x = 2" in ran["cells"][0]["outputs"][0]["text"]
    error = failed["cells"][1]["outputs"][0]
    assert error["ename"] == "EditError"
    assert "Could not find the exact text" in error["evalue"]
    assert error["traceback"] == [f"EditError: {error['evalue']}"]
