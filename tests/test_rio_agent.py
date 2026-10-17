"""Durability boundaries and recovery behavior, using real SQLite and uv processes."""

import asyncio
import copy
import json
import math
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import get_args

import pytest
from pydantic import ValidationError

from rio.agent.ledger import RESERVED
from rio.agent.prompt import CONCLUSION, PROTOCOL
from rio.agent.runner import Owner, RecoveryFailure, replay, run
from rio.agent.session import RunStopped, Session
from rio.agent.shapes import ControlType
from rio.agent.state import (
    CONTROL_FIELDS,
    CONTROLS,
    NOTE,
    USAGE,
    CellKind,
    InvalidPatch,
    Limits,
    zero_usage,
)
from rio.agent.store import StorageFailure, Store, reduce_event
from rio.ai import FakeProvider
from rio.ai.messages import AssistantMessage, ToolCall
from rio.coding.cells import KINDS
from rio.coding.owner import LOG_BYTES, ProcessOwner, stop_worker


@pytest.fixture
def session(tmp_path):
    with Store(tmp_path / "session.sqlite3") as store:
        yield Session.create(
            store, "fix the code", kinds=KINDS, owner_config={"cwd": str(tmp_path)}
        )


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


def control(kind, reason="because", **fields):
    return {"op": "control", "control": {"type": kind, "reason": reason, **fields}}


def conclude(result="success", reason="done"):
    return control("conclusion", reason, result=result)


def accept(session, *items):
    """Accept Cells (added) and ops, in order."""
    turn = prepare(session)
    return session.accept(turn["id"], [i if "op" in i else add(i) for i in items])


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


@pytest.mark.parametrize(
    ("method", "args", "collection", "key", "event", "fields"),
    [
        (
            "authorize",
            (2,),
            "executions",
            "2",
            "execution_started",
            {"phase": "started", "started": 123.0},
        ),
        (
            "truncate_output",
            (1, {"stdout": "tail"}),
            "executions",
            "1",
            "output_truncated",
            {"output": {"stdout": "tail", "stderr": "diagnostic"}, "truncated": True},
        ),
        (
            "record",
            ("jobs", "v", {"result": {"passed": True}}),
            "jobs",
            "v",
            "owner_record",
            {"result": {"passed": True}},
        ),
        (
            "record",
            ("new_jobs", "new", {"collection": "c", "key": "k", "event": "e"}),
            "new_jobs",
            "new",
            "owner_record",
            {"collection": "c", "key": "k", "event": "e"},
        ),
    ],
)
def test_runtime_record_updates_preserve_journal_and_replay(
    tmp_path, monkeypatch, method, args, collection, key, event, fields
):
    monkeypatch.setattr("rio.agent.ledger.time.time", lambda: 123.0)
    path = tmp_path / "session.sqlite3"
    with Store(path) as store:
        session = Session.create(store, "goal", kinds=KINDS)
        accept(session, cmd("first"), cmd("second"))
        session.authorize(1)
        session.append_output(1, {"stdout": "full output", "stderr": "diagnostic"})
        session.finish(1, {"type": "success"})
        session.record("jobs", "v", {"attempts": 1, "result": None})
        session.record("jobs", "other", {"attempts": 2})
        before = session.data
        snapshot = copy.deepcopy(before)
        expected = copy.deepcopy(before)
        expected.setdefault(collection, {}).setdefault(key, {}).update(fields)
        sequence = store.sequence

        getattr(session, method)(*args)

        assert session.data == expected
        assert before == snapshot
        assert store.sequence == sequence + 1
        committed = store.events(sequence)
        assert committed[0]["type"] == event
        assert committed[0]["changes"] == {collection: {key: expected[collection][key]}}

    with Store(path) as store:
        assert Session(store, KINDS).data == expected


@pytest.mark.parametrize("method,args", [("authorize", (99,)), ("truncate_output", (99, {}))])
def test_runtime_execution_updates_require_existing_record(session, method, args):
    sequence = session.store.sequence
    before = copy.deepcopy(session.data)
    with pytest.raises(KeyError):
        getattr(session, method)(*args)
    assert session.data == before
    assert session.store.sequence == sequence


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
        add(note(role="conclusion", result="success")),
        "add",
        {**control("cancel", target_cell_id=1), "id": 1},
        {"op": "control", "control": "cancel"},
        control("restart"),
        control("cancel"),
        control("cancel", target_cell_id="1"),
        control("cancel", target_cell_id=1, result="success"),
        control("conclusion", result="maybe"),
        control("conclusion", " ", result="success"),
        control("resolution", target_cell_id=1, evidence_refs=[]),
    ],
)
def test_restricted_ops(session, operation):
    accept(session, note())
    turn = prepare(session)
    with pytest.raises(InvalidPatch):
        session.accept(turn["id"], [operation])


@pytest.mark.parametrize(
    ("operation", "message"),
    [
        (control("cancel"), r"ops\[0\]: cancel control requires target_cell_id: id of a Cell"),
        (
            control("cancel", target_cell_id=1, result="success"),
            r"cancel control takes target_cell_id, reason; remove \['result'\]",
        ),
        (control("restart"), "Control type must be one of: cancel, resolution, conclusion"),
        (control("cancel", target_cell_id=9), "Cell 9 has no recorded execution"),
        (add(note(role="cancel")), r"Unknown Cell fields: \['role'\]"),
    ],
)
def test_control_rejections_name_the_fix(session, operation, message):
    turn = prepare(session)
    with pytest.raises(InvalidPatch, match=message):
        session.accept(turn["id"], [operation])


def test_ops_sent_as_a_json_string_are_decoded(session):
    turn = prepare(session)
    session.accept(turn["id"], json.dumps([add(note("a"))]))
    assert session.state["cells"] == [{"id": 1, "previous_id": None, **note("a")}]


def test_conclusion_cannot_share_a_step_with_runnable_cells(session):
    turn = prepare(session)
    with pytest.raises(InvalidPatch, match="conclude in a later step"):
        session.accept(turn["id"], [add(cmd("ls")), conclude("failure")])
    receipt = session.accept(turn["id"], [add(note("summary")), conclude()])
    assert receipt["control_ids"] == [1]


def test_null_fields_from_strict_providers_are_omitted(session):
    accept(session, cmd("ls"))
    turn = prepare(session)
    stop = {"type": "cancel", "target_cell_id": 1, "reason": "stale", "result": None}
    session.accept(
        turn["id"],
        [
            {"op": "add", "id": None, "cell": note(argv=None), "control": None},
            {"op": "control", "id": None, "cell": None, "control": stop},
        ],
    )
    assert session.state["cells"][-1] == {"id": 2, "previous_id": None, **note()}
    assert session.data["controls"] == {
        "1": {
            "control": {"type": "cancel", "target_cell_id": 1, "reason": "stale"},
            "handled": False,
        }
    }


def test_controls_stay_out_of_cells_and_take_their_own_ids(session):
    accept(session, cmd("ls"), cmd("ls"))
    receipt = accept(
        session,
        control("cancel", "stale", target_cell_id=2),
        note("kept"),
        control("cancel", target_cell_id=1),
    )
    assert receipt["cell_ids"] == [3] and receipt["control_ids"] == [1, 2]
    assert [c["kind"] for c in session.state["cells"]] == ["cmd", "cmd", "note"]
    assert session.state["runtime"]["next_cell_id"] == 4
    assert accept(session, conclude())["control_ids"] == [3]
    turn = prepare(session)
    # A control is no Cell, so it cannot be replaced or removed.
    with pytest.raises(InvalidPatch, match="not in cells"):
        session.accept(turn["id"], [remove(4)])


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
        session = Session.create(store, "goal")
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
    assert session.state["runtime"] == {
        "next_cell_id": 2,
        "cells": {},
        "inbox": [],
        "usage": zero_usage(),
    }
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
    session.finish(1, {"type": "UnknownExecution"})
    turn = prepare(session)
    session.start_attempt(turn["id"])
    expected = copy.deepcopy(session.data)
    events = session.store.events()
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("reducer spawned work"))
    replay = {}
    for event in events:
        replay = reduce_event(replay, event["changes"])
    assert replay == expected
    assert replay["meta"]["usage"]["unknown_usage"] == 1


def test_timer_generation_fires_once_after_restart(tmp_path):
    path = tmp_path / "timer.sqlite3"
    with Store(path) as store:
        session = Session.create(store, "wait")
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
    async with ProcessOwner(session) as owner:
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
    async with ProcessOwner(session) as owner:
        await settle(owner)
    assert (Path(session.meta["owner_config"]["cwd"]) / "order").read_text() == "12"
    assert session.data["executions"]["2"]["output"]["stdout"] == "ok\n"
    assert all(e["result"]["type"] == "success" for e in session.data["executions"].values())


async def test_cmd_runs_argv_without_shell(session):
    accept(session, cmd(sys.executable, "-c", "import sys; print(sys.argv[1])", "$HOME"))
    accept(session, cmd("rio-missing-command"))
    async with ProcessOwner(session) as owner:
        await settle(owner)
    assert session.data["executions"]["1"]["output"]["stdout"] == "$HOME\n"
    assert session.data["executions"]["2"]["result"]["exit_code"] == 127
    assert session.meta["pause"] == "execution:2"


@pytest.mark.parametrize("venv", [None, "/project/.venv"])
async def test_active_venv_keeps_uv_run_from_syncing_it(session, monkeypatch, venv):
    if venv:
        monkeypatch.setenv("VIRTUAL_ENV", venv)
    else:
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.delenv("UV_NO_SYNC", raising=False)
    monkeypatch.delenv("UV_PROJECT_ENVIRONMENT", raising=False)
    show = "import os; print(*map(os.environ.get, ['UV_NO_SYNC', 'UV_PROJECT_ENVIRONMENT']))"
    accept(session, cmd(sys.executable, "-c", show))
    # A PEP 723 script still installs its own dependencies, without a no-op warning.
    script = code("import iniconfig\nprint('ok')")
    accept(session, {**script, "source": script["source"].replace("[]", '["iniconfig"]')})
    async with ProcessOwner(session) as owner:
        await settle(owner)
    executions = session.data["executions"]
    assert executions["1"]["output"]["stdout"] == ("1 " + venv if venv else "None None") + "\n"
    assert executions["2"]["output"] == {"stdout": "ok\n"}


async def test_error_pauses_queue_then_cancel_before_dispatch(session):
    accept(session, code("raise RuntimeError('bad')"), code("assert False"))
    async with ProcessOwner(session) as owner:
        async with asyncio.timeout(15):
            while session.meta["pause"] is None:
                await owner.tick()
                await asyncio.sleep(0.02)
        assert session.data["executions"]["2"]["phase"] == "queued"
        accept(session, control("cancel", target_cell_id=2))
        await owner.tick()
    assert session.data["executions"]["2"]["result"]["type"] == "cancelled"
    assert "cell:2" not in session.data.get("workers", {})


async def test_started_recovery_never_reexecutes(session):
    accept(session, code("from pathlib import Path\nPath('effect').touch()"))
    session.authorize(1)
    session.record("workers", "cell:1", {"token": "not-a-real-worker"})
    async with ProcessOwner(session):
        assert session.data["executions"]["1"]["result"]["type"] == "UnknownExecution"
    assert not (Path(session.meta["owner_config"]["cwd"]) / "effect").exists()
    assert session.meta["pause"] == "execution:1"
    seq = session.store.sequence
    async with ProcessOwner(session):
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
            [control("resolution", target_cell_id=1, evidence_refs=[])],
        )
    session.receive("inspection", "external", {"file_hash": "verified"})
    ref = f"session:event:{session.store.sequence}"
    session.accept(
        turn["id"],
        [control("resolution", target_cell_id=1, evidence_refs=[ref])],
    )
    async with ProcessOwner(session):
        assert not session.unresolved()
    assert session.data["executions"]["1"] == original
    assert prepare(session)["state"]["runtime"]["cells"]["1"]["resolved_by"] == 1


def folded(store):
    data = {}
    for event in store.events():
        data = reduce_event(data, event["changes"])
    return data


async def test_runtime_cells_leave_the_live_projection_a_fold_of_events(session, monkeypatch):
    accept(session, code(), code())
    session.authorize(1)
    session.finish(1, {"type": "UnknownExecution"})
    turn = prepare(session)
    session.receive("inspection", "external", {"file_hash": "verified"})
    ref = f"session:event:{session.store.sequence}"
    session.accept(turn["id"], [control("resolution", target_cell_id=1, evidence_refs=[ref])])
    async with Owner(session) as owner:
        await owner.handle_controls()
    turn = prepare(session)
    assert turn["state"]["runtime"]["cells"]["1"]["resolved_by"] == 1
    assert "resolved_by" not in session.data["executions"]["1"]
    assert session.data == folded(session.store)
    session.accept(turn["id"], [control("cancel", target_cell_id=2)])
    prepared = Session.prepare_turn

    def checked(self):
        turn = prepared(self)
        assert self.data == folded(self.store)
        return turn

    monkeypatch.setattr(Session, "prepare_turn", checked)
    assert await run(Owner(session), FakeProvider([response([conclude()])]), "fake")
    expected = copy.deepcopy(session.data)
    assert expected == folded(session.store)
    session.store.close()
    with Store(session.store.path) as reopened:
        assert reopened.data == expected


def test_live_projection_holds_journaled_json(tmp_path):
    with Store(tmp_path / "s.db") as store:
        Session.create(store, "goal", validators=(("check", 1),))
        assert store.data["meta"]["validators"] == [["check", 1]]
        assert store.data == folded(store)


async def test_cancel_requires_started_cell_to_be_active(session):
    accept(session, code())
    session.authorize(1)
    accept(session, control("cancel", target_cell_id=1))
    with pytest.raises(RuntimeError, match="Started Cell 1 is not the active execution"):
        await Owner(session).handle_controls()


async def test_success_rejected_for_new_input_and_failure_is_terminal(session):
    turn = prepare(session)
    session.receive("arrived", "external", {"updated": True})
    session.accept(turn["id"], [conclude()])
    async with ProcessOwner(session) as owner:
        assert not session.meta["terminal"]
        accept(session, conclude("failure"))
        await owner.handle_controls()
    assert session.meta["terminal"]["result"] == "failure"
    ending = session.store.events()[-1]
    assert ending["type"] == "run_ended"
    assert all(record["handled"] for record in session.data["controls"].values())
    assert ending["changes"]["controls"]
    # The rejected success names itself, since controls never enter State.
    rejected = session.data["inbox"]["control:1"]
    assert rejected["kind"] == "control_rejected"
    assert rejected["payload"]["control"] == conclude()["control"]
    assert rejected["payload"]["reason"].startswith("New observations")
    seq = session.store.sequence
    async with ProcessOwner(session):
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
    async with ProcessOwner(session) as owner:
        await owner.tick()
        path = Path(session.meta["owner_config"]["cwd"]) / "child"
        async with asyncio.timeout(15):
            while not path.exists():
                await asyncio.sleep(0.02)
        child_pid = int(path.read_text())
        accept(session, control("cancel", target_cell_id=1))
        await owner.handle_controls()
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
    return message


async def test_public_loop_finishes_and_reopens_without_model_call(session):
    provider = FakeProvider(
        [
            response([add(code())]),
            response(
                [
                    conclude(),
                ]
            ),
        ]
    )
    published = []
    assert await run(ProcessOwner(session), provider, "fake", published.append)
    assert len(provider.calls) == 2
    assert any(e["type"] == "run_ended" for e in published)
    assert await run(ProcessOwner(session), FakeProvider([]), "fake")


async def test_base_owner_runs_note_only_sessions_without_processes(tmp_path):
    provider = FakeProvider([response([conclude()])])
    with Store(tmp_path / "notes.sqlite3") as store:
        session = Session.create(store, "say hello")
        ok = await run(Owner(session), provider, "fake")
        print([e["changes"] for e in store.events() if e["type"] == "patch_rejected"])
        assert ok
        assert await replay(Owner(session))


async def test_model_state_keeps_stable_prefix_after_resume(tmp_path):
    path = tmp_path / "prefix.sqlite3"
    with Store(path) as store:
        session = Session.create(store, "Remember café and 日本語.")
        accept(session, note("Stable memory. " * 1000))
        saved = copy.deepcopy(prepare(session))
        # Existing journals put revision before goal and cells.
        assert list(saved["state"]) == ["schema", "id", "revision", "goal", "cells", "runtime"]

    provider = FakeProvider([response([]), response([conclude()])])
    with Store(path) as store:
        session = Session(store)
        assert await run(Owner(session), provider, "fake")
        assert session.data["turns"][saved["id"]]["state"] == saved["state"]
        assert len(provider.calls) == 2
        first, second = [call[2][0].content for call in provider.calls]
        assert json.loads(first) == saved["state"]
        assert list(json.loads(first)) == ["schema", "id", "goal", "cells", "revision", "runtime"]
        assert json.loads(first)["revision"] != json.loads(second)["revision"]
        assert json.loads(first)["runtime"] != json.loads(second)["runtime"]
        prefix = first.split(',"revision":', 1)[0]
        assert len(prefix.encode()) > 14000
        assert second.startswith(prefix + ',"revision":')


async def test_base_owner_recovers_started_cells_as_unknown(session):
    accept(session, code())
    session.authorize(1)
    async with Owner(session):
        result = session.data["executions"]["1"]["result"]
    assert result["type"] == "UnknownExecution"
    assert result["reason"] == "owner_lost_before_result_commit"


def test_owner_records_merge_outside_session_collections(session):
    session.record("workers", "cell:1", {"token": "t"})
    session.record("workers", "cell:1", {"stopped": True})
    assert session.data["workers"] == {"cell:1": {"token": "t", "stopped": True}}
    assert session.store.events()[-1]["type"] == "owner_record"
    for collection in ("executions", "meta", "output_chunks"):
        with pytest.raises(ValueError, match="Session collection"):
            session.record(collection, "1", {})


async def test_interrupted_validator_blocks_success_after_restart(tmp_path):
    with Store(tmp_path / "s.db") as store:
        session = Session.create(store, "goal", validators=[["true"]])
        accept(session, conclude())
        # A validator worker was registered, then the owner died before its result committed.
        session.record("workers", "validator:1:0", {"token": "lost-validator-token"})
        async with ProcessOwner(session):
            assert session.data["workers"]["validator:1:0"]["stopped"]
            assert not session.data["validator_results"]["1:0"]["passed"]
            assert session.data["controls"]["1"]["outcome"].startswith("Required validator 0")
            assert session.meta["terminal"] is None


async def test_finished_validator_result_is_reused_after_restart(tmp_path):
    with Store(tmp_path / "s.db") as store:
        session = Session.create(store, "goal", validators=[["false"]])
        accept(session, conclude())
        # The validator finished and stopped, then the owner died before the core result.
        result = {"passed": True, "exit": 0, "output": "ok"}
        session.record(
            "workers", "validator:1:0", {"token": "t", "result": result, "stopped": True}
        )
        async with ProcessOwner(session):
            assert session.data["validator_results"]["1:0"] == result
            assert session.meta["terminal"]["result"] == "success"


async def test_base_owner_cannot_launch_runnable_cells(session):
    accept(session, code())
    async with Owner(session) as owner:
        with pytest.raises(NotImplementedError):
            await owner.tick()


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
                    conclude(),
                ]
            ),
        ]
    )
    assert await run(ProcessOwner(session), provider, "fake")
    state = json.loads(provider.calls[1][2][0].content)
    assert [m["id"] for m in state["runtime"]["inbox"]] == ["execution:1", "execution:2"]


async def test_instructions_extend_system_prompt(tmp_path):
    with Store(tmp_path / "session.sqlite3") as store:
        session = Session.create(store, "goal", instructions="\n\nUse tabs.")
        provider = FakeProvider([response([conclude()])])
        assert await run(ProcessOwner(session), provider, "fake")
        # A note-only agent never hears about execution.
        assert provider.calls[0][1] == PROTOCOL + NOTE.prompt + CONCLUSION + "\n\nUse tabs."


def test_note_only_agent_rejects_runnable_cells(tmp_path):
    with Store(tmp_path / "s.db") as store:
        session = Session.create(store, "goal")
        with pytest.raises(InvalidPatch, match="Cell kind must be one of: note$"):
            accept(session, code())
        with pytest.raises(InvalidPatch, match="Cell kind must be"):
            accept(session, {**note(), "kind": ["note"]})
        assert accept(session, note())["cell_ids"] == [1]


def test_control_types_match_control_table():
    # Session admits and Owner handles every declared type; the Literal mirrors the table.
    assert get_args(ControlType) == tuple(CONTROLS) == tuple(Owner.CONTROL_HANDLERS)
    assert Session.CONTROL_CHECKS.keys() <= CONTROLS.keys()
    assert {f for kind in CONTROLS.values() for f in kind.fields} < CONTROL_FIELDS.keys()


def test_note_is_not_an_extension_kind(tmp_path):
    with Store(tmp_path / "s.db") as store, pytest.raises(ValueError, match="built-in"):
        Session.create(store, "goal", kinds=[replace(KINDS[0], name="note")])
    with Store(tmp_path / "s.db") as store:
        assert not store.data


@pytest.mark.parametrize("fields", [{"text": {"type": "integer"}}, {"id": {"type": "integer"}}])
def test_kind_fields_share_one_cell_schema(tmp_path, fields):
    with Store(tmp_path / "s.db") as store, pytest.raises(ValueError, match="redefines field"):
        Session.create(store, "goal", kinds=[replace(KINDS[0], fields=fields)])
    with Store(tmp_path / "n.db") as store:
        cell = Session.create(store, "goal").schema["properties"]["ops"]["items"]
        assert cell["properties"]["cell"]["properties"]["kind"]["enum"] == ["note"]


async def test_owner_requires_the_recorded_cell_kinds(tmp_path):
    path = tmp_path / "s.db"
    with Store(path) as store:
        accept(Session.create(store, "goal", kinds=KINDS), code())
    with Store(path) as store:
        assert Session(store).state["goal"] == "goal"
        with pytest.raises(ValueError, match="Cell kinds: code"):
            async with ProcessOwner(Session(store)):
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
    )

    def command(cell, scratch):
        return [sys.executable, "-c", "import sys; print(sys.argv[1])", cell["text"]]

    with Store(tmp_path / "s.db") as store:
        session = Session.create(store, "goal", kinds=[echo])
        assert echo.prompt in session.system
        with pytest.raises(InvalidPatch, match="Echo requires text"):
            accept(session, {"kind": "echo"})
        accept(session, {"kind": "echo", "text": "hi"})
        async with ProcessOwner(session, {"echo": command}) as owner:
            await settle(owner)
        assert session.data["executions"]["1"]["output"]["stdout"] == "hi\n"


async def test_uv_timer_api_acknowledges_committed_registration(session):
    accept(session, code("from rio.coding.api import timer\ntimer('wake:1', 0, {'done':True})"))
    async with ProcessOwner(session) as owner:
        await settle(owner)
    assert session.data["timers"]["wake:1"]["fired"]
    assert session.data["executions"]["1"]["result"]["type"] == "success"


async def test_owner_wakes_on_commits_and_deadlines_without_polling(session):
    import time

    accept(session, cmd(sys.executable, "-c", "print('done')"))
    async with ProcessOwner(session) as owner:
        # Worker output, the result commit, and the task ending each wake the owner.
        wakes = 0
        async with asyncio.timeout(10):
            await owner.tick()
            while session.pending() or owner.task:
                await owner.wait()
                await owner.tick()
                wakes += 1
        assert session.data["executions"]["1"]["output"]["stdout"] == "done\n"
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
    provider = FakeProvider([response([conclude()])])
    started = time.monotonic()
    assert await run(ProcessOwner(session), provider, "fake")
    assert time.monotonic() - started >= 0.25
    # Polling every 50ms would tick about six times before the timer fires.
    assert ticks <= 4
    state = json.loads(provider.calls[0][2][0].content)
    assert [m["id"] for m in state["runtime"]["inbox"]] == ["timer:wake"]


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"context_bytes": 4095, "output_bytes": 1}, "context_bytes must be at least 4096"),
        ({"context_bytes": 4096, "output_bytes": 1025}, "output_bytes <= context_bytes / 4"),
    ],
)
def test_limits_reserve_context(fields, message):
    with pytest.raises(ValueError, match=message):
        Limits(**fields)


def test_output_limit_is_explicit(session):
    accept(session, code())
    session.authorize(1)
    session.limits = replace(session.limits, output_bytes=7)
    session.append_output(1, {"stdout": "old text", "stderr": "old error"})
    session.append_output(1, {"stdout": "é終!", "stderr": "é終!"})
    output = session.data["executions"]["1"]["output"]
    assert output == {"stdout": "té終!", "stderr": "ré終!"}
    assert len(output["stdout"].encode()) == session.limits.output_bytes
    assert session.data["executions"]["1"]["truncated"]
    session.append_output(1, {"stdout": "尾", "stderr": "尾"})
    assert session.data["executions"]["1"]["output"]["stdout"] == "終!尾"
    session.append_output(1, {"stdout": "é"})
    assert session.data["executions"]["1"]["output"] == {"stdout": "!尾é", "stderr": "終!尾"}


async def test_truncated_output_keeps_tail_and_full_output_file(session):
    source = "print('\\n'.join(map(str, range(3000))))"
    accept(session, cmd("python3", "-c", source), cmd("true"))
    async with ProcessOwner(session) as owner:
        await settle(owner)
        execution = session.data["executions"]["1"]
        assert execution["truncated"]
        stdout = execution["output"]["stdout"]
        assert stdout.startswith("1000\n")
        path = stdout.rsplit("Full output: ", 1)[1].rstrip("]")
        assert stdout.endswith(f"[Showing lines 1001-3000 of 3000. Full output: {path}]")
        assert Path(path).read_text().splitlines() == [str(i) for i in range(3000)]
        assert not session.data["executions"]["2"]["truncated"]
        assert sorted(p.name for p in Path(path).parent.glob("*.log")) == ["cell-1-stdout.log"]
    # Logs last as long as the owner.
    assert not Path(path).exists()


async def test_output_logs_stay_within_disk_budget(session):
    session.limits = replace(session.limits, output_bytes=1024)
    source = "import sys; sys.stdout.write('x' * 100_000)"
    accept(session, cmd(sys.executable, "-c", source), cmd(sys.executable, "-c", source))
    async with ProcessOwner(session, log_bytes=150_000) as owner:
        await settle(owner)
        first, second = (session.data["executions"][k]["output"]["stdout"] for k in "12")
        logs = Path(owner.temporary.name)
        assert first.endswith(f"Full output: {logs / 'cell-1-stdout.log'}]")
        path = logs / "cell-2-stdout.log"
        assert second.endswith(f"First 48.8KB of 97.7KB, log budget reached: {path}]")
        assert path.stat().st_size == 50_000
        assert owner.log_budget == 0


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
async def test_worker_and_validator_output_keep_utf8_tail(session, stream):
    session.limits = replace(session.limits, output_bytes=7, flush_seconds=60)
    source = f"import sys; sys.{stream}.write('old text' + 'é終!' * 3000)"
    accept(session, cmd(sys.executable, "-c", source))
    async with ProcessOwner(session) as owner:
        await settle(owner)
        result = await owner.validate("tail", [sys.executable, "-c", source])
        assert result == {"passed": True, "exit": 0, "output": "!é終!"}
        streamed = [
            event["changes"]["output_chunks"]["1"]["output"][stream]
            for event in session.store.events()
            if event["type"] == "execution_output"
        ]
        assert streamed == ["!é終!"]
        execution = session.data["executions"]["1"]
        assert execution["output"][stream].startswith("!é終!")
        path = execution["output"][stream].rsplit("Full output: ", 1)[1].rstrip("]")
        assert Path(path).read_text() == "old text" + "é終!" * 3000


@pytest.mark.parametrize("exit_code", [0, 7, None])
async def test_worker_reader_decodes_streams_independently_and_flushes_eof(session, exit_code):
    reader = asyncio.StreamReader()
    messages = [
        {"stdout": b"old \xc3".hex()},
        {"stderr": b"err \xe7".hex()},
        {"stdout": b"\xa9".hex(), "stderr": b"\xb5\x82".hex()},
        {"stdout": b"\xff\xe2".hex()},
    ]
    if exit_code is not None:
        messages.append({"exit": exit_code})
    for message in messages:
        reader.feed_data((json.dumps(message) + "\n").encode())
    reader.feed_eof()
    output = {"stdout": "", "stderr": ""}

    def append(name, text):
        output[name] += text

    owner = ProcessOwner(session)
    assert await owner._read_output(SimpleNamespace(stdout=reader), append) == exit_code
    assert output == {"stdout": "old é��", "stderr": "err 終"}


@pytest.mark.parametrize("ending", ["eof", "timeout", "cancel"])
async def test_worker_reader_flushes_while_quiet_and_releases_pending_read(session, ending):
    session.limits = replace(session.limits, flush_seconds=0.01, execution_seconds=0.1)
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"stdout":"6869"}\n')
    output = []
    flushed = asyncio.Event()
    owner = ProcessOwner(session)
    task = asyncio.create_task(
        owner._read_output(
            SimpleNamespace(stdout=reader), lambda _, text: output.append(text), flushed.set
        )
    )
    await asyncio.wait_for(flushed.wait(), timeout=1)
    assert "".join(output) == "hi"
    assert not task.done()
    if ending == "eof":
        reader.feed_eof()
        assert await task is None
    else:
        if ending == "cancel":
            task.cancel()
        with pytest.raises(TimeoutError if ending == "timeout" else asyncio.CancelledError):
            await task
        # A subsequent read must not compete with an abandoned readline task.
        reader.feed_data(b"next\n")
        assert await reader.readline() == b"next\n"


async def test_validator_timeout_records_result_and_stops_worker(session):
    session.limits = replace(session.limits, execution_seconds=0.1)
    async with ProcessOwner(session) as owner:
        result = await owner.validate(
            "timeout", [sys.executable, "-c", "import time; time.sleep(30)"]
        )
    assert result == {
        "passed": False,
        "exit": None,
        "output": "Validator interrupted; inspect effects before retrying",
    }
    worker = session.data["workers"]["validator:timeout"]
    assert worker["result"] == result
    assert worker["stopped"]


async def test_required_validator_blocks_success(tmp_path):
    with Store(tmp_path / "s.db") as store:
        session = Session.create(
            store, "goal", validators=((sys.executable, "-c", "raise SystemExit(1)"),)
        )
        accept(session, conclude())
        async with ProcessOwner(session):
            assert session.meta["terminal"] is None
            assert session.data["validator_results"]["1:0"]["exit"] == 1


def test_recycled_pid_fails_closed():
    with pytest.raises(RecoveryFailure, match="reused"):
        stop_worker({"pid": os.getpid(), "birth": 0, "token": "old"})


def test_commit_uncertainty_reopens_committed_identity(tmp_path):
    import sqlite3

    path = tmp_path / "uncertain.db"
    with Store(path) as store:
        session = Session.create(store, "goal", kinds=KINDS)
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
from rio.coding.owner import ProcessOwner
from rio.coding.cells import KINDS
async def main():
    root=Path(sys.argv[1]); boundary=sys.argv[2]
    with Store(root/'crash.db') as store:
        s=Session.create(store, 'one effect', kinds=KINDS, owner_config={'cwd': str(root)})
        t=s.prepare_turn()
        s.accept(t['id'], [{'op':'add','cell':{'kind':'code','runtime':'python','source':
             "# /// script\\n# dependencies = []\\n# ///\\n"
             "from pathlib import Path\\nimport time\\n"
             "p=Path('effects'); p.write_text(p.read_text()+'x' if p.exists() else 'x')\\n"
             "time.sleep(60)\\n"}}])
        if boundary == 'authorized': s.authorize(1)
        if boundary == 'running':
            async with ProcessOwner(s) as owner:
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
            async with ProcessOwner(session):
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


def test_retries_use_one_turn_and_keep_prepared_context(session):
    session.limits = replace(session.limits, turns=1)
    turn = session.prepare_turn()
    session.start_attempt(turn["id"])
    session.reject(turn["id"], "invalid operations")
    session.receive("new", "external", {"text": "arrived during the turn"})
    retry = session.prepare_turn()
    assert retry["id"] == turn["id"]
    assert retry["state"] == turn["state"]
    session.start_attempt(turn["id"])
    assert session.meta["usage"]["turns"] == 1
    assert session.meta["usage"]["attempts"] == 2
    session.accept(turn["id"], [add(note())])
    assert not session.data["inbox"]["new"]["consumed"]
    with pytest.raises(RunStopped, match="Model budget exhausted"):
        session.prepare_turn()


async def test_budget_survives_restart_and_idle_is_bounded(tmp_path, monkeypatch):
    import rio.agent.runner as runner

    monkeypatch.setattr(runner.time, "time", lambda: 1e20)
    with Store(tmp_path / "budget.db") as store:
        session = Session.create(store, "goal", limits=replace(Limits(), turns=2))
        provider = FakeProvider(
            [
                response([]),
                response([]),
            ]
        )
        assert not await run(ProcessOwner(session), provider, "fake")
        assert session.meta["usage"]["turns"] == 2
    with Store(tmp_path / "budget.db") as store:
        session = Session(store)
        assert not await run(ProcessOwner(session), FakeProvider([]), "fake")
        assert session.meta["usage"]["turns"] == 2


@pytest.mark.parametrize(
    ("limit", "prepared", "reason"),
    [
        ("storage_bytes", False, "Session storage budget exhausted"),
        ("turns", False, "Model budget exhausted"),
        ("tokens", False, "Model budget exhausted"),
        ("context_bytes", False, "Goal and Cells exceed context without runtime facts"),
        ("invalid_attempts", True, "Model retry budget exhausted"),
        ("tokens", True, "Token budget exhausted"),
    ],
)
async def test_run_stops_at_budget_limits(session, limit, prepared, reason):
    if limit == "context_bytes":
        accept(session, note("x" * 5000))
        session.limits = replace(session.limits, context_bytes=4096, output_bytes=1024)
    else:
        if limit in ("turns", "tokens", "invalid_attempts"):
            turn = prepare(session)
            session.start_attempt(turn["id"])
            if limit != "invalid_attempts":
                session.record_response(turn["id"], response([]).model_dump(mode="json"), 1)
                session.accept(turn["id"], [])
                if prepared:
                    prepare(session)
        session.limits = replace(session.limits, **{limit: 1})
    provider = FakeProvider([])
    published = []

    assert not await run(Owner(session), provider, "fake", published.append)

    assert session.meta["terminal"]["reason"] == reason
    assert not provider.calls
    assert sum(event["type"] == "run_ended" for event in published) == 1


@pytest.mark.parametrize("ended", [False, True])
def test_completed_turn_cannot_attempt_again(session, ended):
    turn = prepare(session)
    if ended:
        session.end("failure", "stopped")
    else:
        session.accept(turn["id"], [])
    sequence = session.store.sequence
    with pytest.raises(RunStopped, match="Turn already accepted or run ended"):
        session.start_attempt(turn["id"])
    assert session.store.sequence == sequence


@pytest.mark.parametrize(
    "method", ["prepare_turn", "start_attempt", "record_response", "accept", "request_work"]
)
async def test_unexpected_value_error_escapes_run(session, monkeypatch, method):
    error = ValueError("unexpected conversion error")

    def broken(*args, **kwargs):
        raise error

    monkeypatch.setattr(session, method, broken)
    provider = FakeProvider([response([])])
    with pytest.raises(ValueError) as raised:
        await run(Owner(session), provider, "fake")

    assert raised.value is error
    assert session.meta["terminal"] is None
    assert not any(e["type"] in ("run_ended", "patch_rejected") for e in session.store.events())
    assert session.store.on_commit is None


async def test_invalid_saved_response_escapes_run(session):
    turn = prepare(session)
    session.start_attempt(turn["id"])
    session.record_response(turn["id"], {"content": 123}, 12)
    sequence = session.store.sequence
    provider = FakeProvider([])

    with pytest.raises(ValidationError):
        await run(Owner(session), provider, "fake")

    assert session.meta["terminal"] is None
    assert session.store.sequence == sequence
    assert not provider.calls


async def test_invalid_patch_is_retried_without_ending_run(session, monkeypatch):
    import rio.agent.runner as runner

    monkeypatch.setattr(runner.time, "time", lambda: 1e20)
    provider = FakeProvider([response([{"op": "invalid"}]), response([conclude()])])
    assert await run(Owner(session), provider, "fake")
    assert len(provider.calls) == 2
    events = session.store.events()
    assert sum(e["type"] == "patch_rejected" for e in events) == 1
    assert sum(e["type"] == "run_ended" for e in events) == 1


def test_context_keeps_each_output_whole(session):
    accept(session, code(), code())
    for i in (1, 2):
        session.authorize(i)
        session.append_output(i, {"stdout": "x" * session.limits.output_bytes})
        session.finish(i, {"type": "success"})
    turn = prepare(session)
    for key in ("1", "2"):
        output = turn["state"]["runtime"]["cells"][key]["output"]
        assert output == {"stdout": "x" * session.limits.output_bytes}
    session.accept(turn["id"], [])


async def test_oversized_output_shows_marked_tails_and_replays(tmp_path):
    from rio.agent.store import encode

    path = tmp_path / "overflow.sqlite3"
    with Store(path) as store:
        session = Session.create(store, "goal", kinds=KINDS)
        accept(session, cmd("a"), cmd("b"))
        for i in (1, 2):
            session.authorize(i)
            session.append_output(i, {"stdout": "x" * 60_000, "stderr": "y" * 60_000})
            session.finish(i, {"type": "success"})
        executions = copy.deepcopy(session.data["executions"])
        turn = prepare(session)
        assert session.data["executions"] == executions
        state = turn["state"]
        assert len(encode(state).encode()) <= session.limits.context_bytes
        for key in ("1", "2"):
            cell, recorded = state["runtime"]["cells"][key], executions[key]["output"]
            assert set(cell["omitted"]) == {"stdout", "stderr"}
            for name, text in recorded.items():
                assert text.endswith(cell["output"][name])
                assert cell["omitted"][name] == len(text) - len(cell["output"][name]) > 0
            # The evidence event keeps the recorded output.
            seq = int(cell["result"]["evidence_refs"][0].rsplit(":", 1)[1])
            event = store.events(seq - 1)[0]
            assert event["changes"]["executions"][key]["output"] == recorded
        data = copy.deepcopy(store.data)
    with Store(path) as store:
        assert store.data == data
        session = Session(store, KINDS)
        assert prepare(session) == turn
        provider = FakeProvider([response([conclude()])])
        assert await run(Owner(session), provider, "fake")
        assert len(provider.calls) == 1


def test_many_large_outputs_share_context(session):
    from rio.agent.store import encode

    accept(session, *(cmd(str(i)) for i in range(12)))
    for i in range(1, 13):
        session.authorize(i)
        text = "small" if i == 12 else "line\n" * 20_000
        session.append_output(i, {"stdout": text})
        session.finish(i, {"type": "success"})
    state = prepare(session)["state"]
    assert len(encode(state).encode()) <= session.limits.context_bytes
    cells = state["runtime"]["cells"]
    assert cells["12"]["output"] == {"stdout": "small"}
    assert "omitted" not in cells["12"]
    # Equal tails of the recorded 50 KB; JSON escapes count toward the budget.
    tails = {cells[str(i)]["output"]["stdout"] for i in range(1, 12)}
    assert len(tails) == 1
    tail = tails.pop()
    assert 10_000 < len(tail) < 20_000
    for i in range(1, 12):
        assert cells[str(i)]["omitted"] == {"stdout": session.limits.output_bytes - len(tail)}


def test_output_shares_and_json_tails_respect_budget():
    from rio.agent.session import json_tail, shares

    assert shares([5, 50, 100], 65) == [5, 30, 30]
    assert shares([5, 50], 100) == [5, 50]
    assert shares([7, 7], 0) == [0, 0]
    assert json_tail("ab\n\n", 3) == "\n"
    assert json_tail("\n\n", 1) == ""
    assert json_tail("é", 1) == ""


def test_overflowing_records_keep_outstanding_work_and_notify(session):
    from rio.agent.store import encode

    accept(session, *(cmd(str(i)) for i in range(20)))
    for i in range(1, 19):
        session.authorize(i)
        session.finish(i, {"type": "success"})
    session.authorize(19)
    session.finish(19, {"type": "UnknownExecution"})
    session.limits = replace(session.limits, context_bytes=4096, output_bytes=1024)
    turn = prepare(session)
    state = turn["state"]
    assert len(encode(state).encode()) <= 4096
    assert list(state["runtime"]["cells"]) == ["19", "20"]
    notice = state["runtime"]["inbox"][0]
    assert notice["kind"] == "context"
    assert notice["payload"]["cell_ids"] == list(range(1, 19))
    assert notice["id"] not in turn["delivered"]
    session.accept(turn["id"], [remove(i) for i in range(1, 19)])
    state = prepare(session)["state"]
    assert all(m["kind"] != "context" for m in state["runtime"]["inbox"])
    assert list(state["runtime"]["cells"]) == ["19", "20"]


async def test_saved_complete_response_is_accepted_without_another_provider_call(session):
    turn = prepare(session)
    session.start_attempt(turn["id"])
    operations = [conclude()]
    message = response(operations)
    session.record_response(turn["id"], message.model_dump(mode="json"), 12)
    provider = FakeProvider([])
    assert await run(ProcessOwner(session), provider, "fake")
    assert not provider.calls
    assert session.meta["usage"]["tokens"] == 12


async def test_lost_and_partial_model_responses_are_bounded(session, monkeypatch):
    import rio.agent.runner as runner

    monkeypatch.setattr(runner.time, "time", lambda: 1e20)
    provider = FakeProvider([])
    assert not await run(ProcessOwner(session), provider, "fake")
    assert len(provider.calls) == session.limits.provider_attempts
    assert not session.data["cells"]
    assert session.meta["usage"]["unknown_usage"] == session.limits.provider_attempts
    assert session.meta["terminal"]["reason"] == (
        "Provider retry budget exhausted: FakeProvider has no scripted response"
    )


def provider_error(message="overloaded", **details):
    error = AssistantMessage(
        stop_reason="error",
        error_message=message,
        diagnostics=[{"type": "provider_error", "details": details}],
    )
    return error


class Clock:
    """Fake time that jumps to each awaited deadline instead of sleeping."""

    def __init__(self, monkeypatch):
        import rio.agent.runner as runner

        self.now = 1000.0
        self.waits = []
        wait = Owner.wait
        monkeypatch.setattr(runner.time, "time", lambda: self.now)

        async def jump(owner, until=math.inf, request=None):
            if request is None and until != math.inf:
                self.waits.append(until - self.now)
                self.now = max(self.now, until)
            await wait(owner, until, request)

        monkeypatch.setattr(Owner, "wait", jump)


class Raising(FakeProvider):
    def __init__(self, streams, failures):
        super().__init__(streams)
        self.failures = failures

    async def complete(self, **kwargs):
        if self.failures:
            self.failures -= 1
            self.calls.append(tuple(kwargs.values()))
            raise ConnectionError("connection reset")
        return await super().complete(**kwargs)


async def test_provider_failures_back_off_outside_invalid_budget(session, monkeypatch):
    clock = Clock(monkeypatch)
    provider = Raising(
        [
            provider_error(status_code=529),
            provider_error("stream ended"),
            response([{"op": "invalid"}]),
            response([conclude()]),
        ],
        failures=2,
    )
    assert await run(Owner(session), provider, "fake")
    assert len(provider.calls) == 6
    assert [round(w) for w in clock.waits] == [2, 4, 8, 16, 2]
    events = session.store.events()
    failed = [e for e in events if e["type"] == "provider_failed"]
    assert [next(iter(e["changes"]["turns"].values()))["provider_error"] for e in failed] == [
        "connection reset",
        "connection reset",
        "overloaded",
        "stream ended",
    ]
    assert sum(e["type"] == "patch_rejected" for e in events) == 1
    texts = [[m.content for m in call[2][1:]] for call in provider.calls]
    assert texts == [[], [], [], [], [], ["Rejected step: Invalid ops[0]. " + USAGE]]
    turn = next(iter(session.data["turns"].values()))
    assert turn["attempts"] == 2
    assert turn["provider_failures"] == 4
    assert session.meta["usage"]["attempts"] == 6
    # Fake responses report no tokens, and failed requests never do.
    assert session.meta["usage"]["unknown_usage"] == 6


async def test_invalid_model_output_still_uses_invalid_budget(session, monkeypatch):
    Clock(monkeypatch)
    text = AssistantMessage(content="done")
    provider = FakeProvider([text, response([{"op": "invalid"}]), text])
    assert not await run(Owner(session), provider, "fake")
    assert len(provider.calls) == session.limits.invalid_attempts
    assert session.meta["terminal"]["reason"] == "Model retry budget exhausted"
    assert not any(e["type"] == "provider_failed" for e in session.store.events())


@pytest.mark.parametrize("status", [400, 401, 404])
async def test_final_provider_errors_fail_promptly(session, monkeypatch, status):
    clock = Clock(monkeypatch)
    provider = FakeProvider([provider_error("bad key", status_code=status)])
    assert not await run(Owner(session), provider, "fake")
    assert len(provider.calls) == 1
    assert not clock.waits
    assert session.meta["terminal"]["reason"] == "Provider request failed: bad key"


async def test_provider_backoff_resumes_after_restart(tmp_path, monkeypatch):
    clock = Clock(monkeypatch)
    path = tmp_path / "backoff.db"
    with Store(path) as store:
        session = Session.create(store, "goal")
        turn = session.prepare_turn()
        session.start_attempt(turn["id"])
        session.provider_failed(turn["id"], "rate limited")
        session.start_attempt(turn["id"])
        session.provider_failed(turn["id"], "rate limited")
    clock.now += 1
    with Store(path) as store:
        session = Session(store)
        provider = FakeProvider([response([conclude()])])
        assert await run(Owner(session), provider, "fake")
        assert [round(w) for w in clock.waits] == [3]
        assert len(provider.calls[0][2]) == 1
        turn = session.data["turns"][turn["id"]]
        assert (turn["attempts"], turn["provider_failures"]) == (1, 2)
        assert turn["error"] is None


def test_success_receipt_and_ending_share_a_transaction(session):
    accept(session, conclude())
    session.end("success", "done", control_id="1")
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
        async with ProcessOwner(session) as owner:
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
        conclude(),
    ]
    message = response(operations)
    message.api = "provider-without-usage"
    assert await run(ProcessOwner(session), FakeProvider([message]), "fake")
    assert session.meta["usage"]["unknown_usage"] == 1


def test_terminal_run_cannot_prepare_an_unaccepted_request(session):
    prepare(session)
    session.end("failure", "stopped")
    with pytest.raises(RunStopped, match="Terminal"):
        prepare(session)


def test_runtime_selector_is_removed():
    from rio.cli import build_parser

    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(["run", "--runtime", "notebook", "goal"])
    assert error.value.code == 2


@pytest.mark.parametrize("prefix", RESERVED)
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
                    conclude(),
                ]
            ),
        ]
    )
    provider.aclose = AsyncMock()
    monkeypatch.setattr(cli, "create_model_provider", lambda *a, **kw: provider)
    ok, session_id = await cli.run_persistent_session(
        "write a file",
        cwd=tmp_path,
        output_mode=output_mode,
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
        output_mode=output_mode,
    ) == (True, session_id)
    assert capsys.readouterr().out == transcript
    assert len(provider.calls) == 2
    assert (tmp_path / "effect").read_text() == "changed after completion"
    assert provider.aclose.await_count == 1


def test_idle_decision_and_budget_survive_reopen(session):
    from rio.agent.runner import _idle_action

    owner = ProcessOwner(session)
    assert _idle_action(owner) == "ready"
    accept(session)
    for _ in range(session.limits.invalid_attempts):
        assert _idle_action(owner) == "invalid"
        session.invalid_idle()
        assert _idle_action(owner) == "ready"
        accept(session)
    assert _idle_action(ProcessOwner(Session(session.store, KINDS))) == "fail"
    accept(session, code())
    session.request_work()
    assert session.meta["idle"] == 0
    assert _idle_action(owner) == "wait"


@pytest.mark.parametrize("source", ["worker", "timer", "external"])
def test_idle_waits_for_wakeup_sources(session, source):
    from rio.agent.runner import _idle_action

    accept(session)
    owner = ProcessOwner(session)
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
    session.authorize(1)
    session.finish(1, {"type": result})
    assert _idle_action(ProcessOwner(session)) == ("wait" if result == "success" else "ready")
    session.receive("new", "external", {})
    assert _idle_action(ProcessOwner(session)) == "ready"


def test_external_wakeup_policy_survives_reopen(tmp_path):
    path = tmp_path / "wakeup.db"
    with Store(path) as store:
        Session.create(store, "goal", external_wakeup=True)
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
    session.authorize(1)
    session.finish(1, {"type": "UnknownExecution"})
    turn = prepare(session)
    before = session.store.sequence
    with pytest.raises(InvalidPatch, match="recorded observation"):
        session.accept(turn["id"], [control("resolution", target_cell_id=1, evidence_refs=[ref])])
    assert session.store.sequence == before


@pytest.mark.parametrize("result", ["success", "failure"])
@pytest.mark.parametrize("output_mode", ["human", "json"])
async def test_terminal_cli_resume_needs_no_provider_or_project(
    tmp_path, monkeypatch, capsys, result, output_mode
):
    import rio.coding.owner as owner_module
    from rio.cli import run as cli
    from rio.coding.paths import RioPaths

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
            kinds=KINDS,
            owner_config={"cwd": str(project)},
            model_config={"provider": "removed", "model": "retired"},
        )
        accept(session, code())
        session.authorize(1)
        session.record("workers", "cell:1", {"token": "leftover"})
        session.finish(1, {"type": "success"})
        session.end(result, "done")
    project.rmdir()
    stopped = []
    monkeypatch.setattr(owner_module, "stop_worker", lambda identity: stopped.append(identity))
    assert await cli.run_persistent_session("", resume=session_id, output_mode=output_mode) == (
        result == "success",
        session_id,
    )
    assert stopped == [{"token": "leftover"}]
    transcript = capsys.readouterr().out
    if output_mode == "json":
        events = [json.loads(line) for line in transcript.splitlines()]
        assert any(e["type"] == "owner_record" for e in events)
        assert "durability_metrics" not in {e["type"] for e in events}
    else:
        assert "done" in transcript
    with Store(path) as store:
        assert store.data["workers"]["cell:1"]["stopped"]
        assert store.data["meta"]["terminal"]["result"] == result


async def test_replay_requires_posix_before_cleanup(session, monkeypatch):
    session.end("failure", "stopped")
    monkeypatch.setattr("rio.coding.owner.os.name", "nt")
    with pytest.raises(RecoveryFailure, match="POSIX"):
        await replay(ProcessOwner(session), lambda event: None)


async def test_terminal_replay_propagates_cleanup_failure(session, monkeypatch):
    import rio.coding.owner as owner_module
    from rio.agent.runner import replay

    accept(session, code())
    session.authorize(1)
    session.record("workers", "cell:1", {"token": "leftover"})
    session.finish(1, {"type": "success"})
    session.end("success", "done")

    def fail(identity):
        raise RecoveryFailure("cannot stop worker")

    monkeypatch.setattr(owner_module, "stop_worker", fail)
    published = []
    with pytest.raises(RecoveryFailure, match="cannot stop"):
        await replay(ProcessOwner(session), published.append)
    assert not published
    assert not session.data["workers"]["cell:1"].get("stopped")


async def test_replay_without_workers_writes_nothing(session):
    from rio.agent.runner import replay

    session.end("success", "done")
    seq = session.store.sequence
    published = []
    assert await replay(ProcessOwner(session), published.append)
    assert session.store.sequence == seq
    assert published[-1]["type"] == "run_ended"


async def test_replay_rejects_active_session(session):
    from rio.agent.runner import replay

    with pytest.raises(ValueError, match="terminal"):
        await replay(ProcessOwner(session))


def test_turn_records_do_not_implicitly_update_state(session):
    before = copy.deepcopy(session.data)
    turn_state = {**session.state, "revision": 42}
    changes = {"turns": {"turn": {"state": turn_state}}}
    reduced = reduce_event(session.data, changes)
    assert reduced["meta"] == before["meta"]
    assert reduced["turns"]["turn"]["state"] == turn_state

    changes["meta"] = {"state": {"revision": 7}}
    reduced = reduce_event(session.data, changes)
    assert reduced["meta"]["state"] == {**before["meta"]["state"], "revision": 7}
    assert session.data == before


def test_prepared_turn_state_survives_reopen(tmp_path):
    path = tmp_path / "session.sqlite3"
    with Store(path) as store:
        session = Session.create(store, "goal")
        session.receive("external:1", "external", {"fact": 1})
        turn = session.prepare_turn()
        expected = copy.deepcopy(session.data)
    with Store(path) as store:
        session = Session(store)
        assert session.data == expected
        assert session.state == turn["state"]
        sequence = store.sequence
        assert session.prepare_turn() == turn
        assert store.sequence == sequence
        session.accept(turn["id"], [add(note())])
        assert store.data["inbox"]["external:1"]["consumed"]


def test_turn_events_store_explicit_state_and_replay_deltas(session):
    turn = session.prepare_turn()
    turn_id = turn["id"]
    prepared = session.store.events()[-1]
    assert prepared["changes"]["meta"]["state"] == turn["state"] == session.state
    session.start_attempt(turn_id)
    session.record_response(turn_id, {"text": "first"}, 5)
    session.reject(turn_id, "retry")
    session.start_attempt(turn_id)
    session.record_response(turn_id, {"text": "second"}, 7)
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


def test_output_journal_grows_linearly_and_replays(session):
    accept(session, cmd("true"))
    session.authorize(1)
    sizes = []
    for _ in range(20):
        before = session.store.journal_bytes
        session.append_output(1, {"stdout": "x" * 100, "stderr": "warning\n"})
        sizes.append(session.store.journal_bytes - before)
    assert len(set(sizes)) == 1
    assert max(sizes) < 250
    expected = copy.deepcopy(session.data)
    session.store.close()
    with Store(session.store.path) as reopened:
        assert reopened.data == expected
        assert reopened.data["executions"]["1"]["output"]["stdout"] == "x" * 2000


@pytest.mark.parametrize("failure", ["timeout", "oserror"])
async def test_worker_failure_flushes_last_output_and_cleans_logs(session, monkeypatch, failure):
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
    async with ProcessOwner(session) as owner:
        await settle(owner)
        assert not list(Path(owner.temporary.name).glob("*.log"))
        assert owner.log_budget == LOG_BYTES
    execution = session.data["executions"]["1"]
    assert execution["output"] == {"stdout": "last stdout\n", "stderr": "last stderr\n"}
    assert execution["result"]["type"] == "UnknownExecution"


async def test_cells_preserve_existing_pythonpath(session, monkeypatch):
    module_dir = Path(session.meta["owner_config"]["cwd"]) / "modules"
    module_dir.mkdir()
    (module_dir / "user_module.py").write_text("value = 42\n")
    monkeypatch.setenv("PYTHONPATH", str(module_dir))
    accept(session, cmd(sys.executable, "-c", "import user_module; print(user_module.value)"))
    async with ProcessOwner(session) as owner:
        await settle(owner)
    assert session.data["executions"]["1"]["output"]["stdout"] == "42\n"


def test_output_chunks_replay_rolling_utf8_tails(session):
    session.limits = replace(session.limits, output_bytes=7)
    accept(session, cmd("true"))
    session.authorize(1)
    for text in ("old text", "é終!", "é終!"):
        session.append_output(1, {"stdout": text, "stderr": text})
    expected = copy.deepcopy(session.data)
    assert expected["executions"]["1"]["output"] == {"stdout": "!é終!", "stderr": "!é終!"}
    assert expected["executions"]["1"]["truncated"]
    session.store.close()
    with Store(session.store.path) as reopened:
        assert reopened.data == expected
