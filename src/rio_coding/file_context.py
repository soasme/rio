"""Cache file slices in execution state and reject writes against stale hashes."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from rio_agent import ActionOutcome, apply_state_delta, state_size_chars
from rio_ai.tools import AgentToolResult
from rio_ai.types import JSONObject, JSONValue
from rio_coding.tools import ToolInputError, resolve_path_argument


class StaleFileError(RuntimeError):
    """A write or edit was refused because the state's copy of the file is out of date."""


def short_hash(data: bytes) -> str:
    """Return the short digest recorded for a file's contents."""
    return hashlib.sha256(data).hexdigest()[:8]


def state_key(path: Path, cwd: Path) -> str:
    """Return the `files` key for a path: relative to the run's cwd where it can be."""
    try:
        return path.resolve().relative_to(cwd.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def file_entry(state: Mapping[str, JSONValue], key: str) -> Mapping[str, JSONValue]:
    """Return what the state records about one file, or an empty mapping."""
    files = state.get("files")
    if not isinstance(files, Mapping):
        return {}
    entry = files.get(key)
    return entry if isinstance(entry, Mapping) else {}


@dataclass(frozen=True, slots=True)
class FileContextObserver:
    """Check writes before execution and cache successful file actions."""

    cwd: Path
    state_budget_chars: int | None = None

    def before_action(
        self, state: JSONObject, name: str, arguments: Mapping[str, JSONValue]
    ) -> None:
        """Refuse a write or edit whose file no longer matches the state's copy."""
        if name not in ("write", "edit"):
            return
        path = self._path(arguments)
        if path is None or not path.is_file():
            # A brand new file has nothing to be stale against; a missing one
            # is the tool's error to report, not ours.
            return
        key = state_key(path, self.cwd)
        recorded = file_entry(state, key).get("hash")
        try:
            current = short_hash(path.read_bytes())
        except OSError:
            current = None
        if recorded == current:
            return
        if not isinstance(recorded, str):
            raise StaleFileError(
                f"{key} exists but is not in your state's `files`. Read it first: "
                f"{name} would discard content you have never seen."
            )
        raise StaleFileError(
            f"{key} changed on disk since you read it (state records hash {recorded}, "
            f"the file is now {current}). Read it again before you {name} it."
        )

    def after_action(
        self,
        state: JSONObject,
        name: str,
        arguments: Mapping[str, JSONValue],
        result: AgentToolResult,
    ) -> ActionOutcome:
        """Record what the action left on disk into `state.files`."""
        if name not in ("read", "write", "edit"):
            return ActionOutcome()
        path = self._path(arguments)
        if path is None:
            return ActionOutcome()
        key = state_key(path, self.cwd)
        previous = file_entry(state, key)
        if name == "read":
            details = result.details or {}
            start, end = details.get("start_line"), details.get("end_line")
            if not isinstance(start, int) or not isinstance(end, int):
                return ActionOutcome()  # Images and oversized lines have no text slice.
            status, ranges = "read", [(start, end)]
        else:
            status = "edited" if previous else "created"
            # Refresh cached windows after edits; deliberately forgotten files stay forgotten.
            ranges = _cached_ranges(previous) if previous else [(1, 2_000)]

        try:
            data = path.read_bytes()
            text = data.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return ActionOutcome()

        digest = short_hash(data)
        entry: dict[str, JSONValue] = {"status": status, "hash": digest}
        if "context" in previous:
            entry["context"] = None
        if not ranges:
            return ActionOutcome(delta={"files": {key: entry}})

        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        slices = _slice_patch(previous, lines, ranges, keep_previous=previous.get("hash") == digest)
        cached = dict(entry)
        cached["context"] = {"total_lines": len(lines), "slices": slices}

        delta: JSONObject = {"files": {key: cached}}
        if (
            self.state_budget_chars is None
            or state_size_chars(apply_state_delta(state, delta)) <= self.state_budget_chars
        ):
            return ActionOutcome(delta=delta)

        return ActionOutcome(
            delta={"files": {key: entry}},
            note=(
                f"{key} was not cached in state.files: the state is at its size limit. "
                "Forget a file you are done with -- set its `files[path].context` to null, "
                "keeping what you learned in its `note` -- then read this one again."
            ),
        )

    def _path(self, arguments: Mapping[str, JSONValue]) -> Path | None:
        try:
            return resolve_path_argument(arguments, cwd=self.cwd)
        except ToolInputError:
            return None


def _cached_ranges(entry: Mapping[str, JSONValue]) -> list[tuple[int, int]]:
    """Return the line ranges an entry has slices for."""
    context = entry.get("context")
    if not isinstance(context, Mapping):
        return []
    slices = context.get("slices")
    if not isinstance(slices, Mapping):
        return []
    ranges = []
    for key in slices:
        try:
            start, end = key.split("-")
            ranges.append((int(start), int(end)))
        except (AttributeError, ValueError):
            continue
    return ranges


def _slice_patch(
    previous: Mapping[str, JSONValue],
    lines: list[str],
    ranges: list[tuple[int, int]],
    *,
    keep_previous: bool,
) -> dict[str, JSONValue]:
    """Replace stale or covered slices using nulls for JSON Merge Patch deletions."""
    fresh: dict[str, JSONValue] = {}
    for start, end in ranges:
        start = max(1, start)
        end = min(end, len(lines))
        if start > end:
            continue
        fresh[f"{start}-{end}"] = "\n".join(lines[start - 1 : end])

    patch: dict[str, JSONValue] = dict(fresh)
    for start, end in _cached_ranges(previous):
        key = f"{start}-{end}"
        if key in fresh:
            continue
        covered = any(new_start <= start and end <= new_end for new_start, new_end in ranges)
        if covered or not keep_previous:
            patch[key] = None
    return patch
