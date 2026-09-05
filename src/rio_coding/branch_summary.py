"""Human-readable summaries of the difference between two execution states.

tau's ``branch_summary.py`` asked a model to read every message on an
abandoned transcript branch and write a structured prose summary, so a
session returning to the trunk had something short to read instead of
replaying the whole branch.

Under SKILL.state there is no transcript to summarize. A branch is a state
checkpoint (see ``rio_coding.session_store.tree.checkpoints``), so
"what happened on that branch" is fully captured by how its final state
differs from the state you are returning to -- a pure, deterministic diff,
not a model call. This module drops the model-calling entry point
(``summarize_branch_messages_with_model``) and every helper that existed
only to serialize a message list for that prompt (conversation formatting,
tool-call rendering, per-message truncation, and the
``BRANCH_SUMMARY_SYSTEM_PROMPT``/``BRANCH_SUMMARY_PROMPT`` constants), and
replaces them with ``summarize_state_diff``.
"""

from __future__ import annotations

from collections.abc import Mapping

from rio_ai.types import JSONValue

__all__ = ["summarize_state_diff"]

_MAX_VALUE_CHARS = 120


def summarize_state_diff(before: Mapping[str, JSONValue], after: Mapping[str, JSONValue]) -> str:
    """Return a short human-readable summary of how `after` differs from `before`.

    Reports added, removed, and changed top-level fields. Fields are not
    diffed recursively -- a changed field is reported as changed, not
    exploded into its own sub-diff -- because the coding skill's state
    schema (`goal`, `plan`, `findings`, `files`, ...) already declares the
    natural unit of description for a human skimming a branch's history.
    Typical callers pass the two states `rio_coding.session_store.tree`
    resolves for the branch point and the branch's leaf.
    """
    before_keys = set(before)
    after_keys = set(after)
    added = sorted(after_keys - before_keys)
    removed = sorted(before_keys - after_keys)
    changed = sorted(key for key in before_keys & after_keys if before[key] != after[key])

    if not added and not removed and not changed:
        return "No changes."

    lines: list[str] = []
    if added:
        lines.append("Added: " + ", ".join(f"{key}={_render_value(after[key])}" for key in added))
    if removed:
        lines.append("Removed: " + ", ".join(removed))
    if changed:
        lines.append(
            "Changed: "
            + ", ".join(
                f"{key} ({_render_value(before[key])} -> {_render_value(after[key])})"
                for key in changed
            )
        )
    return "\n".join(lines)


def _render_value(value: JSONValue) -> str:
    """Return a compact, length-capped rendering of one state field's value."""
    if isinstance(value, str):
        text = value
    elif isinstance(value, list):
        text = f"list[{len(value)}]"
    elif isinstance(value, dict):
        text = f"dict[{len(value)}]"
    else:
        text = str(value)
    if len(text) > _MAX_VALUE_CHARS:
        return text[: _MAX_VALUE_CHARS - 3] + "..."
    return text
