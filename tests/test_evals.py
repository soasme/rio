"""Offline checks for the eval fixtures and runner."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.run import CASES, run_case


def test_starter_fails_and_reference_solution_passes(tmp_path):
    for name, solution in {
        "divide": "def divide(numerator, denominator):\n    return numerator / denominator\n",
        "slugify": (
            "import re\n"
            "def slugify(text: str) -> str:\n"
            "    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')\n"
        ),
    }.items():
        case = CASES / name
        workspace = tmp_path / name
        shutil.copytree(case / "workspace", workspace)
        target = next(workspace.glob("*.py"))
        grader = [sys.executable, "-m", "pytest", "-q", str(case / "grade.py")]
        assert subprocess.run(grader, cwd=workspace, capture_output=True, check=False).returncode
        target.write_text(solution, encoding="utf-8")
        graded = subprocess.run(grader, cwd=workspace, capture_output=True, check=False)
        assert graded.returncode == 0


@pytest.mark.parametrize("agent_exit", [0, 1])
def test_run_case_keeps_grader_outside_agent_workspace(tmp_path, monkeypatch, agent_exit):
    original_run = subprocess.run

    def fake_agent(command, **kwargs):
        if "-c" not in command:
            return original_run(command, **kwargs)
        workspace = command[command.index("--cwd") + 1]
        assert not (Path(workspace) / "grade.py").exists()
        (Path(workspace) / "calculator.py").write_text(
            "def divide(numerator, denominator):\n    return numerator / denominator\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            command, agent_exit, stdout='{"type":"done"}\n', stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_agent)
    result = run_case(
        CASES / "divide", provider="fake", model="fake", timeout=10, output=tmp_path / "output"
    )
    assert result["passed"] is (agent_exit == 0)
    assert (tmp_path / "output" / "agent.jsonl").read_text() == '{"type":"done"}\n'
