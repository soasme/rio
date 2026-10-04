"""Render a completed notebook run from its journal.

Takes the finished `SessionEntry` list a `rio.coding.session_store.SessionStorage`
produced and renders it after the fact, ending with the notebook the run settled
on -- the session's entire memory of what happened.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from rio.coding.agent import render_notebook
from rio.coding.session_store import (
    BranchSummaryEntry,
    CustomEntry,
    LabelEntry,
    LeafEntry,
    ModelChangeEntry,
    ReasoningEntry,
    SessionEntry,
    SessionInfoEntry,
    StateResetEntry,
    StepEntry,
    ThinkingLevelChangeEntry,
    TurnEntry,
    ValidationFailureEntry,
    latest_leaf_id,
    notebook_at_entry,
)

__all__ = ["render_completed_run", "render_final_notebook", "render_run_steps"]

_OBSERVATION_CHARS = 400


def render_run_steps(entries: Sequence[SessionEntry]) -> str:
    """Render a step-by-step plain-text account of a completed run."""
    lines: list[str] = []
    for entry in entries:
        lines.extend(_render_entry(entry))
    return "\n".join(lines)


def render_final_notebook(entries: Sequence[SessionEntry]) -> str:
    """Render the notebook at the active branch's tip, as the model sees it."""
    leaf = latest_leaf_id(entries)
    if leaf is None:
        return ""
    return render_notebook(notebook_at_entry(entries, leaf))


def render_completed_run(entries: Sequence[SessionEntry]) -> str:
    """Render a run's step account followed by the final notebook."""
    entries = list(entries)
    return render_run_steps(entries) + "\n\nFinal notebook:\n" + render_final_notebook(entries)


def _render_entry(entry: SessionEntry) -> list[str]:
    if isinstance(entry, TurnEntry):
        return [f"# turn: {_truncate(entry.observation)}"]
    if isinstance(entry, StepEntry):
        return _render_step(entry)
    if isinstance(entry, ValidationFailureEntry):
        return [f"  retry {entry.attempt}: {entry.error}"]
    if isinstance(entry, ReasoningEntry):
        return [f"  reasoning: {_truncate(entry.reasoning)}"]
    if isinstance(entry, StateResetEntry):
        reason = f": {entry.reason}" if entry.reason else ""
        return [f"# notebook reset{reason}"]
    if isinstance(entry, ModelChangeEntry):
        return [f"# model changed to {entry.model}"]
    if isinstance(entry, ThinkingLevelChangeEntry):
        return [f"# thinking level changed to {entry.thinking_level or 'off'}"]
    if isinstance(entry, BranchSummaryEntry):
        return [f"# branch summary: {entry.summary}"]
    if isinstance(entry, LabelEntry):
        return [f"# label: {entry.label}"]
    if isinstance(entry, SessionInfoEntry):
        return [f"# session: {entry.title or entry.cwd or entry.skill or 'untitled'}"]
    if isinstance(entry, CustomEntry):
        return [f"# custom[{entry.namespace}]: {len(entry.data)} field(s)"]
    if isinstance(entry, LeafEntry):
        # A branch-leaf pointer, not session content -- nothing to show, the
        # same precedent tau's session export set for its own `LeafEntry`.
        return []
    return [f"# {entry.type}"]


def _render_step(entry: StepEntry) -> list[str]:
    ran = f", ran cells {entry.cells}" if entry.cells else ""
    lines = [f"## step {entry.step}: {len(entry.patch)} patch operation(s){ran}"]
    lines.append(f"   patch: {_truncate(json.dumps(entry.patch, sort_keys=True))}")
    if entry.reply is not None:
        lines.append(f"   reply: {_truncate(entry.reply)}")
    return lines


def _truncate(text: str, *, limit: int = _OBSERVATION_CHARS) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3].rstrip() + "..."
