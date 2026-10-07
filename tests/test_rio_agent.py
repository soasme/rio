"""Durability boundaries and recovery behavior, using real SQLite and uv processes."""

import asyncio
import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from rio.agent import InvalidPatch, Limits, Session, StorageFailure, Store
from rio.agent.owner import Owner, RecoveryFailure, stop_worker
from rio.agent.prompt import SCHEMA, SYSTEM
from rio.agent.runner import run
from rio.agent.store import reduce_event
from rio.ai import FakeProvider
from rio.ai.messages import AssistantMessage, ToolCall
from rio.ai.provider_events import AssistantDoneEvent


@pytest.fixture
def session(tmp_path):
    with Store(tmp_path / "session.sqlite3") as store:
        yield Session.create(store, "fix the code", tmp_path)


def note(n, text="remember", **extra):
    return {"id": n, "previous_id": None, "kind": "note", "text": text, **extra}


def code(n, source="print('hello')", **extra):
    return {
        "id": n,
        "previous_id": None,
        "kind": "code",
        "runtime": "python",
        "source": "# /// script\n# dependencies = []\n# ///\n" + source + "\n",
        **extra,
    }


def prepare(session):
    return session.prepare_turn(SYSTEM, SCHEMA)


def patch(turn, *operations):
    return [{"op": "test", "path": "/revision", "value": turn["state"]["revision"]}, *operations]


def append(cell):
    return {"op": "add", "path": "/cells/-", "value": cell}


def accept(session, *cells):
    turn = prepare(session)
    return session.accept(turn["id"], patch(turn, *(append(c) for c in cells)))


async def settle(owner):
    async with asyncio.timeout(15):
        while owner.session.pending():
            await owner.tick()
            await asyncio.sleep(0.02)
        await owner.tick()


def test_sqlite_full_wal_and_exclusive_owner(session):
    assert session.store.db.execute("PRAGMA synchronous").fetchone() == (2,)
    assert session.store.db.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    with pytest.raises(StorageFailure, match="owner"):
        Store(session.store.path)


def test_atomic_admission_and_linear_history(session):
    accept(session, note(1), note(2))
    turn = prepare(session)
    session.accept(
        turn["id"],
        patch(turn, {"op": "replace", "path": "/cells/0", "value": note(3, "new", previous_id=1)}),
    )
    assert [c["id"] for c in session.state["cells"]] == [3, 2]
    assert session.data["cells"]["1"] == note(1)
    before = copy.deepcopy(session.data)
    turn = prepare(session)
    before = copy.deepcopy(session.data)
    with pytest.raises(InvalidPatch):
        session.accept(
            turn["id"],
            patch(
                turn, append(note(4)), {"op": "replace", "path": "/cells/0/text", "value": "bad"}
            ),
        )
    assert session.data == before


@pytest.mark.parametrize(
    "operation",
    [
        {"op": "replace", "path": "/goal", "value": "weakened"},
        {"op": "replace", "path": "/runtime", "value": {}},
        {"op": "replace", "path": "/revision", "value": 99},
        {"op": "move", "path": "/cells/1", "from": "/cells/0"},
        {"op": "copy", "path": "/cells/-", "from": "/cells/0"},
        {"op": "add", "path": "/cells/0", "value": note(2)},
        {"op": "replace", "path": "/cells/0", "value": note(2, previous_id=77)},
    ],
)
def test_restricted_patch(session, operation):
    accept(session, note(1))
    turn = prepare(session)
    with pytest.raises(InvalidPatch):
        session.accept(turn["id"], patch(turn, operation))


def test_deduplication_and_conflicting_turn(session):
    turn = prepare(session)
    operations = patch(turn, append(code(1)))
    receipt = session.accept(turn["id"], operations)
    seq = session.store.sequence
    assert session.accept(turn["id"], operations) == receipt
    assert session.store.sequence == seq
    assert len(session.data["executions"]) == 1
    with pytest.raises(InvalidPatch, match="Conflicting"):
        session.accept(turn["id"], patch(turn))


def test_busy_replace_rejected_removal_keeps_work(session):
    accept(session, code(1))
    turn = prepare(session)
    with pytest.raises(InvalidPatch, match="queued"):
        session.accept(
            turn["id"],
            patch(turn, {"op": "replace", "path": "/cells/0", "value": code(2, previous_id=1)}),
        )
    session.accept(turn["id"], patch(turn, {"op": "remove", "path": "/cells/0"}))
    assert not session.state["cells"]
    assert session.pending()[0]["cell_id"] == 1
    assert "1" in prepare(session)["state"]["runtime"]["cells"]


def test_unchanged_successor_requests_new_execution(session):
    accept(session, code(1))
    session.finish(1, {"type": "success"})
    turn = prepare(session)
    session.accept(
        turn["id"],
        patch(turn, {"op": "replace", "path": "/cells/0", "value": code(2, previous_id=1)}),
    )
    assert session.data["executions"]["2"]["phase"] == "queued"
    assert "result" not in session.data["executions"]["2"]


def test_fixed_snapshot_and_unrelated_response_cannot_clear_pause(session):
    accept(session, code(1), code(2))
    turn = prepare(session)
    session.finish(1, {"type": "error"})
    assert prepare(session) == turn
    session.accept(turn["id"], patch(turn))
    assert session.meta["pause"] == "execution:1"
    turn = prepare(session)
    assert turn["delivered"] == ["execution:1"]
    session.accept(turn["id"], patch(turn))
    assert session.meta["pause"] is None


def test_inbox_deduplication_and_acknowledgment(session):
    session.receive("external:1", "external", {"fact": 1})
    session.receive("external:1", "external", {"fact": 1})
    with pytest.raises(ValueError, match="Conflicting"):
        session.receive("external:1", "external", {"fact": 2})
    turn = prepare(session)
    with pytest.raises(InvalidPatch):
        session.accept(turn["id"], [])
    assert not session.data["inbox"]["external:1"]["consumed"]
    session.accept(turn["id"], patch(turn))
    assert session.data["inbox"]["external:1"]["consumed"]


def test_rebuild_is_pure_and_preserves_usage(session, monkeypatch):
    accept(session, code(1))
    session.finish(1, {"type": "UnknownExecution", "worker_stopped": True})
    turn = prepare(session)
    session.attempt(turn["id"])
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


async def test_serial_uv_runs_and_independent_variables(session):
    accept(
        session,
        code(1, "from pathlib import Path\nx = 7\nPath('order').write_text('1')"),
        code(
            2,
            "from pathlib import Path\nassert 'x' not in globals()\n"
            "p=Path('order')\np.write_text(p.read_text()+'2')\nprint('ok')",
        ),
    )
    async with Owner(session) as owner:
        await settle(owner)
    assert (Path(session.meta["cwd"]) / "order").read_text() == "12"
    assert session.data["executions"]["2"]["output"] == "ok\n"
    assert all(e["result"]["type"] == "success" for e in session.data["executions"].values())


async def test_error_pauses_queue_then_cancel_before_dispatch(session):
    accept(session, code(1, "raise RuntimeError('bad')"), code(2, "assert False"))
    async with Owner(session) as owner:
        async with asyncio.timeout(15):
            while session.meta["pause"] is None:
                await owner.tick()
                await asyncio.sleep(0.02)
        assert session.data["executions"]["2"]["phase"] == "queued"
        accept(session, note(3, role="cancel", target_cell_id=2))
        await owner.tick()
    assert session.data["executions"]["2"]["result"]["type"] == "cancelled"
    assert "worker" not in session.data["executions"]["2"]


async def test_started_recovery_never_reexecutes(session):
    accept(session, code(1, "from pathlib import Path\nPath('effect').touch()"))
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
    accept(session, code(1))
    session.finish(1, {"type": "UnknownExecution"})
    original = copy.deepcopy(session.data["executions"]["1"])
    turn = prepare(session)
    with pytest.raises(InvalidPatch):
        session.accept(
            turn["id"],
            patch(turn, append(note(2, role="resolution", target_cell_id=1, evidence_refs=[]))),
        )
    session.receive("inspection", "external", {"file_hash": "verified"})
    ref = f"session:event:{session.store.sequence}"
    session.accept(
        turn["id"],
        patch(turn, append(note(2, role="resolution", target_cell_id=1, evidence_refs=[ref]))),
    )
    async with Owner(session):
        assert not session.unresolved()
    assert session.data["executions"]["1"] == original
    assert prepare(session)["state"]["runtime"]["cells"]["1"]["resolved_by"] == 2


async def test_success_rejected_for_new_input_and_failure_is_terminal(session):
    turn = prepare(session)
    session.receive("arrived", "external", {"updated": True})
    session.accept(turn["id"], patch(turn, append(note(1, role="conclusion", result="success"))))
    async with Owner(session) as owner:
        assert not session.meta["terminal"]
        accept(session, note(2, role="conclusion", result="failure"))
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
            1,
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
        accept(session, note(2, role="cancel", target_cell_id=1))
        await owner.controls()
    import psutil

    assert (
        not psutil.pid_exists(child_pid)
        or psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE
    )
    assert session.data["executions"]["1"]["result"]["type"] == "UnknownExecution"


def response(operations):
    message = AssistantMessage(
        content=[ToolCall(id="call", name="step", arguments={"patch": operations})]
    )
    return [AssistantDoneEvent(reason="toolUse", message=message)]


async def test_public_loop_finishes_and_reopens_without_model_call(session):
    provider = FakeProvider(
        [
            response([{"op": "test", "path": "/revision", "value": 1}, append(code(1))]),
            response(
                [
                    {"op": "test", "path": "/revision", "value": 3},
                    append(note(2, "completed", role="conclusion", result="success")),
                ]
            ),
        ]
    )
    published = []
    assert await run(session, provider, "fake", published.append)
    assert len(provider.calls) == 2
    assert any(e["type"] == "run_ended" for e in published)
    assert await run(session, FakeProvider([]), "fake")


async def test_uv_timer_api_acknowledges_committed_registration(session):
    accept(session, code(1, "from rio.agent.api import timer\ntimer('wake:1', 0, {'done':True})"))
    async with Owner(session) as owner:
        await settle(owner)
    assert session.data["timers"]["wake:1"]["fired"]
    assert session.data["executions"]["1"]["result"]["type"] == "success"


def test_output_limit_is_explicit(session):
    accept(session, code(1))
    session.authorize(1, {"token": "none"})
    session.output(1, "x" * 20000)
    execution = session.data["executions"]["1"]
    assert len(execution["output"].encode()) == session.limits.output_bytes
    assert execution["truncated"]


async def test_required_validator_blocks_success(tmp_path):
    with Store(tmp_path / "s.db") as store:
        session = Session.create(
            store, "goal", tmp_path, validators=((sys.executable, "-c", "raise SystemExit(1)"),)
        )
        accept(session, note(1, role="conclusion", result="success"))
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
        session = Session.create(store, "goal", tmp_path)
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
            session.accept(turn["id"], patch(turn, append(code(1))))
        assert store.failed and store.data == {}
        with pytest.raises(StorageFailure):
            store.events()
    with Store(path) as store:
        session = Session(store)
        receipt = session.accept(turn["id"], patch(turn, append(code(1))))
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
from rio.agent import Session, Store
from rio.agent.owner import Owner
from rio.agent.prompt import SYSTEM, SCHEMA
async def main():
    root=Path(sys.argv[1]); boundary=sys.argv[2]
    with Store(root/'crash.db') as store:
        s=Session.create(store, 'one effect', root)
        t=s.prepare_turn(SYSTEM, SCHEMA)
        s.accept(t['id'], [{'op':'test','path':'/revision','value':1},
            {'op':'add','path':'/cells/-','value':{'id':1,'previous_id':None,
             'kind':'code','runtime':'python','source':
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
            session = Session(store)
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
    from dataclasses import replace

    from rio.agent import runner

    monkeypatch.setattr(runner.time, "time", lambda: 1e20)
    with Store(tmp_path / "budget.db") as store:
        session = Session.create(store, "goal", tmp_path, limits=replace(Limits(), rounds=2))
        provider = FakeProvider(
            [
                response([{"op": "test", "path": "/revision", "value": 1}]),
                response([{"op": "test", "path": "/revision", "value": 3}]),
            ]
        )
        assert not await run(session, provider, "fake")
        assert session.meta["usage"]["rounds"] == 2
    with Store(tmp_path / "budget.db") as store:
        session = Session(store)
        assert not await run(session, FakeProvider([]), "fake")
        assert session.meta["usage"]["rounds"] == 2


def test_context_reserves_output_and_observation_space(session):
    accept(session, *(code(i) for i in range(1, 20)))
    for i in range(1, 20):
        session.authorize(i, {"token": "none"})
        session.output(i, "x" * session.limits.output_bytes, "y" * session.limits.output_bytes)
        session.finish(i, {"type": "success"})
    turn = prepare(session)
    assert len(json.dumps(turn["state"]).encode()) < session.limits.context_bytes
    assert turn["state"]["runtime"]["cells"]["1"]["context_truncated"]
    assert len(session.data["executions"]["1"]["output"]) == session.limits.output_bytes
    session.accept(turn["id"], patch(turn))


async def test_saved_complete_response_is_accepted_without_another_provider_call(session):
    turn = prepare(session)
    session.attempt(turn["id"])
    operations = patch(turn, append(note(1, role="conclusion", result="success")))
    message = response(operations)[0].message
    session.response(turn["id"], message.model_dump(mode="json"), 12)
    provider = FakeProvider([])
    assert await run(session, provider, "fake")
    assert not provider.calls
    assert session.meta["usage"]["tokens"] == 12


async def test_lost_and_partial_model_responses_are_bounded(session, monkeypatch):
    from rio.agent import runner

    monkeypatch.setattr(runner.time, "time", lambda: 1e20)
    provider = FakeProvider([])
    assert not await run(session, provider, "fake")
    assert len(provider.calls) == session.limits.invalid_attempts
    assert not session.data["cells"]
    assert session.meta["usage"]["unknown_usage"] == session.limits.invalid_attempts


def test_success_receipt_and_ending_share_a_transaction(session):
    accept(session, note(1, role="conclusion", result="success"))
    session.end("success", "done", note_id="1")
    ending = session.store.events()[-1]
    assert ending["changes"]["meta"]["terminal"]["result"] == "success"
    assert ending["changes"]["controls"]["1"]["handled"]


async def test_uncertain_output_commit_stops_worker_before_more_effects(session, monkeypatch):
    accept(
        session,
        code(
            1,
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

    async def run_session(*args):
        calls.append(args)
        return True, "abc"

    monkeypatch.setattr(run_module, "run_persistent_session", run_session)
    app(["run", "--resume", "abc", "--output", "json"])
    assert calls[0][0] == ""
    assert calls[0][7] == "abc"


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
        {"op": "test", "path": "/revision", "value": 1},
        append(note(1, role="conclusion", result="success")),
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


@pytest.mark.parametrize("output_mode", ["human", "json"])
async def test_cli_persists_sqlite_and_resumes_without_repeating_work(
    tmp_path, monkeypatch, capsys, output_mode
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
    provider = FakeProvider(
        [
            response(
                [
                    {"op": "test", "path": "/revision", "value": 1},
                    append(code(1, "from pathlib import Path\nPath('effect').write_text('once')")),
                ]
            ),
            response(
                [
                    {"op": "test", "path": "/revision", "value": 3},
                    append(note(2, "done", role="conclusion", result="success")),
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
        assert events[-1]["type"] == "durability_metrics"
    else:
        assert "• Cell 1 (code)\n  # /// script" in transcript
        assert "  Path('effect').write_text('once')" in transcript
        assert "• Running Cell 1\n  └ Cell 1: success (exit 0)" in transcript
        assert "• Cell 2 (note), conclusion requested: success\n  done" in transcript
        assert transcript.endswith("• Success: done\n")
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
    assert provider.aclose.await_count == 2
