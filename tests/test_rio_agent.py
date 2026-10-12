"""Durability boundaries and recovery behavior, using real SQLite and uv processes."""

import asyncio
import copy
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from rio.agent.owner import Owner, RecoveryFailure, stop_worker
from rio.agent.prompt import CONCLUSION, PROTOCOL
from rio.agent.runner import run
from rio.agent.session import Session
from rio.agent.state import CellKind, InvalidPatch, Limits
from rio.agent.store import StorageFailure, Store, reduce_event
from rio.ai import FakeProvider
from rio.ai.messages import AssistantMessage, ToolCall
from rio.ai.provider_events import AssistantDoneEvent
from rio.coding.cells import KINDS


@pytest.fixture
def session(tmp_path):
    with Store(tmp_path / "session.sqlite3") as store:
        yield Session.create(store, "fix the code", tmp_path, kinds=KINDS)


def note(text="remember", **extra):
    return {"kind": "note", "text": text, **extra}


def code(source="print('hello')", **extra):
    return {
        "kind": "code",
        "runtime": "python",
        "source": "# /// script\n# dependencies = []\n# ///\n" + source + "\n",
        **extra,
    }


def cmd(*argv, **extra):
    return {"kind": "cmd", "argv": list(argv), **extra}


def prepare(session):
    return session.prepare_turn()


def add(cell):
    return {"op": "add", "cell": cell}


def replace_cell(cell_id, cell):
    return {"op": "replace", "id": cell_id, "cell": cell}


def remove(cell_id):
    return {"op": "remove", "id": cell_id}


def accept(session, *cells):
    turn = prepare(session)
    return session.accept(turn["id"], [add(c) for c in cells])


async def settle(owner):
    async with asyncio.timeout(15):
        await owner.tick()
        while owner.session.pending():
            await owner.wait()
            await owner.tick()


def test_sqlite_full_wal_and_exclusive_owner(session):
    assert session.store.db.execute("PRAGMA synchronous").fetchone() == (2,)
    assert session.store.db.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    with pytest.raises(StorageFailure, match="owner"):
        Store(session.store.path)


def test_atomic_admission_and_linear_history(session):
    accept(session, note("a"), note("b"))
    turn = prepare(session)
    session.accept(turn["id"], [replace_cell(1, note("new"))])
    assert [c["id"] for c in session.state["cells"]] == [3, 2]
    assert session.state["cells"][0] == {"id": 3, "previous_id": 1, **note("new")}
    assert session.data["cells"]["1"] == {"id": 1, "previous_id": None, **note("a")}
    turn = prepare(session)
    before = copy.deepcopy(session.data)
    with pytest.raises(InvalidPatch):
        session.accept(turn["id"], [add(note()), replace_cell(3, {"kind": "note", "text": 1})])
    assert session.data == before


def test_runtime_assigns_ids_positions_and_order_free_removal(session):
    accept(session, note("a"), note("b"), note("c"), note("d"))
    turn = prepare(session)
    session.accept(
        turn["id"],
        [remove(1), add(note("e")), replace_cell(3, note("c2")), remove(4), add(note("f"))],
    )
    cells = session.state["cells"]
    assert [(c["id"], c["previous_id"], c["text"]) for c in cells] == [
        (2, None, "b"),
        (6, 3, "c2"),
        (5, None, "e"),
        (7, None, "f"),
    ]
    assert session.state["runtime"]["next_cell_id"] == 8


@pytest.mark.parametrize(
    "operation",
    [
        {"op": "replace", "path": "/goal", "value": "weakened"},
        {"op": "move", "id": 1},
        {"op": ["add"], "cell": note()},
        {"op": "add", "id": 1, "cell": note()},
        {"op": "remove", "id": 1, "cell": note()},
        {"op": "replace", "cell": note()},
        {"op": "replace", "id": True, "cell": note()},
        add({"id": 5, "previous_id": None, **note()}),
        add(cmd()),
        add(cmd("ls", 1)),
        add(cmd("ls", source="x")),
        "add",
    ],
)
def test_restricted_ops(session, operation):
    accept(session, note())
    turn = prepare(session)
    with pytest.raises(InvalidPatch):
        session.accept(turn["id"], [operation])


def test_null_fields_from_strict_providers_are_omitted(session):
    from rio.ai.constrained_sampling import make_strict_json_schema

    strict = make_strict_json_schema(session.schema)["properties"]["ops"]["items"]
    assert strict["required"] == ["op", "id", "cell"]
    assert strict["properties"]["cell"]["anyOf"][0]["properties"]["kind"]["enum"] == [
        "note",
        "code",
        "cmd",
    ]
    turn = prepare(session)
    session.accept(turn["id"], [{"op": "add", "id": None, "cell": note(role=None)}])
    assert session.state["cells"] == [{"id": 1, "previous_id": None, **note()}]


def test_unknown_id_names_current_ids(session):
    accept(session, note(), note())
    turn = prepare(session)
    session.accept(turn["id"], [remove(1)])
    turn = prepare(session)
    with pytest.raises(InvalidPatch, match=r"Cell 1 is not in cells \(ids: \[2\]\)"):
        session.accept(turn["id"], [remove(1)])
    with pytest.raises(InvalidPatch, match=r"Cell 3 is not in cells .*new Cells get ids"):
        session.accept(turn["id"], [add(note()), replace_cell(3, note())])


def test_second_op_on_a_cell_names_the_cell(session):
    accept(session, note(), note())
    turn = prepare(session)
    with pytest.raises(InvalidPatch, match=r"ops\[1\]: Cell 1 already has an op"):
        session.accept(turn["id"], [replace_cell(1, note()), replace_cell(1, note())])
    with pytest.raises(InvalidPatch, match=r"ops\[1\]: Cell 1 already has an op"):
        session.accept(turn["id"], [remove(1), remove(1)])


def test_stale_turn_is_rejected(session):
    turn = prepare(session)
    state = session.meta["state"]
    session.store.commit("test_drift", {"meta": {"state": {**state, "revision": 99}}})
    with pytest.raises(InvalidPatch, match="Stale turn"):
        session.accept(turn["id"], [add(note())])
    assert not session.data["cells"]


def test_old_protocol_session_is_rejected(tmp_path):
    path = tmp_path / "old.sqlite3"
    with Store(path) as store:
        session = Session.create(store, "goal", tmp_path)
        state = {**session.meta["state"], "schema": "rio.state/1"}
        store.commit("test_downgrade", {"meta": {"state": state}})
    with Store(path) as store, pytest.raises(ValueError, match="rio.state/1.*new session"):
        Session(store)


def test_deduplication_and_conflicting_turn(session):
    turn = prepare(session)
    operations = [add(code())]
    receipt = session.accept(turn["id"], operations)
    seq = session.store.sequence
    assert session.accept(turn["id"], operations) == receipt
    assert session.store.sequence == seq
    assert len(session.data["executions"]) == 1
    with pytest.raises(InvalidPatch, match="Conflicting"):
        session.accept(turn["id"], [])


def test_busy_replace_rejected_removal_keeps_work(session):
    accept(session, code())
    turn = prepare(session)
    with pytest.raises(InvalidPatch, match="Cell 1 is queued or running.*cancel"):
        session.accept(turn["id"], [replace_cell(1, code())])
    session.accept(turn["id"], [remove(1)])
    assert not session.state["cells"]
    assert session.state["runtime"] == {"next_cell_id": 2, "cells": {}, "inbox": [], "usage": {}}
    assert session.pending()[0]["cell_id"] == 1
    assert "1" in prepare(session)["state"]["runtime"]["cells"]


def test_unchanged_successor_requests_new_execution(session):
    accept(session, code())
    session.finish(1, {"type": "success"})
    turn = prepare(session)
    session.accept(turn["id"], [replace_cell(1, code())])
    assert session.data["executions"]["2"]["phase"] == "queued"
    assert "result" not in session.data["executions"]["2"]


def test_fixed_snapshot_and_unrelated_response_cannot_clear_pause(session):
    accept(session, code(), code())
    turn = prepare(session)
    session.finish(1, {"type": "error"})
    assert prepare(session) == turn
    session.accept(turn["id"], [])
    assert session.meta["pause"] == "execution:1"
    turn = prepare(session)
    assert turn["delivered"] == ["execution:1"]
    session.accept(turn["id"], [])
    assert session.meta["pause"] is None


def test_inbox_deduplication_and_acknowledgment(session):
    session.receive("external:1", "external", {"fact": 1})
    session.receive("external:1", "external", {"fact": 1})
    with pytest.raises(ValueError, match="Conflicting"):
        session.receive("external:1", "external", {"fact": 2})
    turn = prepare(session)
    with pytest.raises(InvalidPatch):
        session.accept(turn["id"], [remove(1)])
    assert not session.data["inbox"]["external:1"]["consumed"]
    session.accept(turn["id"], [])
    assert session.data["inbox"]["external:1"]["consumed"]


def test_rebuild_is_pure_and_preserves_usage(session, monkeypatch):
    accept(session, code())
    session.finish(1, {"type": "UnknownExecution", "worker_stopped": True})
    turn = prepare(session)
    session.attempt(turn["id"])
    expected = copy.deepcopy(session.data)
    events = session.store.events()
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("reducer spawned work"))
    replay = {}
    for event in events:
        replay = reduce_event(replay, event["changes"], event["type"])
    assert replay == expected
    assert replay["meta"]["usage"]["unknown_usage"] == 1


def test_timer_generation_fires_once_after_restart(tmp_path):
    path = tmp_path / "timer.sqlite3"
    with Store(path) as store:
        session = Session.create(store, "wait", tmp_path)
        session.register_timer("alarm:1", 10, {"wake": True})
    with Store(path) as store:
        session = Session(store)
        session.fire_timers(11)
        session.fire_timers(12)
        session.register_timer("alarm:1", 10, {"wake": True})
        assert len(session.data["inbox"]) == 1
        with pytest.raises(ValueError, match="Conflicting"):
            session.register_timer("alarm:1", 12, {})


@pytest.mark.parametrize(
    "timer_request",
    [
        {"id": "", "deadline": 0, "payload": {}},
        {"id": 1, "deadline": 0, "payload": {}},
        {"id": "wake", "deadline": True, "payload": {}},
        {"id": "wake", "deadline": "0", "payload": {}},
        {"id": "wake", "deadline": float("inf"), "payload": {}},
        {"id": "wake", "deadline": float("nan"), "payload": {}},
        {"id": "wake", "deadline": 0, "payload": []},
        {},
        None,
    ],
)
async def test_timer_rejects_invalid_requests_without_committing(session, timer_request):
    if isinstance(timer_request, dict) and timer_request:
        with pytest.raises(ValueError):
            session.register_timer(
                timer_request["id"], timer_request["deadline"], timer_request["payload"]
            )
    async with Owner(session) as owner:
        sequence = session.store.sequence
        reader, writer = await asyncio.open_unix_connection(owner.socket_path)
        writer.write((json.dumps(timer_request) + "\n").encode())
        await writer.drain()
        response = json.loads(await asyncio.wait_for(reader.readline(), timeout=2))
        writer.close()
        await writer.wait_closed()
        assert "error" in response
        assert session.store.sequence == sequence
        assert not session.data["timers"]


async def test_serial_uv_runs_and_independent_variables(session):
    accept(
        session,
        code("from pathlib import Path\nx = 7\nPath('order').write_text('1')"),
        code(
            "from pathlib import Path\nassert 'x' not in globals()\n"
            "p=Path('order')\np.write_text(p.read_text()+'2')\nprint('ok')",
        ),
    )
    async with Owner(session) as owner:
        await settle(owner)
    assert (Path(session.meta["cwd"]) / "order").read_text() == "12"
    assert session.data["executions"]["2"]["output"] == "ok\n"
    assert all(e["result"]["type"] == "success" for e in session.data["executions"].values())


async def test_cmd_runs_argv_without_shell(session):
    accept(session, cmd(sys.executable, "-c", "import sys; print(sys.argv[1])", "$HOME"))
    accept(session, cmd("rio-missing-command"))
    async with Owner(session) as owner:
        await settle(owner)
    assert session.data["executions"]["1"]["output"] == "$HOME\n"
    assert session.data["executions"]["2"]["result"]["exit_code"] == 127
    assert session.meta["pause"] == "execution:2"


async def test_error_pauses_queue_then_cancel_before_dispatch(session):
    accept(session, code("raise RuntimeError('bad')"), code("assert False"))
    async with Owner(session) as owner:
        async with asyncio.timeout(15):
            while session.meta["pause"] is None:
                await owner.tick()
                await asyncio.sleep(0.02)
        assert session.data["executions"]["2"]["phase"] == "queued"
        accept(session, note(role="cancel", target_cell_id=2))
        await owner.tick()
    assert session.data["executions"]["2"]["result"]["type"] == "cancelled"
    assert "worker" not in session.data["executions"]["2"]


async def test_started_recovery_never_reexecutes(session):
    accept(session, code("from pathlib import Path\nPath('effect').touch()"))
    session.authorize(1, {"token": "not-a-real-worker"})
    async with Owner(session):
        assert session.data["executions"]["1"]["result"]["type"] == "UnknownExecution"
    assert not (Path(session.meta["cwd"]) / "effect").exists()
    assert session.meta["pause"] == "execution:1"
    seq = session.store.sequence
    async with Owner(session):
        pass
    assert session.store.sequence == seq


async def test_resolution_requires_evidence_and_keeps_original_unknown(session):
    accept(session, code())
    session.finish(1, {"type": "UnknownExecution"})
    original = copy.deepcopy(session.data["executions"]["1"])
    turn = prepare(session)
    with pytest.raises(InvalidPatch):
        session.accept(
            turn["id"],
            [add(note(role="resolution", target_cell_id=1, evidence_refs=[]))],
        )
    session.receive("inspection", "external", {"file_hash": "verified"})
    ref = f"session:event:{session.store.sequence}"
    session.accept(
        turn["id"],
        [add(note(role="resolution", target_cell_id=1, evidence_refs=[ref]))],
    )
    async with Owner(session):
        assert not session.unresolved()
    assert session.data["executions"]["1"] == original
    assert prepare(session)["state"]["runtime"]["cells"]["1"]["resolved_by"] == 2


async def test_success_rejected_for_new_input_and_failure_is_terminal(session):
    turn = prepare(session)
    session.receive("arrived", "external", {"updated": True})
    session.accept(turn["id"], [add(note(role="conclusion", result="success"))])
    async with Owner(session) as owner:
        assert not session.meta["terminal"]
        accept(session, note(role="conclusion", result="failure"))
        await owner.controls()
    assert session.meta["terminal"]["result"] == "failure"
    seq = session.store.sequence
    async with Owner(session):
        pass
    assert session.store.sequence == seq


async def test_cancel_running_worker_and_children(session):
    accept(
        session,
        code(
            "import subprocess,time\nfrom pathlib import Path\n"
            "p=subprocess.Popen(['sleep','60'])\nPath('child').write_text(str(p.pid))\ntime.sleep(60)",
        ),
    )
    async with Owner(session) as owner:
        await owner.tick()
        path = Path(session.meta["cwd"]) / "child"
        async with asyncio.timeout(15):
            while not path.exists():
                await asyncio.sleep(0.02)
        child_pid = int(path.read_text())
        accept(session, note(role="cancel", target_cell_id=1))
        await owner.controls()
    import psutil

    assert (
        not psutil.pid_exists(child_pid)
        or psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE
    )
    assert session.data["executions"]["1"]["result"]["type"] == "UnknownExecution"


def response(operations):
    message = AssistantMessage(
        content=[ToolCall(id="call", name="step", arguments={"ops": operations})]
    )
    return [AssistantDoneEvent(reason="toolUse", message=message)]


async def test_public_loop_finishes_and_reopens_without_model_call(session):
    provider = FakeProvider(
        [
            response([add(code())]),
            response(
                [
                    add(note("completed", role="conclusion", result="success")),
                ]
            ),
        ]
    )
    published = []
    assert await run(session, provider, "fake", published.append)
    assert len(provider.calls) == 2
    assert any(e["type"] == "run_ended" for e in published)
    assert await run(session, FakeProvider([]), "fake")


async def test_successful_completions_wake_the_model_once_after_the_queue_drains(session):
    provider = FakeProvider(
        [
            response(
                [
                    add(cmd(sys.executable, "-c", "print(1)")),
                    add(cmd(sys.executable, "-c", "print(2)")),
                ]
            ),
            response(
                [
                    add(note("completed", role="conclusion", result="success")),
                ]
            ),
        ]
    )
    assert await run(session, provider, "fake")
    state = json.loads(provider.calls[1][2][0].content)
    assert [m["id"] for m in state["runtime"]["inbox"]] == ["execution:1", "execution:2"]


async def test_instructions_extend_system_prompt(tmp_path):
    with Store(tmp_path / "session.sqlite3") as store:
        session = Session.create(store, "goal", tmp_path, instructions="\n\nUse tabs.")
        conclude = note("done", role="conclusion", result="success")
        provider = FakeProvider([response([add(conclude)])])
        assert await run(session, provider, "fake")
        # A note-only agent never hears about execution.
        assert provider.calls[0][1] == PROTOCOL + CONCLUSION + "\n\nUse tabs."


def test_note_only_agent_rejects_runnable_cells(tmp_path):
    with Store(tmp_path / "s.db") as store:
        session = Session.create(store, "goal", tmp_path)
        with pytest.raises(InvalidPatch, match="Cell kind must be one of: note$"):
            accept(session, code())
        with pytest.raises(InvalidPatch, match="Cell kind must be"):
            accept(session, {**note(), "kind": ["note"]})
        assert accept(session, note())["cell_ids"] == [1]


def test_note_is_not_an_extension_kind(tmp_path):
    with Store(tmp_path / "s.db") as store, pytest.raises(ValueError, match="built-in"):
        Session.create(store, "goal", tmp_path, kinds=[replace(KINDS[0], name="note")])
    with Store(tmp_path / "s.db") as store:
        assert not store.data


@pytest.mark.parametrize("fields", [{"text": {"type": "integer"}}, {"id": {"type": "integer"}}])
def test_kind_fields_share_one_cell_schema(tmp_path, fields):
    with Store(tmp_path / "s.db") as store, pytest.raises(ValueError, match="redefines field"):
        Session.create(store, "goal", tmp_path, kinds=[replace(KINDS[0], fields=fields)])
    with Store(tmp_path / "n.db") as store:
        cell = Session.create(store, "goal", tmp_path).schema["properties"]["ops"]["items"]
        assert cell["properties"]["cell"]["properties"]["kind"]["enum"] == ["note"]


async def test_owner_requires_the_recorded_cell_kinds(tmp_path):
    path = tmp_path / "s.db"
    with Store(path) as store:
        accept(Session.create(store, "goal", tmp_path, kinds=KINDS), code())
    with Store(path) as store:
        assert Session(store).state["goal"] == "goal"
        with pytest.raises(ValueError, match="Cell kinds: code"):
            async with Owner(Session(store)):
                pass


async def test_caller_extends_agent_with_a_cell_kind(tmp_path):
    def validate(cell):
        if not isinstance(cell.get("text"), str):
            raise InvalidPatch("Echo requires text")

    echo = CellKind(
        name="echo",
        fields={"text": {"type": "string"}},
        prompt='Echo: {"kind":"echo","text":"..."}.\n',
        validate=validate,
        command=lambda cell, scratch: [
            sys.executable,
            "-c",
            "import sys; print(sys.argv[1])",
            cell["text"],
        ],
    )
    with Store(tmp_path / "s.db") as store:
        session = Session.create(store, "goal", tmp_path, kinds=[echo])
        assert echo.prompt in session.system
        with pytest.raises(InvalidPatch, match="Echo requires text"):
            accept(session, {"kind": "echo"})
        accept(session, {"kind": "echo", "text": "hi"})
        async with Owner(session) as owner:
            await settle(owner)
        assert session.data["executions"]["1"]["output"] == "hi\n"


async def test_uv_timer_api_acknowledges_committed_registration(session):
    accept(session, code("from rio.agent.api import timer\ntimer('wake:1', 0, {'done':True})"))
    async with Owner(session) as owner:
        await settle(owner)
    assert session.data["timers"]["wake:1"]["fired"]
    assert session.data["executions"]["1"]["result"]["type"] == "success"


async def test_owner_wakes_on_commits_and_deadlines_without_polling(session):
    import time

    accept(session, cmd(sys.executable, "-c", "print('done')"))
    async with Owner(session) as owner:
        # Worker output, the result commit, and the task ending each wake the owner.
        wakes = 0
        async with asyncio.timeout(10):
            await owner.tick()
            while session.pending() or owner.task:
                await owner.wait()
                await owner.tick()
                wakes += 1
        assert session.data["executions"]["1"]["output"] == "done\n"
        assert wakes < 10
        # With nothing changing, a wait sleeps until the next timer deadline, then fires it.
        session.register_timer("wake", time.time() + 0.3, {})
        await owner.tick()
        started = time.monotonic()
        await owner.wait()
        assert 0.25 < time.monotonic() - started < 1
        await owner.tick()
        assert session.data["timers"]["wake"]["fired"]
        # Input committed while waiting wakes it at once.
        waiting = asyncio.create_task(owner.wait(time.time() + 60))
        await asyncio.sleep(0)
        session.receive("external:1", "external", {})
        async with asyncio.timeout(1):
            await waiting


async def test_loop_sleeps_until_timer_instead_of_polling(session, monkeypatch):
    import time

    turn = prepare(session)
    session.accept(turn["id"], [])
    session.register_timer("wake", time.time() + 0.3, {})
    ticks = 0
    tick = Owner.tick

    async def counted(self):
        nonlocal ticks
        ticks += 1
        await tick(self)

    monkeypatch.setattr(Owner, "tick", counted)
    provider = FakeProvider([response([add(note(role="conclusion", result="success"))])])
    started = time.monotonic()
    assert await run(session, provider, "fake")
    assert time.monotonic() - started >= 0.25
    # Polling every 50ms would tick about six times before the timer fires.
    assert ticks <= 4
    state = json.loads(provider.calls[0][2][0].content)
    assert [m["id"] for m in state["runtime"]["inbox"]] == ["timer:wake"]


def test_output_limit_is_explicit(session):
    accept(session, code())
    session.authorize(1, {"token": "none"})
    session.limits = replace(session.limits, output_bytes=7)
    session.output(1, "old text", "old error")
    session.output(1, "é終!", "é終!")
    execution = session.data["executions"]["1"]
    assert execution["output"] == "té終!"
    assert execution["stderr"] == "ré終!"
    assert len(execution["output"].encode()) == session.limits.output_bytes
    assert execution["truncated"]
    session.output(1, "尾", "尾")
    assert session.data["executions"]["1"]["output"] == "終!尾"
    session.output(1, "é")
    assert session.data["executions"]["1"]["output"] == "!尾é"


async def test_truncated_output_keeps_tail_and_full_output_file(session):
    source = "print('\\n'.join(map(str, range(3000))))"
    accept(session, cmd("python3", "-c", source), cmd("true"))
    async with Owner(session) as owner:
        await settle(owner)
    execution = session.data["executions"]["1"]
    assert execution["truncated"]
    assert execution["output"].startswith("1000\n")
    path = execution["output"].rsplit("Full output: ", 1)[1].rstrip("]")
    assert execution["output"].endswith(f"[Showing lines 1001-3000 of 3000. Full output: {path}]")
    assert Path(path).read_text().splitlines() == [str(i) for i in range(3000)]
    assert not session.data["executions"]["2"]["truncated"]


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
async def test_worker_and_validator_output_keep_utf8_tail(session, stream):
    session.limits = replace(session.limits, output_bytes=7, flush_seconds=60)
    source = f"import sys; sys.{stream}.write('old text' + 'é終!' * 3000)"
    accept(session, cmd(sys.executable, "-c", source))
    async with Owner(session) as owner:
        await settle(owner)
        result = await owner._validate("tail", [sys.executable, "-c", source])
    assert result == {"exit": 0, "output": "!é終!"}
    key = "output" if stream == "stdout" else "stderr"
    streamed = [
        event["changes"]["output_chunks"]["1"][key]
        for event in session.store.events()
        if event["type"] == "execution_output"
    ]
    assert streamed == ["!é終!"]
    execution = session.data["executions"]["1"]
    assert execution[key].startswith("!é終!")
    path = execution[key].rsplit("Full output: ", 1)[1].rstrip("]")
    assert Path(path).read_text() == "old text" + "é終!" * 3000
    Path(path).unlink()


async def test_required_validator_blocks_success(tmp_path):
    with Store(tmp_path / "s.db") as store:
        session = Session.create(
            store, "goal", tmp_path, validators=((sys.executable, "-c", "raise SystemExit(1)"),)
        )
        accept(session, note(role="conclusion", result="success"))
        async with Owner(session):
            assert session.meta["terminal"] is None
            assert session.data["validator_results"]["0"]["exit"] == 1


def test_recycled_pid_fails_closed():
    with pytest.raises(RecoveryFailure, match="reused"):
        stop_worker({"pid": os.getpid(), "birth": 0, "token": "old"})


def test_commit_uncertainty_reopens_committed_identity(tmp_path):
    import sqlite3

    path = tmp_path / "uncertain.db"
    with Store(path) as store:
        session = Session.create(store, "goal", tmp_path, kinds=KINDS)
        turn = prepare(session)
        connection = store.db

        class UncertainCommit:
            def execute(self, sql, *args):
                result = connection.execute(sql, *args)
                if sql == "COMMIT":
                    raise sqlite3.OperationalError("lost commit acknowledgment")
                return result

            def close(self):
                connection.close()

        store.db = UncertainCommit()
        with pytest.raises(StorageFailure):
            session.accept(turn["id"], [add(code())])
        assert store.failed and store.data == {}
        with pytest.raises(StorageFailure):
            store.events()
    with Store(path) as store:
        session = Session(store, KINDS)
        receipt = session.accept(turn["id"], [add(code())])
        assert receipt["cell_ids"] == [1]
        assert len(session.data["executions"]) == 1


@pytest.mark.parametrize("boundary", ["queued", "authorized", "running"])
async def test_process_kill_at_commit_boundaries(tmp_path, boundary):
    import signal
    import subprocess

    path = tmp_path / "crash.db"
    source = """
import asyncio, sys
from pathlib import Path
from rio.agent.session import Session
from rio.agent.store import Store
from rio.agent.owner import Owner
from rio.coding.cells import KINDS
async def main():
    root=Path(sys.argv[1]); boundary=sys.argv[2]
    with Store(root/'crash.db') as store:
        s=Session.create(store, 'one effect', root, kinds=KINDS)
        t=s.prepare_turn()
        s.accept(t['id'], [{'op':'add','cell':{'kind':'code','runtime':'python','source':
             "# /// script\\n# dependencies = []\\n# ///\\n"
             "from pathlib import Path\\nimport time\\n"
             "p=Path('effects'); p.write_text(p.read_text()+'x' if p.exists() else 'x')\\n"
             "time.sleep(60)\\n"}}])
        if boundary == 'authorized': s.authorize(1, {'token':'unlaunched'})
        if boundary == 'running':
            async with Owner(s) as owner:
                await owner.tick()
                while not (root/'effects').exists(): await asyncio.sleep(.01)
                (root/'ready').touch()
                await asyncio.sleep(100)
        else:
            (root/'ready').touch()
            await asyncio.sleep(100)
asyncio.run(main())
"""
    process = subprocess.Popen([sys.executable, "-c", source, str(tmp_path), boundary])
    try:
        async with asyncio.timeout(15):
            while not (tmp_path / "ready").exists():
                assert process.poll() is None
                await asyncio.sleep(0.02)
        os.kill(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        await asyncio.sleep(0.1)  # Allow the supervisor's EOF cleanup to finish.
        with Store(path) as store:
            session = Session(store, KINDS)
            async with Owner(session):
                execution = session.data["executions"]["1"]
                if boundary == "queued":
                    assert execution["phase"] == "queued"
                else:
                    assert execution["result"]["type"] == "UnknownExecution"
                assert len(session.data["executions"]) == 1
            effect = tmp_path / "effects"
            assert effect.read_text() == "x" if boundary == "running" else not effect.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


async def test_budget_survives_restart_and_idle_is_bounded(tmp_path, monkeypatch):
    import rio.agent.runner as runner

    monkeypatch.setattr(runner.time, "time", lambda: 1e20)
    with Store(tmp_path / "budget.db") as store:
        session = Session.create(store, "goal", tmp_path, limits=replace(Limits(), rounds=2))
        provider = FakeProvider(
            [
                response([]),
                response([]),
            ]
        )
        assert not await run(session, provider, "fake")
        assert session.meta["usage"]["rounds"] == 2
    with Store(tmp_path / "budget.db") as store:
        session = Session(store)
        assert not await run(session, FakeProvider([]), "fake")
        assert session.meta["usage"]["rounds"] == 2


def test_context_keeps_each_output_whole(session):
    accept(session, code(), code())
    for i in (1, 2):
        session.authorize(i, {"token": "none"})
        session.output(i, "x" * session.limits.output_bytes)
        session.finish(i, {"type": "success"})
    turn = prepare(session)
    for key in ("1", "2"):
        assert turn["state"]["runtime"]["cells"][key]["output"] == "x" * session.limits.output_bytes
    session.accept(turn["id"], [])


async def test_saved_complete_response_is_accepted_without_another_provider_call(session):
    turn = prepare(session)
    session.attempt(turn["id"])
    operations = [add(note(role="conclusion", result="success"))]
    message = response(operations)[0].message
    session.response(turn["id"], message.model_dump(mode="json"), 12)
    provider = FakeProvider([])
    assert await run(session, provider, "fake")
    assert not provider.calls
    assert session.meta["usage"]["tokens"] == 12


async def test_lost_and_partial_model_responses_are_bounded(session, monkeypatch):
    import rio.agent.runner as runner

    monkeypatch.setattr(runner.time, "time", lambda: 1e20)
    provider = FakeProvider([])
    assert not await run(session, provider, "fake")
    assert len(provider.calls) == session.limits.invalid_attempts
    assert not session.data["cells"]
    assert session.meta["usage"]["unknown_usage"] == session.limits.invalid_attempts


def test_success_receipt_and_ending_share_a_transaction(session):
    accept(session, note(role="conclusion", result="success"))
    session.end("success", "done", note_id="1")
    ending = session.store.events()[-1]
    assert ending["changes"]["meta"]["terminal"]["result"] == "success"
    assert ending["changes"]["controls"]["1"]["handled"]


async def test_uncertain_output_commit_stops_worker_before_more_effects(session, monkeypatch):
    accept(
        session,
        code(
            "import time\nfrom pathlib import Path\n"
            "print('before', flush=True)\ntime.sleep(5)\n"
            "Path('late-effect').touch()",
        ),
    )
    commit = session.store.commit

    def uncertain(kind, changes, **kwargs):
        if kind == "execution_output":
            commit(kind, changes, **kwargs)
            session.store.failed = True
            session.store.data = {}
            raise StorageFailure("lost output commit acknowledgment")
        return commit(kind, changes, **kwargs)

    monkeypatch.setattr(session.store, "commit", uncertain)
    with pytest.raises(StorageFailure):
        async with Owner(session) as owner:
            await settle(owner)
    assert not (session.store.path.parent / "late-effect").exists()


def test_cli_defaults_to_durable_and_resume_needs_no_new_goal(monkeypatch):
    from rio.cli import app
    from rio.cli import run as run_module

    calls = []

    async def run_session(*args, **kwargs):
        calls.append((args, kwargs))
        return True, "abc"

    monkeypatch.setattr(run_module, "run_persistent_session", run_session)
    app(["run", "--resume", "abc", "--output", "json"])
    assert calls[0][0] == ("",)
    assert calls[0][1]["resume"] == "abc"


@pytest.mark.parametrize("alias_kind", ["symlink", "hardlink"])
def test_database_alias_cannot_acquire_a_second_owner(session, tmp_path, alias_kind):
    alias = tmp_path / "alias.sqlite3"
    if alias_kind == "symlink":
        alias.symlink_to(session.store.path)
    else:
        os.link(session.store.path, alias)
    with pytest.raises(StorageFailure, match="owner"), Store(alias):
        pass


async def test_provider_name_does_not_make_missing_usage_known(session):
    operations = [
        add(note(role="conclusion", result="success")),
    ]
    events = response(operations)
    events[0].message.api = "provider-without-usage"
    assert await run(session, FakeProvider([events]), "fake")
    assert session.meta["usage"]["unknown_usage"] == 1


def test_terminal_run_cannot_prepare_an_unaccepted_request(session):
    prepare(session)
    session.end("failure", "stopped")
    with pytest.raises(ValueError, match="Terminal"):
        prepare(session)


def test_runtime_selector_is_removed():
    from rio.cli import build_parser

    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(["run", "--runtime", "notebook", "goal"])
    assert error.value.code == 2


@pytest.mark.parametrize("prefix", ["execution", "control", "timer", "idle"])
def test_external_ids_cannot_overwrite_harness_observations(session, prefix):
    before = session.store.sequence
    with pytest.raises(ValueError, match="reserved"):
        session.receive(f"{prefix}:1", "external", {"fact": "new"})
    assert session.store.sequence == before


@pytest.mark.parametrize("cell_kind", ["code", "cmd"])
@pytest.mark.parametrize("output_mode", ["human", "json"])
async def test_cli_persists_sqlite_and_resumes_without_repeating_work(
    tmp_path, monkeypatch, capsys, output_mode, cell_kind
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from rio.cli import run as cli
    from rio.coding.paths import RioPaths
    from rio.coding.rendering import PrintOutputMode

    paths = RioPaths(home=tmp_path / "home")
    monkeypatch.setattr(cli, "RioPaths", lambda: paths)
    selection = SimpleNamespace(provider=SimpleNamespace(name="fake"), model="fake")
    monkeypatch.setattr(cli, "load_provider_settings", lambda: None)
    monkeypatch.setattr(cli, "resolve_provider_selection", lambda *a, **kw: selection)
    monkeypatch.setattr(cli, "resolve_startup_thinking_level", lambda *a, **kw: None)
    source = "from pathlib import Path\nPath('effect').write_text('once')"
    work = code(source) if cell_kind == "code" else cmd(sys.executable, "-c", source)
    provider = FakeProvider(
        [
            response(
                [
                    add(work),
                ]
            ),
            response(
                [
                    add(note("done", role="conclusion", result="success")),
                ]
            ),
        ]
    )
    provider.aclose = AsyncMock()
    monkeypatch.setattr(cli, "create_model_provider", lambda *a, **kw: provider)
    ok, session_id = await cli.run_persistent_session(
        "write a file",
        cwd=tmp_path,
        output_mode=PrintOutputMode(output_mode),
    )
    assert ok
    assert (tmp_path / "effect").read_text() == "once"
    transcript = capsys.readouterr().out
    if output_mode == "json":
        events = [json.loads(line) for line in transcript.splitlines()]
        assert {event["session_id"] for event in events} == {session_id}
        assert events[-1]["type"] == "run_ended"
        assert events[-1]["changes"]["metrics"]["journal_bytes"] > 0
    else:
        if cell_kind == "code":
            assert "• # /// script" in transcript
            assert "  Path('effect').write_text('once')" in transcript
        else:
            assert sys.executable in transcript
            assert " -c " in transcript
        assert "  └ success (exit 0)" in transcript
        assert "• done" in transcript
        assert transcript.endswith("• Success: done\n")
        assert "Cell" not in transcript
    path = paths.sessions_dir / f"{session_id}.sqlite3"
    with Store(path) as store:
        assert Session(store).state["goal"] == "write a file"
        assert len(store.data["executions"]) == 1
    (tmp_path / "effect").write_text("changed after completion")
    assert await cli.run_persistent_session(
        "",
        resume=session_id,
        output_mode=PrintOutputMode(output_mode),
    ) == (True, session_id)
    assert capsys.readouterr().out == transcript
    assert len(provider.calls) == 2
    assert (tmp_path / "effect").read_text() == "changed after completion"
    assert provider.aclose.await_count == 1


def test_idle_decision_and_budget_survive_reopen(session):
    from rio.agent.runner import _idle_action

    owner = Owner(session)
    assert _idle_action(owner) == "ready"
    accept(session)
    for _ in range(session.limits.invalid_attempts):
        assert _idle_action(owner) == "invalid"
        session.invalid_idle()
        assert _idle_action(owner) == "ready"
        accept(session)
    assert _idle_action(Owner(Session(session.store, KINDS))) == "fail"
    accept(session, code())
    session.request_work()
    assert session.meta["idle"] == 0
    assert _idle_action(owner) == "wait"


@pytest.mark.parametrize("source", ["worker", "timer", "external"])
def test_idle_waits_for_wakeup_sources(session, source):
    from rio.agent.runner import _idle_action

    accept(session)
    owner = Owner(session)
    if source == "worker":
        owner.task = object()
    elif source == "timer":
        session.register_timer("later", 1e20, {})
    else:
        session.external_wakeup = True
    assert _idle_action(owner) == "wait"
    session.receive("new", "external", {})
    assert _idle_action(owner) == "ready"


@pytest.mark.parametrize("result", ["success", "error"])
def test_idle_batches_only_successful_execution_observations(session, result):
    from rio.agent.runner import _idle_action

    accept(session, code(), code())
    session.authorize(1, {"token": "none"})
    session.finish(1, {"type": result})
    assert _idle_action(Owner(session)) == ("wait" if result == "success" else "ready")
    session.receive("new", "external", {})
    assert _idle_action(Owner(session)) == "ready"


@pytest.mark.parametrize("legacy", [False, True])
def test_external_wakeup_policy_survives_reopen(tmp_path, legacy):
    path = tmp_path / "wakeup.db"
    with Store(path) as store:
        session = Session.create(store, "goal", tmp_path, external_wakeup=not legacy)
        if legacy:
            # Simulate the metadata written before external_wakeup moved out of Limits.
            meta = dict(session.meta)
            del meta["external_wakeup"]
            meta["limits"] = {**meta["limits"], "external_wakeup": True}
            store.db.execute(
                "UPDATE events SET changes = ? WHERE seq = 1",
                (json.dumps({**store.events()[0]["changes"], "meta": meta}),),
            )
    with Store(path) as store:
        session = Session(store)
        assert session.external_wakeup is True
        assert not hasattr(session.limits, "external_wakeup")


@pytest.mark.parametrize("spare", [-1, 0])
def test_context_budget_counts_utf8_and_inbox_commas(session, spare):
    from rio.agent.store import encode

    session.receive("first", "external", {"text": "é" * 1200})
    session.receive("second", "external", {"text": "é" * 1200})
    expected = session.state
    expected["revision"] += 1
    expected["runtime"]["usage"] = dict(session.meta["usage"])
    expected["runtime"]["inbox"] = [
        {k: v for k, v in m.items() if k != "consumed"} for m in session.data["inbox"].values()
    ]
    budget = len(encode(expected).encode()) + spare
    session.limits = replace(session.limits, context_bytes=budget, output_bytes=128)
    turn = prepare(session)
    assert turn["delivered"] == (["first"] if spare == -1 else ["first", "second"])
    assert len(encode(turn["state"]).encode()) <= budget
    session.accept(turn["id"], [])
    if spare == -1:
        assert prepare(session)["delivered"] == ["second"]


def test_run_end_records_metrics_once(session):
    session.end("success", "done")
    seq = session.store.sequence
    assert session.store.event_kind(seq) == "run_ended"
    assert set(session.data["metrics"]) == {
        "commit_p50_seconds",
        "commit_max_seconds",
        "rebuild_seconds",
        "journal_bytes",
    }
    session.end("failure", "again")
    assert session.store.sequence == seq


@pytest.mark.parametrize("ref", ["session:event:99999", "session:event:1"])
def test_resolution_rejects_missing_events_and_non_observations(session, ref):
    accept(session, code())
    session.authorize(1, {"token": "none"})
    session.finish(1, {"type": "UnknownExecution", "worker_stopped": True})
    turn = prepare(session)
    before = session.store.sequence
    with pytest.raises(InvalidPatch, match="recorded observation"):
        session.accept(
            turn["id"], [add(note(role="resolution", target_cell_id=1, evidence_refs=[ref]))]
        )
    assert session.store.sequence == before


@pytest.mark.parametrize("result", ["success", "failure"])
@pytest.mark.parametrize("output_mode", ["human", "json"])
async def test_terminal_cli_resume_needs_no_provider_or_project(
    tmp_path, monkeypatch, capsys, result, output_mode
):
    import rio.agent.owner as owner_module
    from rio.cli import run as cli
    from rio.coding.paths import RioPaths
    from rio.coding.rendering import PrintOutputMode

    paths = RioPaths(home=tmp_path / "home")
    monkeypatch.setattr(cli, "RioPaths", lambda: paths)

    def unavailable(*args, **kwargs):
        raise AssertionError("terminal replay must not configure a provider")

    monkeypatch.setattr(cli, "load_provider_settings", unavailable)
    monkeypatch.setattr(cli, "create_model_provider", unavailable)
    project = tmp_path / "project"
    project.mkdir()
    session_id = "a" * 32
    path = paths.sessions_dir / f"{session_id}.sqlite3"
    with Store(path) as store:
        session = Session.create(
            store,
            "goal",
            project,
            kinds=KINDS,
            model_config={"provider": "removed", "model": "retired"},
        )
        accept(session, code())
        session.authorize(1, {"token": "leftover"})
        session.finish(1, {"type": "success"})
        session.end(result, "done")
    project.rmdir()
    stopped = []
    monkeypatch.setattr(owner_module, "stop_worker", lambda identity: stopped.append(identity))
    assert await cli.run_persistent_session(
        "", resume=session_id, output_mode=PrintOutputMode(output_mode)
    ) == (result == "success", session_id)
    assert stopped == [{"token": "leftover"}]
    transcript = capsys.readouterr().out
    if output_mode == "json":
        events = [json.loads(line) for line in transcript.splitlines()]
        assert any(e["type"] == "worker_cleanup" for e in events)
        assert "durability_metrics" not in {e["type"] for e in events}
    else:
        assert "done" in transcript
    with Store(path) as store:
        assert store.data["executions"]["1"]["worker_stopped"]
        assert store.data["meta"]["terminal"]["result"] == result


async def test_terminal_replay_propagates_cleanup_failure(session, monkeypatch):
    import rio.agent.owner as owner_module
    from rio.agent.runner import replay

    accept(session, code())
    session.authorize(1, {"token": "leftover"})
    session.finish(1, {"type": "success"})
    session.end("success", "done")

    def fail(identity):
        raise RecoveryFailure("cannot stop worker")

    monkeypatch.setattr(owner_module, "stop_worker", fail)
    published = []
    with pytest.raises(RecoveryFailure, match="cannot stop"):
        await replay(session, published.append)
    assert not published
    assert not session.data["executions"]["1"]["worker_stopped"]


async def test_replay_without_workers_writes_nothing(session):
    from rio.agent.runner import replay

    session.end("success", "done")
    seq = session.store.sequence
    published = []
    assert await replay(session, published.append)
    assert session.store.sequence == seq
    assert published[-1]["type"] == "run_ended"


async def test_replay_rejects_active_session(session):
    from rio.agent.runner import replay

    with pytest.raises(ValueError, match="terminal"):
        await replay(session)


def test_turn_events_store_state_once_and_replay_deltas(session):
    turn = session.prepare_turn()
    turn_id = turn["id"]
    prepared = session.store.events()[-1]
    assert "state" not in prepared["changes"]["meta"]
    session.attempt(turn_id)
    session.response(turn_id, {"text": "first"}, 5)
    session.reject(turn_id, "retry")
    session.attempt(turn_id)
    session.response(turn_id, {"text": "second"}, 7)
    session.accept(turn_id, [add(note())])
    for event in session.store.events(after=prepared["seq"]):
        fields = event["changes"].get("turns", {}).get(turn_id, {})
        assert not {"state", "system", "tool_schema", "model_config"} & fields.keys()
        state_delta = event["changes"].get("meta", {}).get("state", {})
        assert not {"goal", "schema", "id"} & state_delta.keys()
        assert state_delta.get("runtime", {}).get("usage", {}) == {}
    expected = copy.deepcopy(session.data)
    path = session.store.path
    session.store.close()
    with Store(path) as reopened:
        assert reopened.data == expected
        assert reopened.data["turns"][turn_id]["attempts"] == 2
        assert reopened.data["meta"]["usage"]["tokens"] == 12


def test_output_journal_grows_linearly_and_replays_legacy_records(session):
    accept(session, cmd("true"))
    session.authorize(1, {"token": "none"})
    # Older records contain a full execution snapshot.
    old = {**session.data["executions"]["1"], "output": "legacy\n"}
    session.store.commit("execution_output", {"executions": {"1": old}})
    sizes = []
    for _ in range(20):
        before = session.store.journal_bytes
        session.output(1, "x" * 100, "warning\n")
        sizes.append(session.store.journal_bytes - before)
    assert len(set(sizes)) == 1
    assert max(sizes) < 250
    expected = copy.deepcopy(session.data)
    session.store.close()
    with Store(session.store.path) as reopened:
        assert reopened.data == expected
        assert reopened.data["executions"]["1"]["output"] == "legacy\n" + "x" * 2000


@pytest.mark.parametrize("failure", ["timeout", "oserror"])
async def test_worker_failure_flushes_last_output_and_cleans_logs(session, monkeypatch, failure):
    import tempfile

    logs = Path(session.meta["cwd"]) / "logs"
    logs.mkdir()
    original = tempfile.NamedTemporaryFile
    monkeypatch.setattr(
        tempfile, "NamedTemporaryFile", lambda *a, **kw: original(*a, dir=logs, **kw)
    )
    session.limits = replace(session.limits, execution_seconds=0.5, flush_seconds=10)
    accept(
        session,
        cmd(
            sys.executable,
            "-u",
            "-c",
            'import time; print("last stdout"); '
            'import sys; print("last stderr", file=sys.stderr); time.sleep(30)',
        ),
    )
    if failure == "oserror":
        readline = asyncio.StreamReader.readline

        async def fail_after_output(reader):
            line = await readline(reader)
            if b"stderr" in line:
                # Return this buffered chunk, then fail the next read.
                monkeypatch.setattr(reader, "readline", fail_read)
            return line

        async def fail_read():
            raise OSError("read failed")

        monkeypatch.setattr(asyncio.StreamReader, "readline", fail_after_output)
    async with Owner(session) as owner:
        await settle(owner)
    execution = session.data["executions"]["1"]
    assert execution["output"] == "last stdout\n"
    assert execution["stderr"] == "last stderr\n"
    assert execution["result"]["type"] == "UnknownExecution"
    assert not list(logs.iterdir())


async def test_cells_preserve_existing_pythonpath(session, monkeypatch):
    module_dir = Path(session.meta["cwd"]) / "modules"
    module_dir.mkdir()
    (module_dir / "user_module.py").write_text("value = 42\n")
    monkeypatch.setenv("PYTHONPATH", str(module_dir))
    accept(session, cmd(sys.executable, "-c", "import user_module; print(user_module.value)"))
    async with Owner(session) as owner:
        await settle(owner)
    assert session.data["executions"]["1"]["output"] == "42\n"


def test_output_chunks_replay_rolling_utf8_tails(session):
    session.limits = replace(session.limits, output_bytes=7)
    accept(session, cmd("true"))
    session.authorize(1, {"token": "none"})
    for text in ("old text", "é終!", "é終!"):
        session.output(1, text, text)
    expected = copy.deepcopy(session.data)
    assert expected["executions"]["1"]["output"] == "!é終!"
    assert expected["executions"]["1"]["stderr"] == "!é終!"
    assert expected["executions"]["1"]["truncated"]
    session.store.close()
    with Store(session.store.path) as reopened:
        assert reopened.data == expected
