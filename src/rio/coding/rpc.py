"""JSONL RPC frontend for a rio coding session.

tau's RPC surface was a Pi-compatible protocol over an append-only transcript:
`get_messages`, `get_fork_messages`, `compact`, `set_auto_compaction`, and a
`get_entries`/`get_tree` pair that projected conversation messages. None of
that has a counterpart here. A rio session has no transcript to page through
and nothing to compact (see `rio.coding.session` and `rio.coding.step_footprint`
for why), so those methods are dropped rather than reimplemented.

What a frontend needs instead is direct visibility into the one thing a rio
session actually holds: its execution state. `get_execution_state` is new for
this port -- it has no tau equivalent because tau's session never had a single
state object to expose. `get_entries`/`get_tree`/`get_checkpoints` survive, but
now project `rio.coding.session_store` state-journal entries (turns, steps,
resets, ...) instead of messages, and `restore` replaces tau's `fork`: a rio
"branch" is adopting a checkpointed state snapshot wholesale, not replaying a
message subtree.
"""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from typing import IO, Protocol, cast

import anyio
from pydantic import BaseModel

from rio.agent import (
    ActionEndEvent,
    ActionStartEvent,
    ReasoningDiscardedEvent,
    RunEndEvent,
    RunStartEvent,
    StateUpdateEvent,
    StepEndEvent,
    StepStartEvent,
    ValidationErrorEvent,
)
from rio.ai.types import JSONValue
from rio.coding.events import CodingSessionEvent, EntryAppendedEvent
from rio.coding.session_store import (
    BranchSummaryEntry,
    CustomEntry,
    JsonlSessionStorage,
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
)
from rio.coding.thinking import THINKING_LEVELS, next_thinking_level

_MAX_RECORD_BYTES = 16 * 1024 * 1024


class RpcSession(Protocol):
    """Public `CodingSession` surface consumed by RPC mode."""

    @property
    def model(self) -> str: ...

    @property
    def provider_name(self) -> str | None: ...

    @property
    def thinking_level(self) -> str | None: ...

    @property
    def state(self) -> dict[str, JSONValue]: ...

    @property
    def plan_progress(self) -> tuple[int, int]: ...

    @property
    def touched_files(self) -> list[str]: ...

    @property
    def answer(self) -> str | None: ...

    @property
    def state_summary(self) -> str: ...

    @property
    def context_window_tokens(self) -> int: ...

    @property
    def context_usage(self) -> object: ...

    @property
    def is_running(self) -> bool: ...

    @property
    def queued_steering_messages(self) -> tuple[str, ...]: ...

    @property
    def queued_follow_up_messages(self) -> tuple[str, ...]: ...

    @property
    def session_name(self) -> str: ...

    @property
    def storage(self) -> object: ...

    def prompt(self, text: str) -> AsyncIterator[CodingSessionEvent]: ...

    def continue_(self) -> AsyncIterator[CodingSessionEvent]: ...

    def cancel(self) -> None: ...

    def queue_steering_message(self, text: str) -> object: ...

    def queue_follow_up_message(self, text: str) -> object: ...

    async def set_thinking_level(self, level: str | None) -> object: ...

    async def set_model(self, model: str, *, provider_name: str | None = None) -> None: ...

    async def new_session(self) -> None: ...

    async def session_entries(self) -> list[SessionEntry]: ...

    async def checkpoints(self) -> list[SessionEntry]: ...

    async def restore(self, entry_id: str, *, reason: str | None = None) -> object: ...

    async def set_session_name(self, name: str) -> object: ...

    async def reload(self) -> object: ...

    async def aclose(self) -> None: ...


class RpcServer:
    """Read JSONL commands and stream responses/events as strict JSONL."""

    def __init__(
        self,
        session: RpcSession,
        *,
        stdin: IO[str] | None = None,
        stdout: IO[str] | None = None,
    ) -> None:
        self._session = session
        self._stdin = stdin or sys.stdin
        self._stdout = stdout or sys.stdout
        self._write_lock = anyio.Lock()
        self._active_run_tasks = 0

    async def run(self) -> None:
        """Serve commands until stdin reaches EOF."""
        try:
            async with anyio.create_task_group() as tasks:
                while True:
                    line = await anyio.to_thread.run_sync(self._stdin.readline)
                    if line == "":
                        break
                    if line.endswith("\n"):
                        line = line[:-1]
                    if line.endswith("\r"):
                        line = line[:-1]
                    if not line:
                        continue
                    if len(line.encode("utf-8")) > _MAX_RECORD_BYTES:
                        await self._error(None, "parse", "RPC record exceeds 16 MiB")
                        continue
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError as exc:
                        await self._error(None, "parse", f"Failed to parse command: {exc.msg}")
                        continue
                    if not isinstance(value, dict):
                        await self._error(None, "parse", "Command must be a JSON object")
                        continue
                    await self._dispatch(cast(dict[str, object], value), tasks)
                # EOF closes input; let accepted runs finish writing their output.
        finally:
            await self._session.aclose()

    async def _dispatch(self, command: dict[str, object], tasks: anyio.abc.TaskGroup) -> None:
        request_id = command.get("id")
        command_type = command.get("type")
        if not isinstance(command_type, str):
            await self._error(request_id, "parse", "Command requires a string 'type'")
            return
        try:
            if command_type in {
                "set_thinking_level",
                "cycle_thinking_level",
                "set_model",
                "new_session",
                "reload",
                "restore",
            } and (self._active_run_tasks or self._session.is_running):
                raise ValueError("Wait for the active run before changing session configuration")
            if command_type == "prompt":
                if self._active_run_tasks or self._session.is_running:
                    raise ValueError(
                        "Agent is already running; use 'steer' or 'follow_up' instead of 'prompt'"
                    )
                await self._start_run(
                    request_id,
                    command_type,
                    self._session.prompt(_required_string(command, "message")),
                    tasks,
                )
                return
            if command_type == "steer":
                self._session.queue_steering_message(_required_string(command, "message"))
                await self._response(request_id, command_type)
                return
            if command_type == "follow_up":
                self._session.queue_follow_up_message(_required_string(command, "message"))
                await self._response(request_id, command_type)
                return
            if command_type == "continue":
                if self._active_run_tasks or self._session.is_running:
                    raise ValueError("Agent is already running")
                await self._start_run(request_id, command_type, self._session.continue_(), tasks)
                return
            if command_type == "abort":
                self._session.cancel()
                await self._response(request_id, command_type)
                return
            if command_type == "get_state":
                await self._response(request_id, command_type, self._state_wire())
                return
            if command_type == "get_execution_state":
                await self._response(request_id, command_type, self._execution_state_wire())
                return
            if command_type == "get_available_thinking_levels":
                await self._response(request_id, command_type, {"levels": list(THINKING_LEVELS)})
                return
            if command_type == "set_thinking_level":
                event = await self._session.set_thinking_level(_required_string(command, "level"))
                await self._response(request_id, command_type, _jsonable(event))
                return
            if command_type == "cycle_thinking_level":
                level = next_thinking_level(self._session.thinking_level)
                event = await self._session.set_thinking_level(level)
                await self._response(request_id, command_type, _jsonable(event))
                return
            if command_type == "set_model":
                provider = _optional_string(command, "provider")
                await self._session.set_model(
                    _required_string(command, "model"), provider_name=provider
                )
                await self._response(
                    request_id,
                    command_type,
                    {"model": self._session.model, "provider": self._session.provider_name},
                )
                return
            if command_type == "new_session":
                await self._session.new_session()
                await self._response(request_id, command_type)
                return
            if command_type == "set_session_name":
                event = await self._session.set_session_name(_required_string(command, "name"))
                await self._response(request_id, command_type, _jsonable(event))
                return
            if command_type == "reload":
                await self._session.reload()
                await self._response(request_id, command_type)
                return
            if command_type == "get_entries":
                cursor_entries = list(await self._session.session_entries())
                leaf_id = latest_leaf_id(cursor_entries)
                since = _optional_string(command, "since")
                if since is not None:
                    try:
                        index = next(
                            i for i, entry in enumerate(cursor_entries) if entry.id == since
                        )
                    except StopIteration as exc:
                        raise ValueError(f"Entry not found: {since}") from exc
                    cursor_entries = cursor_entries[index + 1 :]
                await self._response(
                    request_id,
                    command_type,
                    {
                        "entries": [
                            projected
                            for entry in cursor_entries
                            if (projected := _entry_wire(entry)) is not None
                        ],
                        "leafId": leaf_id,
                    },
                )
                return
            if command_type == "get_tree":
                entries = await self._session.session_entries()
                await self._response(
                    request_id,
                    command_type,
                    {"tree": _tree_wire(entries), "leafId": latest_leaf_id(entries)},
                )
                return
            if command_type == "get_checkpoints":
                entries = await self._session.checkpoints()
                await self._response(
                    request_id,
                    command_type,
                    {
                        "checkpoints": [
                            projected
                            for entry in entries
                            if (projected := _entry_wire(entry)) is not None
                        ]
                    },
                )
                return
            if command_type == "restore":
                event = await self._session.restore(
                    _required_string(command, "entryId"),
                    reason=_optional_string(command, "reason"),
                )
                await self._response(request_id, command_type, _jsonable(event))
                return
            raise ValueError(f"Unknown command: {command_type}")
        except Exception as exc:
            await self._error(request_id, command_type, str(exc))

    async def _start_run(
        self,
        request_id: object,
        command_type: str,
        stream: AsyncIterator[CodingSessionEvent],
        tasks: anyio.abc.TaskGroup,
    ) -> None:
        try:
            first_event = await anext(stream)
        except StopAsyncIteration:
            await self._response(request_id, command_type)
            return
        self._active_run_tasks += 1
        await self._response(request_id, command_type)

        async def drain() -> None:
            try:
                await self._write(_event_wire(first_event))
                async for event in stream:
                    await self._write(_event_wire(event))
            except Exception as exc:
                await self._error(request_id, command_type, str(exc))
            finally:
                await stream.aclose()
                self._active_run_tasks -= 1

        tasks.start_soon(drain)

    # -- wire projections ------------------------------------------------

    def _state_wire(self) -> dict[str, JSONValue]:
        usage = self._session.context_usage
        return {
            "model": self._session.model,
            "provider": self._session.provider_name,
            "thinkingLevel": self._session.thinking_level or "off",
            "isRunning": self._session.is_running,
            "queuedSteering": list(self._session.queued_steering_messages),
            "queuedFollowUp": list(self._session.queued_follow_up_messages),
            "sessionName": self._session.session_name,
            "sessionFile": _session_file(self._session),
            "contextUsage": {
                "tokens": usage.total_tokens,
                "contextWindow": self._session.context_window_tokens,
                "percent": round(usage.utilization * 100, 2),
            },
        }

    def _execution_state_wire(self) -> dict[str, JSONValue]:
        completed, total = self._session.plan_progress
        return {
            "state": self._session.state,
            "planProgress": {"completed": completed, "total": total},
            "touchedFiles": list(self._session.touched_files),
            "answer": self._session.answer,
            "summary": self._session.state_summary,
        }

    # -- transport ---------------------------------------------------------

    async def _response(
        self,
        request_id: object,
        command: str,
        data: object | None = None,
    ) -> None:
        response: dict[str, object] = {
            "type": "response",
            "command": command,
            "success": True,
        }
        if request_id is not None:
            response["id"] = request_id
        if data is not None:
            response["data"] = data
        await self._write(response)

    async def _error(self, request_id: object, command: str, error: str) -> None:
        response: dict[str, object] = {
            "type": "response",
            "command": command,
            "success": False,
            "error": error,
        }
        if request_id is not None:
            response["id"] = request_id
        await self._write(response)

    async def _write(self, value: object) -> None:
        payload = json.dumps(_jsonable(value), ensure_ascii=False, separators=(",", ":"))
        async with self._write_lock:
            self._stdout.write(payload + "\n")
            self._stdout.flush()


def _required_string(command: Mapping[str, object], key: str) -> str:
    value = command.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _optional_string(command: Mapping[str, object], key: str) -> str | None:
    value = command.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _session_file(session: RpcSession) -> str | None:
    storage = session.storage
    return str(storage.path) if isinstance(storage, JsonlSessionStorage) else None


def _entry_wire(entry: SessionEntry) -> dict[str, JSONValue] | None:
    if isinstance(entry, LeafEntry):
        return None
    timestamp = datetime.fromtimestamp(entry.timestamp, tz=UTC).isoformat().replace("+00:00", "Z")
    base: dict[str, JSONValue] = {
        "type": entry.type,
        "id": entry.id,
        "parentId": entry.parent_id,
        "timestamp": timestamp,
    }
    if isinstance(entry, TurnEntry):
        return {**base, "observation": entry.observation, "state": entry.state}
    if isinstance(entry, StepEntry):
        return {
            **base,
            "step": entry.step,
            "stateDelta": entry.state_delta,
            "state": entry.state,
            "action": {"name": entry.action.name, "arguments": entry.action.arguments},
            "observation": entry.observation,
            "observationTruncated": entry.observation_truncated,
            "terminated": entry.terminated,
        }
    if isinstance(entry, StateResetEntry):
        return {
            **base,
            "state": entry.state,
            "reason": entry.reason,
            "restoredFromEntryId": entry.restored_from_entry_id,
        }
    if isinstance(entry, ValidationFailureEntry):
        return {**base, "step": entry.step, "attempt": entry.attempt, "error": entry.error}
    if isinstance(entry, ReasoningEntry):
        return {
            **base,
            "step": entry.step,
            "reasoning": entry.reasoning,
            "truncated": entry.truncated,
        }
    if isinstance(entry, ModelChangeEntry):
        return {**base, "model": entry.model, "provider": entry.provider}
    if isinstance(entry, ThinkingLevelChangeEntry):
        return {**base, "thinkingLevel": entry.thinking_level or "off"}
    if isinstance(entry, BranchSummaryEntry):
        return {**base, "summary": entry.summary, "branchRootId": entry.branch_root_id}
    if isinstance(entry, LabelEntry):
        return {**base, "label": entry.label}
    if isinstance(entry, SessionInfoEntry):
        return {**base, "cwd": entry.cwd, "title": entry.title, "skill": entry.skill}
    if isinstance(entry, CustomEntry):
        return {**base, "namespace": entry.namespace, "data": entry.data}
    raise AssertionError(f"Unhandled rio session entry: {entry.type}")  # pragma: no cover


def _tree_wire(entries: list[SessionEntry]) -> list[JSONValue]:
    visible = tuple(entry for entry in entries if not isinstance(entry, LeafEntry))
    children: dict[str | None, list[SessionEntry]] = {}
    ids = {entry.id for entry in visible}
    for entry in visible:
        parent = entry.parent_id if entry.parent_id in ids else None
        children.setdefault(parent, []).append(entry)

    def build(entry: SessionEntry) -> dict[str, JSONValue]:
        projected = _entry_wire(entry)
        if projected is None:
            raise AssertionError("Leaf entries must be filtered before tree projection")
        return {
            "entry": projected,
            "children": [build(child) for child in children.get(entry.id, [])],
        }

    return [build(entry) for entry in children.get(None, [])]


def _skill_event_wire(event: object) -> dict[str, JSONValue]:
    if isinstance(event, RunStartEvent):
        return {"type": "run_start", "skill": event.skill}
    if isinstance(event, StepStartEvent):
        return {
            "type": "step_start",
            "step": event.step,
            "state": event.state,
            "observation": {
                "userMessage": event.observation.user_message,
                "toolCallResult": event.observation.tool_call_result,
            },
        }
    if isinstance(event, ReasoningDiscardedEvent):
        return {"type": "reasoning_discarded", "step": event.step, "reasoning": event.reasoning}
    if isinstance(event, ValidationErrorEvent):
        return {
            "type": "validation_error",
            "step": event.step,
            "attempt": event.attempt,
            "error": event.error,
        }
    if isinstance(event, StateUpdateEvent):
        return {
            "type": "state_update",
            "step": event.step,
            "delta": event.delta,
            "state": event.state,
        }
    if isinstance(event, ActionStartEvent):
        return {
            "type": "action_start",
            "step": event.step,
            "name": event.name,
            "arguments": event.arguments,
        }
    if isinstance(event, ActionEndEvent):
        return {
            "type": "action_end",
            "step": event.step,
            "name": event.name,
            "result": _jsonable(event.result),
            "isError": event.is_error,
        }
    if isinstance(event, StepEndEvent):
        return {
            "type": "step_end",
            "step": event.step,
            "state": event.state,
            "terminated": event.terminated,
        }
    if isinstance(event, RunEndEvent):
        return {"type": "run_end", "steps": event.steps, "state": event.state}
    raise AssertionError(f"Unhandled skill event: {event!r}")  # pragma: no cover


def _event_wire(event: CodingSessionEvent) -> dict[str, JSONValue]:
    """Project one skill or session event to its JSONL wire shape."""
    if isinstance(event, EntryAppendedEvent):
        return {"type": "entry_appended", "entry": _entry_wire(event.entry)}
    if isinstance(event, BaseModel):
        return cast(dict[str, JSONValue], event.model_dump(mode="json", by_alias=True))
    return _skill_event_wire(event)


def _jsonable(value: object) -> JSONValue:
    if isinstance(value, EntryAppendedEvent):
        return _event_wire(value)
    if isinstance(value, BaseModel):
        return cast(JSONValue, value.model_dump(mode="json", by_alias=True))
    if isinstance(value, Mapping):
        return cast(JSONValue, {str(key): _jsonable(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return cast(JSONValue, [_jsonable(item) for item in value])
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


async def run_rpc_session(session: RpcSession) -> None:
    """Run RPC mode for an already configured `CodingSession`."""
    await RpcServer(session).run()
