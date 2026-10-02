"""Render a completed CLM run from its journal.

Takes the finished `SessionEntry` list a `rio.coding.session_store.SessionStorage`
produced and renders it after the fact, ending with the context the run settled
on -- the session's entire memory of what happened.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from rio.agent import render_context
from rio.ai.types import JSONValue
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
    entry_context,
    latest_leaf_id,
    state_at_entry,
)

__all__ = ["render_completed_run", "render_final_context", "render_run_steps"]

_OBSERVATION_CHARS = 400


def render_run_steps(entries: Sequence[SessionEntry]) -> str:
    """Render a step-by-step plain-text account of a completed run."""
    lines: list[str] = []
    for entry in entries:
        lines.extend(_render_entry(entry))
    return "\n".join(lines)


def render_final_context(entries: Sequence[SessionEntry]) -> str:
    """Render the context at the active branch's newest snapshot, as the model sees it."""
    leaf = latest_leaf_id(entries)
    if leaf is None:
        return ""
    _state, snapshot = state_at_entry(entries, leaf)
    context = entry_context(snapshot) if snapshot is not None else None
    return render_context(context) if context else ""


def render_completed_run(entries: Sequence[SessionEntry]) -> str:
    """Render a run's step account followed by the final context."""
    entries = list(entries)
    return render_run_steps(entries) + "\n\nFinal context:\n" + render_final_context(entries)


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
        return [f"# context reset{reason}"]
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
    lines = [f"## step {entry.step}: {entry.action.name}({_render_json(entry.action.arguments)})"]
    if entry.observation:
        suffix = " (truncated)" if entry.observation_truncated else ""
        lines.append(f"   observation{suffix}: {_truncate(entry.observation)}")
    if entry.terminated:
        lines.append("   (terminated)")
    return lines


def _render_json(value: dict[str, JSONValue]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _truncate(text: str, *, limit: int = _OBSERVATION_CHARS) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3].rstrip() + "..."
