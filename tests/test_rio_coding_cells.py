import pytest

from rio.agent import InvalidPatch
from rio.agent.prompt import EXECUTION, system
from rio.coding.cells import CMD, CODE, KINDS

SCRIPT = "# /// script\n# dependencies = []\n# ///\nprint('hi')\n"


def test_coding_prompt_describes_both_kinds_and_execution():
    prompt = system(KINDS)
    assert CODE.prompt in prompt and CMD.prompt in prompt and EXECUTION in prompt


@pytest.mark.parametrize(
    ("kind", "cell", "message"),
    [
        (CODE, {"runtime": "shell", "source": SCRIPT}, "runtime: python"),
        (CODE, {"runtime": "python", "source": "print('hi')"}, "PEP 723"),
        (
            CODE,
            {"runtime": "python", "source": "# /// script\n# dependencies = [1]\n# ///\n"},
            "array of strings",
        ),
        (CMD, {"argv": []}, "nonempty argv"),
        (CMD, {"argv": "pytest -q"}, "nonempty argv"),
    ],
)
def test_malformed_cells_are_rejected(kind, cell, message):
    with pytest.raises(InvalidPatch, match=message):
        kind.validate(cell)


def test_code_runs_its_script_through_uv(tmp_path):
    argv = CODE.command({"id": 7, "source": SCRIPT}, tmp_path)
    assert argv == ["uv", "run", "--script", str(tmp_path / "cell-7.py")]
    assert (tmp_path / "cell-7.py").read_text() == SCRIPT


def test_cmd_runs_its_argv(tmp_path):
    assert CMD.command({"argv": ["pytest", "-q"]}, tmp_path) == ["pytest", "-q"]
