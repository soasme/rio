"""Render a completed SKILL.state run from its journal.

Ported from tau's `transcript.py`, and redesigned rather than translated.
tau's `TranscriptRenderer` replayed a *live* stream of character deltas and
tool-call blocks as an assistant turn happened. rio's steps have no live
text stream to replay -- reasoning is discarded, not streamed -- and a step
only becomes interesting once the whole cycle (proposal, validation, commit,
action) has already landed in the journal. So unlike tau's version, this
module does not implement `EventRenderer`: it takes the finished
`SessionEntry` list a `rio.coding.session_store.SessionStorage` produced and
renders it after the fact, ending with the execution state the run settled
on -- the session's entire memory of what happened.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

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
    resume_state,
)

__all__ = ["render_completed_run", "render_final_state", "render_run_steps"]

_OBSERVATION_CHARS = 400


def render_run_steps(entries: Sequence[SessionEntry]) -> str:
    """Render a step-by-step plain-text account of a completed run."""
    lines: list[str] = []
    for entry in entries:
        lines.extend(_render_entry(entry))
    return "\n".join(lines)


def render_final_state(entries: Sequence[SessionEntry]) -> str:
    """Render the execution state the run settled on, as formatted JSON.

    This is the state at the active branch's newest snapshot -- see
    `rio.coding.session_store.resume_state` -- not a replay of anything: a
    SKILL.state journal always stores the full state a step produced, never
    just the message that produced it.
    """
    return json.dumps(resume_state(entries), indent=2, sort_keys=True)


def render_completed_run(entries: Sequence[SessionEntry]) -> str:
    """Render a run's step account followed by the final execution state."""
    entries = list(entries)
    return render_run_steps(entries) + "\n\nFinal execution state:\n" + render_final_state(entries)


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
        return [f"# state reset{reason}"]
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
    if entry.state_delta:
        lines.append(f"   state delta: {_render_json(entry.state_delta)}")
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
