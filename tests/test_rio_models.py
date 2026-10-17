"""Validated records preserve the journal wire format and extension fields."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from rio.agent.runner import Owner, run
from rio.agent.session import Session
from rio.agent.shapes import Cell, Execution, HistoryRecord, JournalEvent, Projection, Usage
from rio.agent.state import InvalidPatch
from rio.agent.store import Store, encode
from rio.ai import AssistantMessage, FakeProvider, ToolCall
from rio.coding.api import records
from rio.coding.owner import WorkerIdentity


@pytest.mark.parametrize("invalid", [True, "1", 1.5, None])
def test_record_integer_fields_do_not_coerce(invalid: object) -> None:
    with pytest.raises(ValidationError):
        Usage.model_validate({"turns": invalid, "attempts": 0, "tokens": 0, "unknown_usage": 0})


def test_required_fields_and_unknown_core_fields_are_rejected() -> None:
    with pytest.raises(ValidationError, match="token"):
        WorkerIdentity.model_validate({"pid": 123})
    with pytest.raises(ValidationError, match="unexpected"):
        Usage.model_validate(
            {"turns": 0, "attempts": 0, "tokens": 0, "unknown_usage": 0, "unexpected": True}
        )


def test_missing_and_explicit_null_fields_keep_their_wire_representation() -> None:
    raw = {"cell_id": 1, "phase": "queued", "output": {}, "truncated": False, "accepted": 2}
    missing = Execution.model_validate(raw)
    explicit = Execution.model_validate({**raw, "result": None})
    assert missing.result is explicit.result is None
    assert json.loads(encode(missing)) == raw
    assert json.loads(encode(explicit)) == {**raw, "result": None}


def test_custom_cells_and_owner_records_survive_model_round_trip(tmp_path: Path) -> None:
    cell = Cell.model_validate(
        {"id": 1, "previous_id": None, "kind": "custom", "settings": {"argv": ["true"]}}
    )
    assert json.loads(encode(cell))["settings"] == {"argv": ["true"]}
    with Store(tmp_path / "session.db") as store:
        session = Session.create(store, "goal", model_config={"model": "offline"})
        session.record("workers", "cell:1", {"token": "worker", "stopped": True})
        snapshot = session.data
        assert isinstance(snapshot, Projection)
        assert snapshot.meta.provider_model_config == {"model": "offline"}
        wire = json.loads(encode(snapshot))
        assert wire == store.data
        assert wire["meta"]["state"]["schema"] == session.state.schema_version
        assert "provider_model_config" not in wire["meta"]
        assert Projection.model_validate(wire) == snapshot
    with Store(tmp_path / "session.db") as store:
        assert Session(store).data == snapshot


def test_commit_invalidates_the_validated_projection(tmp_path: Path) -> None:
    with Store(tmp_path / "session.db") as store:
        session = Session.create(store, "goal")
        before = session.data
        session.receive("external:1", "external", {"text": "news"})
        assert not before.inbox
        assert session.data.inbox["external:1"].payload == {"text": "news"}
        assert session.data is not before


def test_events_and_script_history_are_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with Store(tmp_path / "session.db") as store:
        Session.create(store, "goal")
        monkeypatch.setenv("RIO_SESSION_URI", store.path.as_uri() + "?mode=ro")
        event = store.events()[0]
        record = records()[0]
        assert isinstance(event, JournalEvent)
        assert isinstance(record, HistoryRecord)
        assert (record.seq, record.kind, record.changes) == (event.seq, event.type, event.changes)
        assert records(after=event.seq) == []


async def test_runner_accepts_json_encoded_ops(tmp_path: Path) -> None:
    ops = [
        {"op": "control", "control": {"type": "conclusion", "result": "success", "reason": "done"}}
    ]
    message = AssistantMessage(
        content=[ToolCall(id="step", name="step", arguments={"ops": json.dumps(ops)})],
        stop_reason="toolUse",
    )
    with Store(tmp_path / "session.db") as store:
        session = Session.create(store, "goal")
        assert await run(Owner(session), FakeProvider([message]), "offline")


def test_invalid_control_fields_remain_retryable_patch_errors(tmp_path: Path) -> None:
    with Store(tmp_path / "session.db") as store:
        session = Session.create(store, "goal")
        turn = session.prepare_turn()
        sequence = store.sequence
        with pytest.raises(InvalidPatch, match="evidence_refs"):
            session.accept(
                turn.id,
                [
                    {
                        "op": "control",
                        "control": {
                            "type": "resolution",
                            "reason": "checked",
                            "target_cell_id": 1,
                            "evidence_refs": [123],
                        },
                    }
                ],
            )
        assert store.sequence == sequence
