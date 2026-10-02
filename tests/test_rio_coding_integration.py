"""End-to-end: a real coding session doing real work on a real directory.

Everything here is genuine except the model. The skill instructions are the
ones a real run would get, cells run in a real kernel through papermill, the
journal is a real JSONL file on disk, and the edits actually land. Only the
model's patches are scripted, so the run is deterministic.
"""

from __future__ import annotations

from conftest import add_code, step_response
from rio.ai import FakeProvider
from rio.coding.paths import RioPaths
from rio.coding.rendering import PlainEventRenderer, render_completed_run
from rio.coding.resources import RioResourcePaths
from rio.coding.session import CodingSession, CodingSessionConfig
from rio.coding.session_store import JsonlSessionStorage, StepEntry

BUGGY_SOURCE = '''"""A tiny module with a bug."""


def add(a, b):
    return a - b
'''


def _config(tmp_path, repo, provider) -> CodingSessionConfig:
    home = tmp_path / "home"
    paths = RioPaths(home=home / ".rio", agents_home=home / ".agents")
    return CodingSessionConfig(
        provider=provider,
        model="test-model",
        cwd=repo,
        paths=paths,
        resource_paths=RioResourcePaths(
            root=home / ".rio", cwd=repo, agents_root=home / ".agents", paths=paths
        ),
        storage=JsonlSessionStorage(paths.default_session_path(repo)),
    )


def fix_the_bug_streams():
    """Read the file, fix it with Python, verify with a shell command, then answer."""
    return [
        step_response(
            reasoning="I need to see calc.py before changing anything.",
            patch=[add_code("source = open('calc.py').read()\nprint(source)")],
        ),
        step_response(
            reasoning="add() subtracts. Swap the operator, reusing `source`.",
            patch=[
                {"op": "replace", "path": "/cells/1/outputs", "value": []},
                add_code(
                    "open('calc.py', 'w').write(source.replace('a - b', 'a + b'))\n"
                    "!python -c 'import calc; print(calc.add(2, 3))'"
                ),
            ],
        ),
        step_response(
            reasoning="It prints 5. Done.",
            reply="Fixed calc.add: it subtracted instead of adding. Verified 2+3=5.",
        ),
    ]


async def test_the_agent_fixes_the_file_and_the_journal_rebuilds_the_notebook(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "calc.py").write_text(BUGGY_SOURCE, encoding="utf-8")
    provider = FakeProvider(fix_the_bug_streams())
    session = await CodingSession.load(_config(tmp_path, repo, provider))
    renderer = PlainEventRenderer()

    async for event in session.prompt("calc.add returns the wrong answer, please fix it"):
        renderer.render(event)

    assert "return a + b" in (repo / "calc.py").read_text(encoding="utf-8")
    assert session.answer == "Fixed calc.add: it subtracted instead of adding. Verified 2+3=5."

    # Each request carries the outputs of the cells the previous patch ran.
    assert "return a - b" in provider.calls[1][2][0].text
    cells = session.notebook["cells"]
    assert cells[1]["outputs"] == []
    assert cells[2]["outputs"][0]["text"].strip() == "5"

    entries = await session.session_entries()
    steps = [entry for entry in entries if isinstance(entry, StepEntry)]
    assert [step.cells for step in steps] == [[1], [2], []]
    assert "calc.py" in render_completed_run(entries)

    # The notebook lives only in the journal: a fresh session rebuilds it.
    reopened = await CodingSession.load(_config(tmp_path, repo, FakeProvider([])))
    assert reopened.notebook == session.notebook
    assert reopened.provider.calls == []
