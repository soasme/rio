"""One-shot CLI checks."""

import pytest

from rio.cli import _resolve_task, app


def test_cli_requires_a_task():
    with pytest.raises(SystemExit) as error:
        app(["run"])
    assert error.value.code == 2


def test_long_task_is_not_treated_as_a_file():
    task = "Fix nthroot_mod in README.md " + "x" * 300 + " README.md"
    assert _resolve_task([task]) == task


def test_markdown_task_file_is_expanded(tmp_path):
    task_file = tmp_path / "task.md"
    task_file.write_text("fix the parser", encoding="utf-8")
    assert _resolve_task([str(task_file)]) == "fix the parser"
