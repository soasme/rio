"""Tests for the notebook context: patching, validation, change detection, and execution."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

from conftest import add_code, add_markdown
from rio.agent import KernelExecutor, NotebookError, apply_patch, changed_cells, new_notebook
from rio.agent.notebook import cell_names, stale_cells, stale_uses, with_kernel


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


def test_a_failed_patch_names_the_cell_paths_that_exist():
    notebook = apply_patch(new_notebook(), [add_code("x"), add_code("y")])
    ids = [cell["id"] for cell in notebook["cells"]]

    with pytest.raises(NotebookError) as error:
        apply_patch(notebook, [{"op": "remove", "path": "/cells/4"}])

    message = str(error.value)
    assert message.startswith("patch failed")
    assert f"/cells/0 = {ids[0]}, /cells/1 = {ids[1]}" in message
    assert "each remove shifts later indices down" in message


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


def _stamped(*kernels: str | None) -> dict:
    notebook = apply_patch(new_notebook(), [add_code(f"c{i}") for i in range(len(kernels))])
    for cell, kernel in zip(notebook["cells"], kernels, strict=True):
        if kernel:
            cell["metadata"] = {"rio": {"kernel": kernel}}
    return notebook


def test_removing_a_kernel_stamp_runs_the_cell_again():
    before = _stamped("k1", "k1")
    after = apply_patch(before, [{"op": "remove", "path": "/cells/1/metadata/rio/kernel"}])

    assert changed_cells(before, after) == [1]


def test_stale_cells_ran_in_another_kernel():
    notebook = with_kernel(_stamped("k1", "k2", None), "k2", running=False)

    assert notebook["metadata"]["rio"]["kernel"] == {"id": "k2", "running": False}
    assert stale_cells(notebook) == [0]


def test_cell_names_see_through_magics_and_shell_commands():
    source = "import os.path as p\nfrom x import y\ndef f(): return z\n!ls\nw = v + 1"

    defined, used = cell_names(source)

    assert {"p", "y", "f", "w"} <= defined
    assert {"z", "v"} <= used
    assert cell_names("%%edit a.py\n<<<<<<< SEARCH\nq\n") == (set(), {"get_ipython"})
    assert cell_names("def (") == (set(), set())


def _with_stale_source(*sources: str) -> dict:
    notebook = apply_patch(new_notebook(), [add_code(source) for source in sources])
    for cell in notebook["cells"]:
        cell["metadata"] = {
            "rio": {"kernel": "old", "defines": sorted(cell_names(cell["source"])[0])}
        }
    return with_kernel(notebook, "new", running=False)


def test_reading_a_variable_only_a_stale_cell_defines_is_reported():
    before = _with_stale_source("data = load()", "print('hi')")
    after = apply_patch(before, [add_code("total = sum(data)")])

    assert stale_uses(before, after) == [
        "cell 2 reads `data`, which only stale cell 0 defined; that cell ran in an older kernel"
    ]


def test_stale_cells_may_be_left_alone():
    before = _with_stale_source("data = load()")
    after = apply_patch(before, [add_code("total = 1")])

    assert stale_uses(before, after) == []


def test_running_the_stale_cell_first_or_redefining_the_name_is_accepted():
    before = _with_stale_source("data = load()")
    rerun = {"op": "remove", "path": "/cells/0/metadata/rio/kernel"}

    assert stale_uses(before, apply_patch(before, [rerun, add_code("sum(data)")])) == []
    redefine = add_code("data = [1]\nprint(sum(data))")
    assert stale_uses(before, apply_patch(before, [redefine])) == []


def test_a_live_cell_that_defines_the_name_satisfies_the_read():
    before = _with_stale_source("data = load()", "data = [2]")
    before["cells"][1]["metadata"] = {"rio": {"kernel": "new", "defines": ["data"]}}
    after = apply_patch(before, [add_code("sum(data)")])

    assert stale_uses(before, after) == []


@pytest.fixture
async def kernel(tmp_path):
    executor = KernelExecutor(tmp_path)
    yield executor
    await executor.aclose()


def _text(cell) -> str:
    return "".join(output.get("text", "") for output in cell["outputs"])


@pytest.mark.asyncio
async def test_sessions_have_separate_python_environments(tmp_path):
    wheel = tmp_path / "rio_session_probe-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("rio_session_probe.py", "VALUE = 42\n")
        archive.writestr(
            "rio_session_probe-1.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: rio-session-probe\nVersion: 1.0\n",
        )
        archive.writestr(
            "rio_session_probe-1.0.dist-info/WHEEL", "Wheel-Version: 1.0\nTag: py3-none-any\n"
        )
        archive.writestr("rio_session_probe-1.0.dist-info/RECORD", "")
    first, second = KernelExecutor(tmp_path), KernelExecutor(tmp_path)
    try:
        source = (
            "import sys, os\n"
            "print(sys.executable, os.environ['VIRTUAL_ENV'])\n"
            f"!pip install --no-index {wheel}\n"
            "import rio_session_probe\nprint(rio_session_probe.VALUE)"
        )
        installed = await first(apply_patch(new_notebook(), [add_code(source)]), [0])
        output = _text(installed["cells"][0])
        assert "42\n" in output
        executable, environment = output.splitlines()[:2][0].split()
        assert executable.startswith(environment)
        assert executable != sys.executable
        await first.shutdown()
        reload_package = "import rio_session_probe\nprint(rio_session_probe.VALUE)"
        restarted = await first(
            apply_patch(new_notebook(), [add_code(reload_package)]),
            [0],
        )
        assert _text(restarted["cells"][0]) == "42\n"
        check = "import importlib.util\nprint(importlib.util.find_spec('rio_session_probe'))"
        other = await second(apply_patch(new_notebook(), [add_code(check)]), [0])
        assert _text(other["cells"][0]) == "None\n"
    finally:
        await first.aclose()
        await second.aclose()
    assert not Path(environment).exists()


@pytest.mark.asyncio
async def test_variables_build_up_and_earlier_cells_never_run_again(kernel, tmp_path):
    first = apply_patch(new_notebook(), [add_code("x = 41\nopen('log', 'a').write('ran\\n')")])
    first = await kernel(first, [0])
    second = apply_patch(first, [add_code("import os\nprint(x + 1, os.getcwd())")])

    result = await kernel(second, changed_cells(first, second))

    assert _text(result["cells"][1]) == f"42 {tmp_path.resolve()}\n"
    assert (tmp_path / "log").read_text() == "ran\n"


@pytest.mark.asyncio
async def test_a_failing_cell_stops_the_changed_cells_after_it(kernel):
    notebook = apply_patch(new_notebook(), [add_code("1/0"), add_code("print('skipped')")])

    result = await kernel(notebook, [0, 1])

    assert result["cells"][0]["outputs"][0]["ename"] == "ZeroDivisionError"
    assert result["cells"][0]["metadata"] == {"rio": {"kernel": kernel.kernel_id, "defines": []}}
    assert result["cells"][1]["metadata"] == {}
    assert "\x1b[" not in "".join(result["cells"][0]["outputs"][0]["traceback"])
    assert result["cells"][1]["outputs"] == []


@pytest.mark.asyncio
async def test_shutdown_starts_the_next_run_with_an_empty_kernel(kernel):
    notebook = apply_patch(new_notebook(), [add_code("y = 1")])
    notebook = await kernel(notebook, [0])
    old_id = kernel.kernel_id
    await kernel.shutdown()
    check = apply_patch(notebook, [add_code("print(type(y).__name__)"), add_code("y + 1")])

    result = await kernel(check, [1, 2])

    # The new kernel has no `y`: a placeholder stands in, and using it names cell 0.
    assert _text(result["cells"][1]) == "StaleValue\n"
    error = result["cells"][2]["outputs"][0]
    assert error["ename"] == "StaleVariableError"
    assert error["traceback"] == [f"StaleVariableError: {error['evalue']}"]
    assert "defined by cell 0" in error["evalue"]
    assert kernel.kernel_id != old_id
    assert result["metadata"]["rio"]["kernel"] == {"id": kernel.kernel_id, "running": True}
    assert stale_cells(result) == [0]


@pytest.mark.asyncio
async def test_long_output_is_cut_and_binary_data_dropped(tmp_path):
    kernel = KernelExecutor(tmp_path, output_chars=10)
    source = (
        "from IPython.display import display\n"
        "print('y' * 20)\n"
        "display({'image/png': 'AAAA', 'text/plain': 'img'}, raw=True)"
    )
    notebook = apply_patch(new_notebook(), [add_code(source)])
    try:
        result = await kernel(notebook, [0])
    finally:
        await kernel.shutdown()

    stream, image = result["cells"][0]["outputs"]
    assert stream["text"].startswith("y" * 10 + "\n[11 more characters cut")
    assert image["data"] == {"text/plain": "img"}


@pytest.mark.asyncio
async def test_startup_code_runs_once_when_the_kernel_starts(tmp_path):
    kernel = KernelExecutor(tmp_path, startup="started = True")
    notebook = apply_patch(new_notebook(), [add_code("print(started)")])
    try:
        result = await kernel(notebook, [0])
    finally:
        await kernel.shutdown()

    assert _text(result["cells"][0]) == "True\n"


@pytest.mark.asyncio
async def test_defined_names_are_recorded_at_run_time(kernel):
    source = "import os\nexec('made = 1')\ndef f():\n    local = 2\n    return local\nx = f()"
    notebook = apply_patch(new_notebook(), [add_code(source), add_code("x = 3\nos.sep")])

    result = await kernel(notebook, [0, 1])

    assert result["cells"][0]["metadata"]["rio"]["defines"] == ["f", "made", "os", "x"]
    assert result["cells"][1]["metadata"]["rio"]["defines"] == ["x"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source",
    [
        "globals()['data'].append(1)",
        "eval('data')[0]",
        "data = data + [1]",
        "with data: pass",
        "for item in data: pass",
    ],
)
async def test_placeholders_catch_dynamic_and_indirect_reads(kernel, source):
    first = apply_patch(new_notebook(), [add_code("data = [0]")])
    first = await kernel(first, [0])
    await kernel.shutdown()
    second = apply_patch(first, [add_code(source)])

    result = await kernel(second, [1])

    output = result["cells"][1]["outputs"][0]
    assert output.get("ename") == "StaleVariableError" or "StaleVariableError" in output.get(
        "text", ""
    )


@pytest.mark.asyncio
async def test_rerunning_the_stale_cell_replaces_the_placeholder(kernel):
    first = apply_patch(new_notebook(), [add_code("data = [0]")])
    first = await kernel(first, [0])
    await kernel.shutdown()
    second = apply_patch(
        first,
        [{"op": "remove", "path": "/cells/0/metadata/rio/kernel"}, add_code("print(data)")],
    )

    result = await kernel(second, changed_cells(first, second))

    assert _text(result["cells"][1]) == "[0]\n"
    assert stale_cells(result) == []
