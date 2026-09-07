"""End-to-end: a real coding session doing real work on a real directory.

Everything here is genuine except the model. The tools are the ported coding
tools, the skill instructions are the ones a real run would get, the journal is
a real JSONL file on disk, and the edits actually land. Only the model's step
output is scripted, so the run is deterministic.

The point is to check that the pieces compose: that a ported tool works as a
SKILL.state *action*, that its output becomes the next observation, and that
what the agent learned survives in the execution state rather than in a
conversation nobody kept.
"""

from __future__ import annotations

import json

from conftest import step_response
from rio_ai import FakeProvider
from rio_coding.paths import RioPaths
from rio_coding.rendering import PlainEventRenderer, render_completed_run
from rio_coding.resources import RioResourcePaths
from rio_coding.session import CodingSession, CodingSessionConfig
from rio_coding.session_store import JsonlSessionStorage, StepEntry

BUGGY_SOURCE = '''"""A tiny module with a bug."""


def add(a, b):
    return a - b
'''


async def build_session(tmp_path, streams):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "calc.py").write_text(BUGGY_SOURCE, encoding="utf-8")
    (repo / "AGENTS.md").write_text("Prefer minimal diffs.\n", encoding="utf-8")

    home = tmp_path / "home"
    paths = RioPaths(home=home / ".rio", agents_home=home / ".agents")
    session = await CodingSession.load(
        CodingSessionConfig(
            provider=FakeProvider(streams),
            model="test-model",
            cwd=repo,
            paths=paths,
            resource_paths=RioResourcePaths(
                root=home / ".rio", cwd=repo, agents_root=home / ".agents", paths=paths
            ),
            storage=JsonlSessionStorage(paths.default_session_path(repo)),
        )
    )
    return session, repo


def fix_the_bug_streams():
    """Read the file, fix it, verify with bash, then answer."""
    return [
        step_response(
            reasoning="I need to see calc.py before changing anything.",
            state_delta={
                "goal": "fix the bug in calc.add",
                "plan": [
                    {"id": "1", "title": "read calc.py", "status": "in_progress"},
                    {"id": "2", "title": "fix the operator", "status": "pending"},
                    {"id": "3", "title": "verify", "status": "pending"},
                ],
            },
            action="read",
            args={"path": "calc.py"},
        ),
        step_response(
            reasoning="add() subtracts. Swap the operator.",
            state_delta={
                "findings": {"bug": "calc.add used '-' instead of '+'"},
                "files": {"calc.py": {"status": "edited", "note": "operator fixed"}},
                "plan": [
                    {"id": "1", "title": "read calc.py", "status": "done"},
                    {"id": "2", "title": "fix the operator", "status": "in_progress"},
                    {"id": "3", "title": "verify", "status": "pending"},
                ],
            },
            action="edit",
            args={
                "path": "calc.py",
                "edits": [{"oldText": "return a - b", "newText": "return a + b"}],
            },
        ),
        step_response(
            reasoning="Confirm the fix by running it.",
            state_delta={
                "plan": [
                    {"id": "1", "title": "read calc.py", "status": "done"},
                    {"id": "2", "title": "fix the operator", "status": "done"},
                    {"id": "3", "title": "verify", "status": "in_progress"},
                ],
            },
            action="bash",
            args={"command": 'python -c "import calc; print(calc.add(2, 3))"'},
        ),
        step_response(
            reasoning="It prints 5. Done.",
            state_delta={
                "plan": [
                    {"id": "1", "title": "read calc.py", "status": "done"},
                    {"id": "2", "title": "fix the operator", "status": "done"},
                    {"id": "3", "title": "verify", "status": "done"},
                ],
            },
            action="respond",
            args={"message": "Fixed calc.add: it subtracted instead of adding. Verified 2+3=5."},
        ),
    ]


async def test_the_agent_actually_fixes_the_file(tmp_path) -> None:
    session, repo = await build_session(tmp_path, fix_the_bug_streams())
    async for _event in session.prompt("calc.add returns the wrong answer, please fix it"):
        pass

    assert "return a + b" in (repo / "calc.py").read_text(encoding="utf-8")
    # The answer is the terminating action's message, not a copy kept in state.
    assert session.answer == "Fixed calc.add: it subtracted instead of adding. Verified 2+3=5."


async def test_the_plan_progresses_through_the_state(tmp_path) -> None:
    session, _repo = await build_session(tmp_path, fix_the_bug_streams())
    async for _event in session.prompt("fix calc.add"):
        pass

    assert session.plan_progress == (3, 3)
    assert session.touched_files == ["calc.py"]
    assert session.state["findings"]["bug"] == "calc.add used '-' instead of '+'"


async def test_each_tool_result_becomes_the_next_observation(tmp_path) -> None:
    """A ported tool is an action; its output is `O_t+1` and nothing else."""
    session, _repo = await build_session(tmp_path, fix_the_bug_streams())
    provider = session.provider
    async for _event in session.prompt("fix calc.add"):
        pass

    observations = [
        messages[0].content.partition("Latest Observation:")[2]
        for _m, _s, messages, _t in provider.calls
    ]
    # Step 1 sees the read output; step 2 sees the edit's; step 3 sees bash's.
    assert "return a - b" in observations[1]
    assert "calc.py" in observations[2]
    assert "5" in observations[3]


async def test_the_bash_action_really_runs(tmp_path) -> None:
    session, _repo = await build_session(tmp_path, fix_the_bug_streams())
    provider = session.provider
    async for _event in session.prompt("fix calc.add"):
        pass

    final_observation = provider.calls[-1][2][0].content
    assert "exit 0" in final_observation


async def test_the_journal_survives_the_process(tmp_path) -> None:
    """Reopen from the JSONL file alone and find the whole run's memory intact."""
    session, repo = await build_session(tmp_path, fix_the_bug_streams())
    async for _event in session.prompt("fix calc.add"):
        pass
    original = session.state

    home = tmp_path / "home"
    paths = RioPaths(home=home / ".rio", agents_home=home / ".agents")
    journal = paths.default_session_path(repo)
    assert journal.exists()

    reopened = await CodingSession.load(
        CodingSessionConfig(
            provider=FakeProvider([]),
            model="test-model",
            cwd=repo,
            paths=paths,
            resource_paths=RioResourcePaths(
                root=home / ".rio", cwd=repo, agents_root=home / ".agents", paths=paths
            ),
            storage=JsonlSessionStorage(journal),
        )
    )
    assert reopened.state == original


async def test_the_journal_records_the_actions_that_changed_the_file(tmp_path) -> None:
    session, _repo = await build_session(tmp_path, fix_the_bug_streams())
    async for _event in session.prompt("fix calc.add"):
        pass

    steps = [e for e in await session.session_entries() if isinstance(e, StepEntry)]
    assert [s.action.name for s in steps] == ["read", "edit", "bash", "respond"]
    edit = steps[1]
    assert edit.action.arguments["path"] == "calc.py"
    assert json.dumps(edit.state_delta)  # serializable, as the journal requires


async def test_the_run_renders(tmp_path) -> None:
    """The renderers consume a real run without special-casing."""
    session, _repo = await build_session(tmp_path, fix_the_bug_streams())
    renderer = PlainEventRenderer()
    async for event in session.prompt("fix calc.add"):
        renderer.render(event)

    rendered = render_completed_run(await session.session_entries())
    assert "edit" in rendered
    assert "return a + b" in rendered or "calc.py" in rendered


async def test_a_rejected_delta_does_not_touch_the_file(tmp_path) -> None:
    """Validation happens before the action runs, so a bad patch changes nothing."""
    streams = [
        step_response(
            reasoning="",
            state_delta={"not_a_declared_field": True},
            action="write",
            args={"path": "calc.py", "content": "wrecked"},
        ),
        *fix_the_bug_streams(),
    ]
    session, repo = await build_session(tmp_path, streams)
    async for _event in session.prompt("fix calc.add"):
        pass

    assert "wrecked" not in (repo / "calc.py").read_text(encoding="utf-8")
    assert "not_a_declared_field" not in session.state
    assert "return a + b" in (repo / "calc.py").read_text(encoding="utf-8")
