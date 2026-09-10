"""Cache file slices in execution state and reject writes against stale hashes."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from rio.agent import apply_state_delta, state_size_chars
from rio.ai.messages import TextContent
from rio.ai.tools import AgentTool, AgentToolResult, ToolCancellationToken
from rio.ai.types import JSONObject, JSONValue
from rio.coding.tools import DEFAULT_MAX_OUTPUT_LINES, ToolInputError, resolve_path_argument


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
class FileContext:
    """Check writes before execution and cache successful file actions."""

    cwd: Path
    state_budget_chars: int | None = None

    async def execute(
        self,
        action: AgentTool,
        call_id: str,
        arguments: Mapping[str, JSONValue],
        state: JSONObject,
        signal: ToolCancellationToken | None = None,
    ) -> tuple[AgentToolResult, JSONObject]:
        name = action.name
        if name not in ("read", "write", "edit"):
            return await action.execute(call_id, arguments, signal), {}

        path = resolve_path_argument(arguments, cwd=self.cwd)
        key = state_key(path, self.cwd)
        previous = file_entry(state, key)
        existed = path.is_file()
        # ponytail: preflight is outside the tool lock; move it inside for concurrent writes.
        if name in ("write", "edit") and existed:
            recorded = previous.get("hash")
            if not isinstance(recorded, str):
                raise ToolInputError(f"{key} exists but has no recorded hash. Read it first.")
            if recorded != short_hash(path.read_bytes()):
                raise ToolInputError(f"{key} changed on disk. Read it again before you {name} it.")

        result = await action.execute(call_id, arguments, signal)
        if name == "read":
            details = result.details
            if not isinstance(details, Mapping):
                return result, {}
            start, end = details.get("start_line"), details.get("end_line")
            if not isinstance(start, int) or not isinstance(end, int):
                return result, {}  # Images and oversized lines have no text slice.
            status, ranges = "read", [(start, end)]
        else:
            status = "edited" if existed else "created"
            # Refresh cached windows after edits; deliberately forgotten files stay forgotten.
            ranges = _cached_ranges(previous) if existed else [(1, DEFAULT_MAX_OUTPUT_LINES)]

        try:
            data = path.read_bytes()
            text = data.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return result, {}

        digest = short_hash(data)
        entry: dict[str, JSONValue] = {"status": status, "hash": digest}
        if "context" in previous:
            entry["context"] = None
        cached = dict(entry)
        if ranges:
            lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            slices = _slice_patch(
                previous, lines, ranges, keep_previous=previous.get("hash") == digest
            )
            cached["context"] = {"total_lines": len(lines), "slices": slices}

        delta: JSONObject = {"files": {key: cached}}
        if (
            self.state_budget_chars is None
            or state_size_chars(apply_state_delta(state, delta)) <= self.state_budget_chars
        ):
            return result, delta

        result.content.append(
            TextContent(
                text=(
                    f"\n\n[{key} was not cached in state.files: the state is at its size limit. "
                    "Forget a file you are done with -- set its `files[path].context` to null, "
                    "keeping what you learned in its `note` -- then read this one again.]"
                )
            )
        )
        delta = {"files": {key: entry}}
        if state_size_chars(apply_state_delta(state, delta)) > self.state_budget_chars:
            delta = {}  # Even metadata needs space; leave the state unchanged until a re-read.
        return result, delta


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

    # ponytail: quadratic in slice count; use interval merging if many ranges accumulate.
    patch: dict[str, JSONValue] = dict(fresh)
    for start, end in _cached_ranges(previous):
        key = f"{start}-{end}"
        if key in fresh:
            continue
        covered = any(new_start <= start and end <= new_end for new_start, new_end in ranges)
        if covered or not keep_previous:
            patch[key] = None
    return patch
