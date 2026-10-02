"""Tests for the notebook journal.

The notebook is never stored anywhere but the journal: a reset holds a whole
notebook and each step holds the JSON Patch it made, so resuming or branching
replays the patches on one branch.
"""

from __future__ import annotations

import json

import pytest

from rio.agent import markdown_cell, new_notebook
from rio.coding.session_store import (
    InMemorySessionStorage,
    JsonlSessionStorage,
    LeafEntry,
    ReasoningEntry,
    SessionInfoEntry,
    SessionJsonlError,
    SessionTreeError,
    StateResetEntry,
    StepEntry,
    TurnEntry,
    ValidationFailureEntry,
    checkpoints,
    entries_from_json_lines,
    entry_to_json_line,
    latest_leaf_id,
    notebook_at_entry,
    path_to_entry,
    resume_notebook,
)


def make_chain(count: int, *, root_parent: str | None = None) -> list[StepEntry]:
    """Return `count` chained steps, each adding one markdown cell."""
    entries: list[StepEntry] = []
    parent = root_parent
    for index in range(count):
        cell = markdown_cell(f"finding {index}")
        entry = StepEntry(
            parent_id=parent,
            step=index,
            patch=[{"op": "add", "path": "/cells/-", "value": cell}],
        )
        entries.append(entry)
        parent = entry.id
    return entries


def sources(notebook: dict) -> list[str]:
    return [cell["source"] for cell in notebook["cells"]]


class TestSerialization:
    def test_step_entry_round_trips(self) -> None:
        entry = StepEntry(
            step=4,
            patch=[{"op": "replace", "path": "/cells/0/source", "value": "x"}],
            cells=[0],
            reply="done",
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

    def test_reasoning_is_not_representable_in_a_step_entry(self) -> None:
        """Reasoning is journaled as its own entry, never folded into a step.

        A step is what a resume reads back, so the schema refuses to carry
        reasoning there even if a writer tries.
        """
        line = json.dumps(
            {
                "type": "step",
                "id": "a" * 32,
                "timestamp": 0.0,
                "step": 0,
                "patch": [],
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


class TestNotebookRecovery:
    def test_resume_replays_every_step_patch(self) -> None:
        entries = make_chain(5)
        assert sources(resume_notebook(entries)) == [f"finding {i}" for i in range(5)]

    def test_resume_skips_entries_without_a_patch(self) -> None:
        entries = make_chain(2)
        failure = ValidationFailureEntry(
            parent_id=entries[-1].id, step=2, attempt=1, error="bad patch"
        )
        pointer = LeafEntry(entry_id=failure.id)
        assert resume_notebook([*entries, failure, pointer]) == resume_notebook(entries)

    def test_resume_of_an_empty_journal_is_an_empty_notebook(self) -> None:
        assert resume_notebook([]) == new_notebook()

    def test_notebook_at_entry_stops_at_that_entry(self) -> None:
        entries = make_chain(3)
        assert sources(notebook_at_entry(entries, entries[1].id)) == ["finding 0", "finding 1"]

    def test_a_state_reset_shadows_everything_before_it(self) -> None:
        entries = make_chain(4)
        fresh = new_notebook()
        fresh["cells"] = [markdown_cell("fresh")]
        reset = StateResetEntry(parent_id=entries[-1].id, notebook=fresh, reason="new session")
        after = make_chain(1, root_parent=reset.id)
        assert sources(resume_notebook([*entries, reset, *after])) == ["fresh", "finding 0"]

    def test_branching_replays_only_its_own_branch(self) -> None:
        entries = make_chain(5)
        [branched] = make_chain(1, root_parent=entries[1].id)
        notebook = resume_notebook([*entries, branched, LeafEntry(entry_id=branched.id)])
        assert sources(notebook) == ["finding 0", "finding 1", "finding 0"]

    def test_checkpoints_are_the_entries_that_change_the_notebook(self) -> None:
        entries = make_chain(3)
        failure = ValidationFailureEntry(step=9, attempt=1, error="nope")
        info = SessionInfoEntry(cwd="/repo")
        found = checkpoints([info, *entries, failure])
        assert [e.id for e in found] == [e.id for e in entries]

    def test_reasoning_entries_are_invisible_to_resume_and_branching(self) -> None:
        """The journal keeps reasoning; the resume path cannot see it."""
        entries = make_chain(3)
        notes = [
            ReasoningEntry(
                parent_id=entry.parent_id, step=entry.step, reasoning=f"why {entry.step}"
            )
            for entry in entries
        ]
        interleaved = [item for pair in zip(notes, entries, strict=True) for item in pair]

        assert resume_notebook(interleaved) == resume_notebook(entries)
        assert [e.id for e in checkpoints(interleaved)] == [e.id for e in entries]
        assert latest_leaf_id(interleaved) == entries[-1].id


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
        assert len(resume_notebook(await storage.read_all())["cells"]) == 6
