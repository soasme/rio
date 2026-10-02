"""Tests for the notebook context: patching, validation, change detection, and execution."""

from __future__ import annotations

import pytest

from conftest import add_code, add_markdown
from rio.agent import NotebookError, PapermillExecutor, apply_patch, changed_cells, new_notebook
from rio.agent.notebook import merge_outputs


def test_added_cells_get_ids_metadata_and_outputs():
    notebook = apply_patch(new_notebook(), [add_code("x = 1")])

    cell = notebook["cells"][0]
    assert cell["id"] and cell["metadata"] == {} and cell["outputs"] == []
    assert cell["execution_count"] is None


def test_list_sources_are_joined():
    op = {"op": "add", "path": "/cells/-", "value": {"cell_type": "code", "source": ["a\n", "b"]}}

    assert apply_patch(new_notebook(), [op])["cells"][0]["source"] == "a\nb"


def test_patch_does_not_change_the_input():
    notebook = new_notebook()

    apply_patch(notebook, [add_code("x")])

    assert notebook["cells"] == []


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"op": "add"}, "JSON array"),
        ([{"op": "remove", "path": "/cells/0"}], "patch failed"),
        ([{"op": "remove", "path": "/cells"}], "no `cells`"),
        ([{"op": "add", "path": "/nbformat", "value": 3}], "invalid notebook"),
    ],
)
def test_bad_patches_raise(patch, message):
    with pytest.raises(NotebookError, match=message):
        apply_patch(new_notebook(), patch)


def test_duplicate_cell_ids_are_rejected():
    notebook = apply_patch(new_notebook(), [add_code("x")])
    duplicate = {"op": "copy", "from": "/cells/0", "path": "/cells/-"}

    with pytest.raises(NotebookError, match="unique"):
        apply_patch(notebook, [duplicate])


def test_changed_cells_are_new_or_edited_code_cells():
    before = apply_patch(new_notebook(), [add_code("a"), add_code("b"), add_markdown("m")])
    after = apply_patch(
        before,
        [
            {"op": "replace", "path": "/cells/1/source", "value": "b2"},
            {"op": "replace", "path": "/cells/0/outputs", "value": []},
            add_markdown("n"),
            add_code("c"),
        ],
    )

    assert changed_cells(before, after) == [1, 4]


def test_merge_keeps_old_outputs_of_replayed_cells_unless_they_failed():
    notebook = apply_patch(new_notebook(), [add_code("a"), add_code("b"), add_code("c")])
    notebook["cells"][0]["outputs"] = [{"output_type": "stream", "name": "stdout", "text": "old"}]
    stream = {"output_type": "stream", "name": "stdout", "text": "x" * 20}
    failed = {
        "output_type": "error",
        "ename": "E",
        "evalue": "",
        "traceback": ["\x1b[31mred\x1b[0m"],
    }
    executed = [
        {"metadata": {"papermill": {"status": "completed"}}, "outputs": [stream]},
        {"metadata": {"papermill": {"status": "failed"}}, "outputs": [failed]},
        {"metadata": {"papermill": {"status": "pending"}}, "outputs": []},
    ]

    merged = merge_outputs(notebook, executed, [2], output_chars=10)

    assert merged["cells"][0]["outputs"][0]["text"] == "old"
    assert merged["cells"][1]["outputs"][0]["traceback"] == ["red"]
    assert merged["cells"][2]["outputs"] == []


def test_merge_cuts_long_output_and_drops_binary_data():
    notebook = apply_patch(new_notebook(), [add_code("a")])
    result = {
        "output_type": "execute_result",
        "execution_count": 1,
        "metadata": {},
        "data": {"text/plain": "y" * 20, "image/png": "AAAA"},
    }

    merged = merge_outputs(notebook, [{"metadata": {}, "outputs": [result]}], [0], output_chars=10)

    data = merged["cells"][0]["outputs"][0]["data"]
    assert list(data) == ["text/plain"]
    assert data["text/plain"].startswith("y" * 10 + "\n[10 more characters cut")


@pytest.mark.asyncio
async def test_papermill_runs_changed_cells_with_variables_from_replayed_cells(tmp_path):
    first = apply_patch(new_notebook(), [add_code("x = 41\nprint('once')")])
    executor = PapermillExecutor(tmp_path)
    first = await executor(first, [0])
    second = apply_patch(first, [add_code("import os\nprint(x + 1, os.getcwd())"), add_code("1/0")])

    result = await executor(second, changed_cells(first, second))

    cells = result["cells"]
    assert cells[0]["outputs"][0]["text"] == "once\n"
    assert cells[1]["outputs"][0]["text"] == f"42 {tmp_path.resolve()}\n"
    assert cells[2]["outputs"][0]["ename"] == "ZeroDivisionError"
