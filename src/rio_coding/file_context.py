"""File contents held in the execution state, and the hash that keeps them honest.

Under SKILL.state the model sees three things each step: the instructions, the
state, and the latest observation. An observation lives for exactly one step,
so a file that was read and not written down is gone by the next step, and the
only way back to it is another `read`. That is how a run ends up reading the
same 200-line file seventeen times (issue #58).

So the runtime writes what a read produced into the state itself. After a
successful `read`, `write`, or `edit`, `state.files[path]` carries:

* `status` -- `read`, `edited`, or `created`, stamped from what the action
  actually did rather than from what the model intended before it ran.
* `hash` -- a short digest of the file on disk at that moment.
* `context` -- the file content, as `total_lines` plus a `slices` map keyed by
  the line range each slice covers. A large file arrives one slice at a time;
  reading another range adds a slice instead of replacing one.
* `note` -- the only field the model owns: why the file matters, and what
  survives after the content is dropped.

The hash is what makes the cache safe to act on. `write` and `edit` are
refused unless the recorded hash still matches the file on disk, so an edit
can never be composed against content that something else has since changed --
the model is told to read the file again instead.

Content in the state costs prompt space every step, so caching is bounded by
the skill's state budget: when a file will not fit, its `status` and `hash`
are still recorded, the content is not, and the observation says so. The model
frees space by forgetting a file -- setting `files[path].context` to null and
keeping what it learned in `note`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from rio_agent import ActionOutcome, apply_state_delta, check_state_budget
from rio_agent.errors import StateValidationError
from rio_ai.tools import AgentToolResult
from rio_ai.types import JSONObject, JSONValue
from rio_coding.tools import ToolInputError, resolve_path_argument

#: Digest length. Long enough that two versions of one file never collide in
#: practice, short enough to be free to carry in every prompt.
HASH_LENGTH = 8

#: Lines cached for a file the model wrote but never read.
DEFAULT_SLICE_LINES = 2_000

READ_ACTIONS = ("read",)
WRITE_ACTIONS = ("write", "edit")


class StaleFileError(RuntimeError):
    """A write or edit was refused because the state's copy of the file is out of date."""


def short_hash(data: bytes) -> str:
    """Return the short digest recorded for a file's contents."""
    return hashlib.sha256(data).hexdigest()[:HASH_LENGTH]


def file_hash(path: Path) -> str | None:
    """Return the digest of the file on disk, or `None` if it cannot be read."""
    try:
        return short_hash(path.read_bytes())
    except OSError:
        return None


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
    """Keeps `state.files` in step with the filesystem.

    Implements `rio_agent.ActionObserver`: it refuses a write against a stale
    copy before the action runs, and records content and hash after it does.
    """

    cwd: Path
    state_budget_chars: int | None = None

    # -- before ---------------------------------------------------------------

    def before_action(
        self, state: JSONObject, name: str, arguments: Mapping[str, JSONValue]
    ) -> None:
        """Refuse a write or edit whose file no longer matches the state's copy."""
        if name not in WRITE_ACTIONS:
            return
        path = self._path(arguments)
        if path is None or not path.is_file():
            # A brand new file has nothing to be stale against; a missing one
            # is the tool's error to report, not ours.
            return
        key = state_key(path, self.cwd)
        recorded = file_entry(state, key).get("hash")
        current = file_hash(path)
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

    # -- after ----------------------------------------------------------------

    def after_action(
        self,
        state: JSONObject,
        name: str,
        arguments: Mapping[str, JSONValue],
        result: AgentToolResult,
    ) -> ActionOutcome:
        """Record what the action left on disk into `state.files`."""
        if name in READ_ACTIONS:
            return self._record_read(state, result)
        if name in WRITE_ACTIONS:
            return self._record_write(state, name, arguments)
        return ActionOutcome()

    def _record_read(self, state: JSONObject, result: AgentToolResult) -> ActionOutcome:
        details = result.details or {}
        raw_path = details.get("path")
        start = details.get("start_line")
        end = details.get("end_line")
        if not isinstance(raw_path, str) or not isinstance(start, int) or not isinstance(end, int):
            # An image, or a line too large to show: nothing text-shaped to cache.
            return ActionOutcome()
        return self._record(state, Path(raw_path), status="read", ranges=[(start, end)])

    def _record_write(
        self, state: JSONObject, name: str, arguments: Mapping[str, JSONValue]
    ) -> ActionOutcome:
        path = self._path(arguments)
        if path is None:
            return ActionOutcome()
        key = state_key(path, self.cwd)
        previous = file_entry(state, key)
        status = "edited" if previous else "created"
        # The file just changed under every cached slice, so re-cache the same
        # windows rather than leaving the model a copy that no longer matches.
        # A file the model deliberately forgot stays forgotten: no slices back.
        ranges = _cached_ranges(previous) if previous else [(1, DEFAULT_SLICE_LINES)]
        return self._record(state, path, status=status, ranges=ranges)

    def _record(
        self,
        state: JSONObject,
        path: Path,
        *,
        status: str,
        ranges: list[tuple[int, int]],
    ) -> ActionOutcome:
        key = state_key(path, self.cwd)
        try:
            data = path.read_bytes()
            text = data.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return ActionOutcome()

        digest = short_hash(data)
        previous = file_entry(state, key)
        # A file with no cached content has no `context` key at all, so the
        # absent key means one thing whether the content was never held, was
        # forgotten, or did not fit.
        entry: dict[str, JSONValue] = {"status": status, "hash": digest}
        if "context" in previous:
            entry["context"] = None
        if not ranges:
            return ActionOutcome(delta={"files": {key: entry}})

        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        # Slices cached from a different version of the file describe content
        # that is no longer there, so a changed hash discards all of them.
        slices = _slice_patch(previous, lines, ranges, keep_previous=previous.get("hash") == digest)
        cached = dict(entry)
        cached["context"] = {"total_lines": len(lines), "slices": slices}

        delta: JSONObject = {"files": {key: cached}}
        if self._fits(state, delta):
            return ActionOutcome(delta=delta)

        # Recording the content would push the state past what the prompt can
        # carry. Keep the facts, drop the copy, and say which lever frees space.
        return ActionOutcome(
            delta={"files": {key: entry}},
            note=(
                f"{key} was not cached in state.files: the state is at its size limit. "
                "Forget a file you are done with -- set its `files[path].context` to null, "
                "keeping what you learned in its `note` -- then read this one again."
            ),
        )

    def _fits(self, state: JSONObject, delta: JSONObject) -> bool:
        try:
            check_state_budget(apply_state_delta(state, delta), max_chars=self.state_budget_chars)
        except StateValidationError:
            return False
        return True

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
    return [parsed for key in slices if (parsed := _parse_range(key)) is not None]


def _parse_range(key: object) -> tuple[int, int] | None:
    if not isinstance(key, str) or "-" not in key:
        return None
    start, _, end = key.partition("-")
    try:
        return int(start), int(end)
    except ValueError:
        return None


def _slice_patch(
    previous: Mapping[str, JSONValue],
    lines: list[str],
    ranges: list[tuple[int, int]],
    *,
    keep_previous: bool,
) -> dict[str, JSONValue]:
    """Return the merge patch for a file's slices: the new ones, plus nulls for those they replace.

    A slice already covered by one being written is dropped rather than kept
    alongside it, so re-reading a wider range of a file does not pay for the
    narrower one twice. When `keep_previous` is false the file itself has
    changed, so every earlier slice goes.
    """
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
