"""Tests for the `%cell` magic: reading back a cell from the session journal."""

from __future__ import annotations

import pytest

from rio.coding.cell_magic import recall, render_cell
from rio.coding.session_store import StepEntry


def step(parent: str | None, entry_id: str, patch: list) -> StepEntry:
    return StepEntry(id=entry_id, parent_id=parent, step=0, patch=patch)


def cell(cell_id: str, cell_type: str, source: str, **extra) -> dict:
    return {"id": cell_id, "cell_type": cell_type, "metadata": {}, "source": source, **extra}


PROBE = cell(
    "probe",
    "code",
    "print(6 * 7)",
    outputs=[{"output_type": "stream", "name": "stdout", "text": "42\n"}],
    execution_count=1,
)


def test_a_removed_cell_is_read_back_with_its_outputs():
    entries = [
        step(None, "a", [{"op": "add", "path": "/cells/-", "value": PROBE}]),
        step("a", "b", [{"op": "remove", "path": "/cells/0"}]),
    ]

    assert recall(entries, "probe") == PROBE
    assert render_cell(PROBE) == "[code cell probe]\nprint(6 * 7)\n42"


def test_a_summary_under_the_same_id_yields_the_cell_it_replaced():
    summary = cell("probe", "markdown", "6 * 7 is 42 (cell probe)")
    entries = [
        step(None, "a", [{"op": "add", "path": "/cells/-", "value": PROBE}]),
        step("a", "b", [{"op": "replace", "path": "/cells/0", "value": summary}]),
    ]

    assert recall(entries, "probe") == PROBE


def test_a_cell_from_an_abandoned_branch_is_not_found():
    entries = [
        step(None, "a", []),
        step("a", "b", [{"op": "add", "path": "/cells/-", "value": PROBE}]),
        step("a", "c", []),
    ]

    with pytest.raises(KeyError):
        recall(entries, "probe")
