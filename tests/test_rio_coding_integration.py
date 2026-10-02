"""End-to-end: a real coding session doing real work on a real directory.

Everything here is genuine except the model. The tools are the ported coding
tools, the skill instructions are the ones a real run would get, the journal is
a real JSONL file on disk, and the edits actually land. Only the model's step
output is scripted, so the run is deterministic.

The point is to check that the pieces compose: that a ported tool works as an
action, that its output lands in the context, and that the agent can manage
that context by editing its context file with the same tools.
"""

from __future__ import annotations

import json

from conftest import step_response
from rio.agent import parse_context, render_context, turn
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
            exposed_tools=("read", "write", "edit", "bash", "respond"),
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
            action="read",
            args={"path": "calc.py"},
        ),
        step_response(
            reasoning="add() subtracts. Swap the operator.",
            action="edit",
            args={
                "path": "calc.py",
                "edits": [{"oldText": "return a - b", "newText": "return a + b"}],
            },
        ),
        step_response(
            reasoning="Confirm the fix by running it.",
            action="bash",
            args={"command": 'python -c "import calc; print(calc.add(2, 3))"'},
        ),
        step_response(
            reasoning="It prints 5. Done.",
            action="respond",
            args={"message": "Fixed calc.add: it subtracted instead of adding. Verified 2+3=5."},
        ),
    ]


async def test_the_agent_actually_fixes_the_file(tmp_path) -> None:
    session, repo = await build_session(tmp_path, fix_the_bug_streams())
    async for _event in session.prompt("calc.add returns the wrong answer, please fix it"):
        pass

    assert "return a + b" in (repo / "calc.py").read_text(encoding="utf-8")
    assert session.answer == "Fixed calc.add: it subtracted instead of adding. Verified 2+3=5."


async def test_the_context_carries_the_whole_run(tmp_path) -> None:
    session, _repo = await build_session(tmp_path, fix_the_bug_streams())
    async for _event in session.prompt("fix calc.add"):
        pass

    context = session.context
    assert context[0] == turn("user", "fix calc.add")
    assert [item["role"] for item in context[1:]] == ["assistant", "tool"] * 4
    assert "add() subtracts. Swap the operator." in context[3]["text"]
    assert parse_context(session.context_file.read_text()) == context[:-2]


async def test_each_tool_result_becomes_the_next_observation(tmp_path) -> None:
    """A ported tool is an action; its output lands in the next request."""
    session, _repo = await build_session(tmp_path, fix_the_bug_streams())
    provider = session.provider
    async for _event in session.prompt("fix calc.add"):
        pass

    observations = [
        "\n".join(message.text for message in messages)
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

    final_observation = "\n".join(message.text for message in provider.calls[-1][2])
    assert "exit 0" in final_observation


async def test_the_journal_survives_the_process(tmp_path) -> None:
    """Reopen from the JSONL file alone and find the whole run's memory intact."""
    session, repo = await build_session(tmp_path, fix_the_bug_streams())
    async for _event in session.prompt("fix calc.add"):
        pass
    original = session.context

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
    assert reopened.context == original


async def test_the_journal_records_the_actions_that_changed_the_file(tmp_path) -> None:
    session, _repo = await build_session(tmp_path, fix_the_bug_streams())
    async for _event in session.prompt("fix calc.add"):
        pass

    steps = [e for e in await session.session_entries() if isinstance(e, StepEntry)]
    assert [s.action.name for s in steps] == ["read", "edit", "bash", "respond"]
    edit = steps[1]
    assert edit.action.arguments["path"] == "calc.py"
    assert json.dumps(edit.state)  # serializable, as the journal requires


async def test_the_run_renders(tmp_path) -> None:
    """The renderers consume a real run without special-casing."""
    session, _repo = await build_session(tmp_path, fix_the_bug_streams())
    renderer = PlainEventRenderer()
    async for event in session.prompt("fix calc.add"):
        renderer.render(event)

    rendered = render_completed_run(await session.session_entries())
    assert "edit" in rendered
    assert "return a + b" in rendered or "calc.py" in rendered


async def test_the_agent_compacts_its_context_with_its_own_tools(tmp_path) -> None:
    """The model rewrites its context file with `write`; the next request uses the rewrite."""
    session, _repo = await build_session(tmp_path, [])
    compacted = render_context([turn("notes", "task: fix calc.add; bug is '-' in add()")])
    session.provider._streams.extend(
        [
            step_response(action="read", args={"path": "calc.py"}),
            step_response(
                reasoning="Compact before editing.",
                action="write",
                args={"path": str(session.context_file), "content": compacted},
            ),
            step_response(action="respond", args={"message": "done"}),
        ]
    )
    async for _event in session.prompt("fix calc.add"):
        pass

    third_request = "\n".join(message.text for message in session.provider.calls[2][2])
    assert "bug is '-' in add()" in third_request
    assert "A tiny module with a bug." not in third_request
    assert session.context[0] == turn("notes", "task: fix calc.add; bug is '-' in add()")
    assert "[context edit applied" in session.context[2]["text"]
