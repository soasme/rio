"""Tests for the execution-state journal.

The property that matters here is the one that distinguishes a state journal
from a transcript: resuming or branching reads a single snapshot, and never
depends on how many entries came before it.
"""

from __future__ import annotations

import json

import pytest

from rio_coding.coding_skill import describe_state, plan_progress, touched_files
from rio_coding.session_store import (
    ActionRecord,
    InMemorySessionStorage,
    JsonlSessionStorage,
    LeafEntry,
    SessionInfoEntry,
    SessionJsonlError,
    SessionTreeError,
    StateResetEntry,
    StepEntry,
    TurnEntry,
    ValidationFailureEntry,
    checkpoints,
    entries_from_json_lines,
    entry_state,
    entry_to_json_line,
    latest_leaf_id,
    path_to_entry,
    resume_state,
    state_at_entry,
)


def make_chain(count: int, *, root_parent: str | None = None) -> list[StepEntry]:
    """Return `count` chained steps whose state grows one finding per step."""
    entries: list[StepEntry] = []
    parent = root_parent
    state: dict = {"goal": "g", "findings": {}}
    for index in range(count):
        state = {**state, "findings": {**state["findings"], f"f{index}": f"value {index}"}}
        entry = StepEntry(
            parent_id=parent,
            step=index,
            state_delta={"findings": {f"f{index}": f"value {index}"}},
            state=state,
            action=ActionRecord(name="read", arguments={"path": f"file{index}.py"}),
            observation=f"observation {index}",
        )
        entries.append(entry)
        parent = entry.id
    return entries


class TestSerialization:
    def test_step_entry_round_trips(self) -> None:
        entry = StepEntry(
            step=4,
            state_delta={"cwd": "/tmp", "last_error": None},
            state={"cwd": "/tmp"},
            action=ActionRecord(name="bash", arguments={"command": "ls"}),
            observation="a.py\nb.py",
        )
        [restored] = entries_from_json_lines([entry_to_json_line(entry)])
        assert restored == entry

    def test_blank_lines_are_skipped(self) -> None:
        entry = TurnEntry(observation="hello")
        lines = ["", entry_to_json_line(entry), "   \n", ""]
        assert len(entries_from_json_lines(lines)) == 1

    def test_invalid_line_names_its_line_number(self) -> None:
        with pytest.raises(SessionJsonlError, match="line 2"):
            entries_from_json_lines(['{"type": "turn", "observation": "ok"}', "{not json"])

    def test_unknown_entry_type_is_rejected(self) -> None:
        with pytest.raises(SessionJsonlError):
            entries_from_json_lines(['{"type": "message", "message": {}}'])

    def test_reasoning_is_not_representable_in_the_journal(self) -> None:
        """The journal has nowhere to put discarded reasoning, by construction."""
        line = json.dumps(
            {
                "type": "step",
                "id": "a" * 32,
                "timestamp": 0.0,
                "step": 0,
                "action": {"name": "read", "arguments": {}},
                "reasoning": "I should look at the file",
            }
        )
        with pytest.raises(SessionJsonlError):
            entries_from_json_lines([line])


class TestTree:
    def test_path_to_entry_returns_root_first(self) -> None:
        entries = make_chain(3)
        path = path_to_entry(entries, entries[-1].id)
        assert [e.id for e in path] == [e.id for e in entries]

    def test_cycles_are_rejected(self) -> None:
        first = TurnEntry(observation="a")
        second = TurnEntry(parent_id=first.id, observation="b")
        first.parent_id = second.id
        with pytest.raises(SessionTreeError, match="Cycle"):
            path_to_entry([first, second], second.id)

    def test_duplicate_ids_are_rejected(self) -> None:
        entry = TurnEntry(observation="a")
        with pytest.raises(SessionTreeError, match="Duplicate"):
            path_to_entry([entry, entry], entry.id)

    def test_missing_parent_is_rejected(self) -> None:
        orphan = TurnEntry(parent_id="missing", observation="a")
        with pytest.raises(SessionTreeError, match="Missing"):
            path_to_entry([orphan], orphan.id)

    def test_latest_leaf_prefers_an_explicit_pointer(self) -> None:
        entries = make_chain(3)
        pointer = LeafEntry(entry_id=entries[0].id)
        assert latest_leaf_id([*entries, pointer]) == entries[0].id

    def test_latest_leaf_falls_back_to_the_last_entry(self) -> None:
        entries = make_chain(3)
        assert latest_leaf_id(entries) == entries[-1].id

    def test_latest_leaf_of_an_empty_journal_is_none(self) -> None:
        assert latest_leaf_id([]) is None


class TestStateRecovery:
    def test_resume_reads_the_newest_snapshot(self) -> None:
        entries = make_chain(5)
        state = resume_state(entries)
        assert state == entries[-1].state
        assert len(state["findings"]) == 5

    def test_resume_skips_entries_without_a_snapshot(self) -> None:
        entries = make_chain(2)
        failure = ValidationFailureEntry(
            parent_id=entries[-1].id, step=2, attempt=1, error="bad delta"
        )
        pointer = LeafEntry(entry_id=failure.id)
        assert resume_state([*entries, failure, pointer]) == entries[-1].state

    def test_resume_of_an_empty_journal_is_empty(self) -> None:
        assert resume_state([]) == {}

    def test_state_at_entry_returns_the_carrying_entry(self) -> None:
        entries = make_chain(3)
        state, carrier = state_at_entry(entries, entries[1].id)
        assert carrier is entries[1]
        assert state == entries[1].state

    def test_a_state_reset_shadows_everything_before_it(self) -> None:
        entries = make_chain(4)
        reset = StateResetEntry(
            parent_id=entries[-1].id, state={"goal": "fresh"}, reason="new session"
        )
        assert resume_state([*entries, reset]) == {"goal": "fresh"}

    def test_branching_restores_an_earlier_snapshot_verbatim(self) -> None:
        entries = make_chain(5)
        branched = StepEntry(
            parent_id=entries[1].id,
            step=2,
            state_delta={"goal": "different"},
            state={**entries[1].state, "goal": "different"},
            action=ActionRecord(name="respond", arguments={"message": "done"}),
            terminated=True,
        )
        # The branch inherits entry 1's two findings, not the five on the trunk.
        state = resume_state([*entries, branched, LeafEntry(entry_id=branched.id)])
        assert state["goal"] == "different"
        assert len(state["findings"]) == 2

    def test_checkpoints_are_the_snapshot_carrying_entries(self) -> None:
        entries = make_chain(3)
        failure = ValidationFailureEntry(step=9, attempt=1, error="nope")
        info = SessionInfoEntry(cwd="/repo")
        found = checkpoints([info, *entries, failure])
        assert [e.id for e in found] == [e.id for e in entries]

    def test_entry_state_is_none_for_non_snapshot_entries(self) -> None:
        assert entry_state(ValidationFailureEntry(step=0, attempt=1, error="x")) is None

    def test_recovery_cost_does_not_depend_on_journal_length(self) -> None:
        """A transcript replays every entry; a state journal reads exactly one.

        `state_at_entry` walks back from the leaf and stops at the first
        snapshot, so a 500-step journal costs the same single read as a
        1-step one.
        """
        short = make_chain(1)
        long = make_chain(500)
        _, short_carrier = state_at_entry(short, short[-1].id)
        _, long_carrier = state_at_entry(long, long[-1].id)
        assert short_carrier is short[-1]
        assert long_carrier is long[-1]
        # Both resumed states hold every finding, without any replay step.
        assert len(resume_state(long)["findings"]) == 500


class TestStorage:
    async def test_jsonl_append_and_read(self, tmp_path) -> None:
        storage = JsonlSessionStorage(tmp_path / "session.jsonl")
        entries = make_chain(3)
        for entry in entries:
            await storage.append(entry)
        assert await storage.read_all() == entries

    async def test_jsonl_batch_is_all_or_nothing(self, tmp_path) -> None:
        storage = JsonlSessionStorage(tmp_path / "session.jsonl")
        first = make_chain(2)
        await storage.append_batch(first)
        second = make_chain(2, root_parent=first[-1].id)
        await storage.append_batch(second)
        assert await storage.read_all() == [*first, *second]

    async def test_empty_batch_is_a_no_op(self, tmp_path) -> None:
        storage = JsonlSessionStorage(tmp_path / "session.jsonl")
        await storage.append_batch([])
        assert await storage.read_all() == []

    async def test_missing_file_is_an_empty_session(self, tmp_path) -> None:
        storage = JsonlSessionStorage(tmp_path / "nested" / "session.jsonl")
        assert await storage.read_all() == []

    async def test_stale_temp_file_is_discarded(self, tmp_path) -> None:
        path = tmp_path / "session.jsonl"
        storage = JsonlSessionStorage(path)
        await storage.append(TurnEntry(observation="kept"))
        storage.temp_path.write_bytes(b"garbage that was never committed")
        entries = await storage.read_all()
        assert len(entries) == 1
        assert not storage.temp_path.exists()

    async def test_in_memory_storage_matches(self) -> None:
        storage = InMemorySessionStorage()
        entries = make_chain(2)
        await storage.append_batch(entries)
        await storage.append(LeafEntry(entry_id=entries[-1].id))
        assert len(await storage.read_all()) == 3

    async def test_resume_from_a_written_journal(self, tmp_path) -> None:
        storage = JsonlSessionStorage(tmp_path / "session.jsonl")
        await storage.append_batch(make_chain(6))
        assert len(resume_state(await storage.read_all())["findings"]) == 6


class TestStateAccessors:
    def test_plan_progress(self) -> None:
        state = {
            "plan": [
                {"id": "1", "title": "a", "status": "done"},
                {"id": "2", "title": "b", "status": "in_progress"},
                {"id": "3", "title": "c", "status": "pending"},
            ]
        }
        assert plan_progress(state) == (1, 3)

    def test_plan_progress_tolerates_a_missing_or_malformed_plan(self) -> None:
        assert plan_progress({}) == (0, 0)
        assert plan_progress({"plan": "not a list"}) == (0, 0)

    def test_touched_files_are_sorted(self) -> None:
        state = {"files": {"b.py": {"status": "edited"}, "a.py": {"status": "read"}}}
        assert touched_files(state) == ["a.py", "b.py"]

    def test_describe_state_summarizes_without_dumping(self) -> None:
        state = {
            "plan": [{"id": "1", "title": "a", "status": "done"}],
            "findings": {"x": "y"},
            "files": {"a.py": {}},
            "blockers": ["network is down"],
        }
        summary = describe_state(state)
        assert "plan 1/1" in summary
        assert "1 findings" in summary
        assert "1 files" in summary
        assert "1 blockers" in summary
