"""Tests for `rio.coding.rpc`, the JSONL RPC frontend over a coding session.

rio's session has no transcript and nothing to compact (see
`rio.coding.session` and `rio.coding.step_footprint`), so unlike tau's RPC
tests there is nothing here exercising `get_messages`, `get_fork_messages`,
or `compact` -- those commands do not exist. What is tested instead is the
protocol's own additions: `get_execution_state` reporting the session's
actual state, and `get_entries`/`get_tree`/`get_checkpoints`/`restore`
projecting the state journal rather than a message log.
"""

from __future__ import annotations

import json
from io import StringIO

import pytest

from conftest import step_response
from rio.ai import AgentTool, AgentToolResult, FakeProvider, TextContent
from rio.coding.rpc import RpcServer
from rio.coding.session import CodingSession, CodingSessionConfig
from rio.coding.session_store import InMemorySessionStorage, JsonlSessionStorage


async def _read(tool_call_id, arguments, signal=None, on_update=None):
    path = arguments.get("path", "")
    return AgentToolResult(content=[TextContent(text=f"read {path}\ncontents of {path}")])


async def _respond(tool_call_id, arguments, signal=None, on_update=None):
    return AgentToolResult(
        content=[TextContent(text=str(arguments.get("message", "")))], terminate=True
    )


READ = AgentTool(
    name="read",
    label="Read",
    description="Read a file.",
    parameters={"type": "object", "properties": {"path": {"type": "string"}}},
    execute_fn=_read,
)
RESPOND = AgentTool(
    name="respond",
    label="Respond",
    description="Answer the user and end the turn.",
    parameters={"type": "object", "properties": {"message": {"type": "string"}}},
    execute_fn=_respond,
)


def two_step_streams():
    return [
        step_response(
            reasoning="Check the entry point.",
            state_delta={
                "goal": "explain main.py",
                "files": {"main.py": {"status": "read", "note": "entry point"}},
                "findings": {"entrypoint": "main.py"},
            },
            action="read",
            args={"path": "main.py"},
        ),
        step_response(
            reasoning="Ready to answer.",
            state_delta={"plan": [{"id": "1", "title": "explain", "status": "done"}]},
            action="respond",
            args={"message": "main.py starts the server."},
        ),
    ]


@pytest.fixture
def project(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    (home / ".rio").mkdir(parents=True)
    return repo, home


async def make_session(project, streams, *, storage=None, **overrides):
    repo, home = project
    from rio.coding.paths import RioPaths
    from rio.coding.resources import RioResourcePaths

    paths = RioPaths(home=home / ".rio", agents_home=home / ".agents")
    config = CodingSessionConfig(
        provider=FakeProvider(streams),
        model="test-model",
        cwd=repo,
        paths=paths,
        resource_paths=RioResourcePaths(
            root=home / ".rio", cwd=repo, agents_root=home / ".agents", paths=paths
        ),
        storage=storage if storage is not None else InMemorySessionStorage(),
        tools=(READ, RESPOND),
        **overrides,
    )
    return await CodingSession.load(config)


async def run_rpc(session, script: str) -> list[dict]:
    stdin = StringIO(script)
    stdout = StringIO()
    await RpcServer(session, stdin=stdin, stdout=stdout).run()
    return [json.loads(line) for line in stdout.getvalue().splitlines() if line]


class TestPromptStreaming:
    async def test_prompt_streams_correlated_response_and_run_events(self, project) -> None:
        session = await make_session(project, two_step_streams())
        records = await run_rpc(
            session, '{"id":"one","type":"prompt","message":"explain main.py"}\n'
        )

        assert records[0] == {
            "type": "response",
            "command": "prompt",
            "success": True,
            "id": "one",
        }
        types = [record.get("type") for record in records[1:]]
        assert "run_start" in types
        assert "action_end" in types
        assert "run_end" in types
        assert types[-1] == "agent_settled"

    async def test_prompt_rejects_a_second_prompt_while_one_is_running(self) -> None:
        class _RunningSession:
            is_running = True

        stdout = StringIO()
        server = RpcServer(_RunningSession(), stdin=StringIO(""), stdout=stdout)
        import anyio

        async with anyio.create_task_group() as tasks:
            await server._dispatch({"id": "x", "type": "prompt", "message": "hi"}, tasks)

        record = json.loads(stdout.getvalue())
        assert record["success"] is False
        assert "already running" in record["error"]

    async def test_action_end_result_is_projected_as_camel_case_wire_data(self, project) -> None:
        session = await make_session(project, two_step_streams())
        records = await run_rpc(
            session, '{"id":"one","type":"prompt","message":"explain main.py"}\n'
        )

        action_ends = [record for record in records if record.get("type") == "action_end"]
        assert action_ends
        first_result = action_ends[0]["result"]
        assert "content" in first_result
        assert "isError" in action_ends[0]


class TestQueueing:
    async def test_steer_and_follow_up_are_acknowledged_without_starting_a_run(
        self, project
    ) -> None:
        session = await make_session(project, [])
        records = await run_rpc(
            session,
            '{"id":"a","type":"steer","message":"focus on tests"}\n'
            '{"id":"b","type":"follow_up","message":"then run lint"}\n'
            '{"id":"c","type":"get_state"}\n',
        )

        steer, follow_up, state = records
        assert steer == {"type": "response", "command": "steer", "success": True, "id": "a"}
        assert follow_up == {
            "type": "response",
            "command": "follow_up",
            "success": True,
            "id": "b",
        }
        assert state["data"]["queuedSteering"] == ["focus on tests"]
        assert state["data"]["queuedFollowUp"] == ["then run lint"]


class TestStateInspection:
    async def test_get_state_reports_the_expected_shape(self, project) -> None:
        session = await make_session(project, [])
        (record,) = await run_rpc(session, '{"id":"s","type":"get_state"}\n')

        assert set(record["data"]) == {
            "model",
            "provider",
            "thinkingLevel",
            "isRunning",
            "queuedSteering",
            "queuedFollowUp",
            "sessionName",
            "sessionFile",
            "contextUsage",
        }
        assert record["data"]["model"] == "test-model"
        assert record["data"]["isRunning"] is False
        assert set(record["data"]["contextUsage"]) == {"tokens", "contextWindow", "percent"}

    async def test_get_execution_state_reports_the_sessions_actual_memory(self, project) -> None:
        session = await make_session(project, two_step_streams())
        await run_rpc(session, '{"id":"one","type":"prompt","message":"explain main.py"}\n')

        (record,) = await run_rpc(session, '{"id":"s","type":"get_execution_state"}\n')

        data = record["data"]
        assert data["answer"] == "main.py starts the server."
        assert data["planProgress"] == {"completed": 1, "total": 1}
        assert data["touchedFiles"] == ["main.py"]
        assert data["state"]["findings"] == {"entrypoint": "main.py"}

    async def test_get_available_thinking_levels_lists_all_levels(self, project) -> None:
        session = await make_session(project, [])
        (record,) = await run_rpc(session, '{"id":"t","type":"get_available_thinking_levels"}\n')

        assert "off" in record["data"]["levels"]
        assert "medium" in record["data"]["levels"]

    async def test_set_and_cycle_thinking_level(self, project) -> None:
        session = await make_session(project, [])
        records = await run_rpc(
            session,
            '{"id":"a","type":"set_thinking_level","level":"low"}\n'
            '{"id":"b","type":"cycle_thinking_level"}\n',
        )

        set_record, cycle_record = records
        assert set_record["data"]["level"] == "low"
        assert cycle_record["data"]["level"] != "low"
        assert session.thinking_level == cycle_record["data"]["level"]

    async def test_set_model_updates_the_session(self, project) -> None:
        session = await make_session(project, [])
        (record,) = await run_rpc(
            session, '{"id":"m","type":"set_model","model":"another-model"}\n'
        )

        assert record["data"] == {"model": "another-model", "provider": session.provider_name}
        assert session.model == "another-model"

    async def test_set_session_name_persists_and_reports_the_change(self, project) -> None:
        session = await make_session(project, [])
        (record,) = await run_rpc(
            session, '{"id":"n","type":"set_session_name","name":"bugfix run"}\n'
        )

        assert record["data"]["name"] == "bugfix run"
        assert session.session_name == "bugfix run"

    async def test_new_session_clears_the_state(self, project) -> None:
        session = await make_session(project, two_step_streams())
        await run_rpc(session, '{"id":"one","type":"prompt","message":"explain main.py"}\n')

        (record,) = await run_rpc(session, '{"id":"n","type":"new_session"}\n')

        assert record["success"] is True
        assert session.answer is None

    async def test_abort_when_idle_is_a_harmless_success(self, project) -> None:
        session = await make_session(project, [])
        (record,) = await run_rpc(session, '{"id":"a","type":"abort"}\n')

        assert record == {"type": "response", "command": "abort", "success": True, "id": "a"}


class TestJournalInspection:
    async def test_get_entries_projects_the_state_journal_not_a_transcript(self, project) -> None:
        session = await make_session(project, two_step_streams())
        await run_rpc(session, '{"id":"one","type":"prompt","message":"explain main.py"}\n')

        (record,) = await run_rpc(session, '{"id":"e","type":"get_entries"}\n')

        entries = record["data"]["entries"]
        assert all(entry["type"] != "leaf" for entry in entries)
        step_entries = [entry for entry in entries if entry["type"] == "step"]
        assert [entry["action"]["name"] for entry in step_entries] == ["read", "respond"]
        assert step_entries[0]["stateDelta"]["findings"] == {"entrypoint": "main.py"}
        assert record["data"]["leafId"] is not None

    async def test_get_entries_since_cursor_returns_only_later_entries(self, project) -> None:
        session = await make_session(project, two_step_streams())
        await run_rpc(session, '{"id":"one","type":"prompt","message":"explain main.py"}\n')
        (full,) = await run_rpc(session, '{"id":"e","type":"get_entries"}\n')
        first_id = full["data"]["entries"][0]["id"]

        (record,) = await run_rpc(
            session, json.dumps({"id": "e2", "type": "get_entries", "since": first_id}) + "\n"
        )

        assert first_id not in {entry["id"] for entry in record["data"]["entries"]}
        assert len(record["data"]["entries"]) == len(full["data"]["entries"]) - 1

    async def test_get_tree_nests_entries_by_parent(self, project) -> None:
        session = await make_session(project, two_step_streams())
        await run_rpc(session, '{"id":"one","type":"prompt","message":"explain main.py"}\n')

        (record,) = await run_rpc(session, '{"id":"t","type":"get_tree"}\n')

        tree = record["data"]["tree"]
        assert len(tree) == 1
        assert tree[0]["entry"]["type"] == "session_info"
        assert tree[0]["children"][0]["entry"]["type"] == "turn"

    async def test_get_checkpoints_lists_state_carrying_entries(self, project) -> None:
        session = await make_session(project, two_step_streams())
        await run_rpc(session, '{"id":"one","type":"prompt","message":"explain main.py"}\n')

        (record,) = await run_rpc(session, '{"id":"c","type":"get_checkpoints"}\n')

        checkpoint_types = {entry["type"] for entry in record["data"]["checkpoints"]}
        assert checkpoint_types <= {"turn", "step", "state_reset"}
        assert len(record["data"]["checkpoints"]) >= 2

    async def test_restore_adopts_an_earlier_checkpoint_as_the_live_state(self, project) -> None:
        session = await make_session(project, two_step_streams())
        await run_rpc(session, '{"id":"one","type":"prompt","message":"explain main.py"}\n')
        (checkpoints,) = await run_rpc(session, '{"id":"c","type":"get_checkpoints"}\n')
        first_step_id = next(
            entry["id"] for entry in checkpoints["data"]["checkpoints"] if entry["type"] == "step"
        )

        (record,) = await run_rpc(
            session,
            json.dumps({"id": "r", "type": "restore", "entryId": first_step_id}) + "\n",
        )

        assert record["data"]["entryId"] == first_step_id
        assert session.answer is None
        assert session.state["findings"] == {"entrypoint": "main.py"}


class TestTransportRobustness:
    async def test_reports_bad_records_and_continues(self, project) -> None:
        session = await make_session(project, [])
        records = await run_rpc(session, 'not-json\n{"id":2,"type":"get_state"}\n')

        assert records[0]["success"] is False
        assert records[0]["command"] == "parse"
        assert records[1]["id"] == 2
        assert records[1]["success"] is True

    async def test_unknown_command_returns_an_error(self, project) -> None:
        session = await make_session(project, [])
        (record,) = await run_rpc(session, '{"id":"z","type":"nonsense"}\n')

        assert record["success"] is False
        assert "Unknown command" in record["error"]

    async def test_splits_only_on_lf_and_accepts_crlf(self, project) -> None:
        session = await make_session(project, [])
        separator = chr(0x2028)
        stdin = StringIO(f'{{"id":"a{separator}b","type":"get_state"}}\r\n')
        stdout = StringIO()

        await RpcServer(session, stdin=stdin, stdout=stdout).run()

        record = json.loads(stdout.getvalue())
        assert record["success"] is True
        assert record["id"] == f"a{separator}b"

    async def test_session_file_reports_the_jsonl_storage_path(self, project, tmp_path) -> None:
        session = await make_session(
            project, [], storage=JsonlSessionStorage(tmp_path / "session.jsonl")
        )
        (record,) = await run_rpc(session, '{"id":"s","type":"get_state"}\n')

        assert record["data"]["sessionFile"] == str(tmp_path / "session.jsonl")


async def test_rpc_reports_background_provider_failure_and_closes_session(project):
    session = await make_session(project, [])
    closed = []
    original_close = session.aclose

    async def close():
        closed.append(True)
        await original_close()

    session.aclose = close
    records = await run_rpc(session, '{"id":"bad","type":"prompt","message":"hello"}\n')
    errors = [record for record in records if record.get("success") is False]
    assert len(errors) == 1
    assert errors[0]["id"] == "bad"
    assert errors[0]["error"]
    assert closed == [True]
    assert not session.is_running


@pytest.mark.parametrize("command", ["new_session", "restore", "reload", "set_model"])
async def test_rpc_rejects_state_reconfiguration_during_run(command):
    from types import SimpleNamespace

    import anyio

    output = StringIO()
    server = RpcServer(SimpleNamespace(is_running=True), stdout=output)
    async with anyio.create_task_group() as tasks:
        await server._dispatch({"type": command, "id": "busy"}, tasks)
    response = json.loads(output.getvalue())
    assert response["success"] is False
    assert "active run" in response["error"]
