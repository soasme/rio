"""Fixed model rounds with persisted attempts, usage, retries, and run endings."""

from __future__ import annotations

import asyncio
import contextlib
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Literal

from rio.agent.prompt import STEP
from rio.agent.session import RunStopped, Session
from rio.agent.shapes import JournalEvent, Turn, ValidatorResult
from rio.agent.state import InvalidPatch
from rio.agent.store import StorageFailure, encode
from rio.ai.messages import AssistantMessage, UserMessage, raw_tool_arguments
from rio.ai.provider import AssistantDoneEvent, AssistantErrorEvent, ModelProvider


class RecoveryFailure(RuntimeError):
    """Active work could not be stopped, so its effects remain uncertain."""


class Owner:
    """Serial dispatch, controls, conclusions and wake-ups, without an execution environment.

    A note-only agent uses this class directly. Agents with runnable Cell kinds subclass it
    and implement `launch` and `validate`; see `rio.coding.owner.ProcessOwner`.
    """

    def __init__(self, session: Session):
        self.session = session
        self.task: asyncio.Task | None = None
        self.active_id: int | None = None
        # Set by every commit and by the active task ending; cleared at the start of tick().
        self.changed = asyncio.Event()

    async def __aenter__(self):
        cells = self.session.data["cells"].values()
        missing = {c["kind"] for c in cells} - {"note", *self.session.kinds}
        if missing:
            raise ValueError(f"Open this Session with its Cell kinds: {', '.join(sorted(missing))}")
        self.session.store.on_commit = self.changed.set
        await self.recover()
        return self

    async def __aexit__(self, *args):
        try:
            await self.stop_active()
        finally:
            self.session.store.on_commit = None

    def launch(self, cell_id: int) -> Awaitable[None]:
        """Authorize `cell_id` synchronously and return the awaitable that executes it."""
        raise NotImplementedError("This owner cannot execute runnable Cells")

    async def validate(self, key: str, command: list[str]) -> ValidatorResult:
        """Run one configured validator; `key` identifies it across restarts."""
        raise NotImplementedError("This owner cannot run validators")

    async def recover(self) -> None:
        # No dispatch happens before this finishes.
        if not self.session.meta["terminal"]:
            self.session.fire_timers()
            await self.controls()

    def clear(self) -> None:
        self.task = self.active_id = None

    async def stop_active(self) -> None:
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        self.clear()

    async def fail(self, reason: str, *, note_id: str | None = None) -> None:
        error = None
        try:
            await self.stop_active()
        except RecoveryFailure as exc:
            error = str(exc)
        for execution in list(self.session.pending()):
            if execution["phase"] == "queued":
                self.session.finish(execution["cell_id"], {"type": "cancelled"})
            else:
                self.session.finish(
                    execution["cell_id"],
                    {"type": "UnknownExecution", "reason": reason, "worker_stopped": error is None},
                )
        self.session.end("failure", reason, cleanup_error=error, note_id=note_id)

    async def controls(self) -> None:
        controls = sorted(
            self.session.data["controls"].items(),
            key=lambda pair: (pair[1]["cell"].get("role") != "cancel", int(pair[0])),
        )
        for key, control in controls:
            if control["handled"]:
                continue
            cell = control["cell"]
            role = cell["role"]
            if role == "cancel":
                target = cell["target_cell_id"]
                execution = self.session.data["executions"][str(target)]
                if execution["phase"] == "started":
                    # recover() finishes leftover work, so a started execution is the active one.
                    assert target == self.active_id
                    await self.stop_active()
                    self.session.finish(
                        target,
                        {
                            "type": "UnknownExecution",
                            "reason": "cancelled_after_launch",
                            "worker_stopped": True,
                        },
                    )
                elif execution["phase"] == "queued":
                    self.session.finish(target, {"type": "cancelled"})
                self.session.handled(key, "accepted")
            elif role == "resolution":
                self.session.handled(key, "accepted", resolution=cell["target_cell_id"])
            elif cell["result"] == "failure":
                await self.fail(cell["text"], note_id=key)
            else:
                reason = await self._success_blocker(key)
                if reason:
                    self.session.handled(key, reason)
                else:
                    self.session.end("success", cell["text"], note_id=key)
            if self.session.meta["terminal"]:
                break

    async def _success_blocker(self, note_id: str) -> str | None:
        self.session.fire_timers()
        if (
            self.session.pending()
            or self.session.unresolved()
            or any(not t["fired"] for t in self.session.data["timers"].values())
        ):
            return "Success requires no pending work or unresolved uncertainty"
        if any(not m["consumed"] for m in self.session.data["inbox"].values()):
            return "New observations must be consumed before success"
        # Validators execute afresh at the conclusion. The API uses argv lists,
        # never model-editable commands.
        for index, command in enumerate(self.session.meta["validators"]):
            result = await self.validate(f"{note_id}:{index}", command)
            self.session.record_validator_result(index, command, result)
            if result["exit"] != 0:
                return f"Required validator {index} failed: {result['output']}"
        self.session.fire_timers()
        if any(not m["consumed"] for m in self.session.data["inbox"].values()):
            return "Observations arrived during validation"
        return None

    async def wait(self, until: float = math.inf, request: asyncio.Task | None = None) -> None:
        """Wait for a commit, the active task ending, the next timer or `until`, or `request`.

        Wake sources: execution output and results, timer registrations and inbox input all
        commit; timer deadlines and retry backoff are deadlines; the model call is `request`.
        """
        timers = (t["deadline"] for t in self.session.data["timers"].values() if not t["fired"])
        deadline = min([until, *timers])
        timeout = None if deadline == math.inf else max(0, deadline - time.time())
        changed = asyncio.create_task(self.changed.wait())
        try:
            await asyncio.wait(
                {changed, *([request] if request else [])},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            changed.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await changed

    async def tick(self) -> None:
        # Anything that changes after this point wakes the next wait().
        self.changed.clear()
        if self.task and self.task.done():
            await self.task
            self.clear()
        if self.session.store.failed:
            raise StorageFailure("Owner must recover storage")
        self.session.fire_timers()
        await self.controls()
        if self.session.meta["terminal"] or self.session.meta["pause"] or self.task:
            return
        pending = self.session.pending()
        if pending:
            cell_id = pending[0]["cell_id"]
            self.task = asyncio.create_task(self.launch(cell_id))
            self.active_id = cell_id
            self.task.add_done_callback(lambda _: self.changed.set())


async def replay(
    owner: Owner,
    publish: Callable[[JournalEvent], None] | None = None,
) -> bool:
    """Recover leftover work and publish a terminal session without a model provider."""
    session = owner.session
    if not session.meta["terminal"]:
        raise ValueError("Only terminal sessions can be replayed")
    await owner.recover()
    if publish:
        for event in session.store.events():
            publish(event)
    return session.meta["terminal"]["result"] == "success"


async def run(
    owner: Owner,
    provider: ModelProvider,
    model: str,
    publish: Callable[[JournalEvent], None] | None = None,
) -> bool:
    session = owner.session
    published = 0

    def flush():
        nonlocal published
        for event in session.store.events(published):
            if publish:
                publish(event)
            published = event["seq"]

    async with owner:
        flush()
        while not session.meta["terminal"]:
            await owner.tick()
            flush()
            if session.meta["terminal"]:
                break
            action = _idle_action(owner)
            if action == "wait":
                await owner.wait()
                continue
            if action == "fail":
                await owner.fail("Repeated invalid idle decisions")
                break
            if action == "invalid":
                session.invalid_idle()
            try:
                turn = session.prepare_turn()
                session.accept(turn["id"], await _model_round(owner, turn, provider, model, flush))
                session.request_work()
                await owner.controls()
            except InvalidPatch as exc:
                session.reject(turn["id"], str(exc))
            except RunStopped as exc:
                await owner.fail(str(exc))
            flush()
        flush()
    return session.meta["terminal"]["result"] == "success"


def _idle_action(owner: Owner) -> Literal["ready", "wait", "invalid", "fail"]:
    session = owner.session
    if not session.data["turns"] or any(
        t["receipt"] is None for t in session.data["turns"].values()
    ):
        return "ready"
    # Successful completions wait for the queue to drain; a failure pauses it and wakes.
    batching = bool(session.pending()) and not session.meta["pause"]
    if any(
        not m["consumed"] and not (batching and m["kind"] == "execution")
        for m in session.data["inbox"].values()
    ):
        return "ready"
    if (
        owner.task
        or batching
        or any(not t["fired"] for t in session.data["timers"].values())
        or session.external_wakeup
    ):
        return "wait"
    if session.meta.get("idle", 0) >= session.limits.invalid_attempts:
        return "fail"
    return "invalid"


async def _model_round(
    owner: Owner,
    turn: Turn,
    provider: ModelProvider,
    model: str,
    flush: Callable[[], None],
) -> list:
    """Return the ops of one complete step call, reusing a saved response after restart."""
    session = owner.session
    if turn["response"] is not None:
        response = AssistantMessage.model_validate(turn["response"])
    else:
        while time.time() < turn["retry_at"]:
            await owner.wait(turn["retry_at"])
            await owner.tick()
            flush()
        turn = session.attempt(turn["id"])
        messages = [UserMessage(content=encode(turn["state"]))]
        if turn["error"]:
            messages.append(UserMessage(content="Rejected step: " + turn["error"]))
        # No State changes during generation. Owner work only appends runtime records.
        request = asyncio.create_task(
            _call_model(
                provider,
                model,
                turn["system"],
                messages,
                [replace(STEP, parameters=turn["tool_schema"])],
            )
        )
        try:
            while not request.done():
                await owner.tick()
                flush()
                await owner.wait(request=request)
            try:
                response = await request
            except Exception as exc:
                raise InvalidPatch(f"Provider request failed: {exc}") from exc
        finally:
            if not request.done():
                request.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await request
        usage = response.usage
        tokens = usage.total_tokens or (
            usage.input + usage.output + usage.cache_read + usage.cache_write
        )
        # Provider/API names do not establish that token usage was reported.
        # Missing and ambiguous zero usage remain unknown, never a free request.
        session.response(
            turn["id"], response.model_dump(mode="json"), max(0, tokens), known=tokens > 0
        )
    if response.stop_reason in ("error", "aborted"):
        raise InvalidPatch(response.error_message or "Provider failed")
    if len(response.tool_calls) != 1 or response.tool_calls[0].name != "step":
        raise InvalidPatch("Call exactly one step tool")
    arguments = response.tool_calls[0].arguments
    if raw_tool_arguments(arguments) is not None or set(arguments) != {"ops"}:
        raise InvalidPatch("step requires one complete ops argument")
    return arguments["ops"]


async def _call_model(
    provider: ModelProvider,
    model: str,
    system: str,
    messages: list,
    tools: list,
) -> AssistantMessage:
    result: AssistantMessage | None = None
    async for event in provider.stream_response(
        model=model, system=system, messages=messages, tools=tools
    ):
        if isinstance(event, AssistantDoneEvent):
            result = event.message
        elif isinstance(event, AssistantErrorEvent):
            result = event.error
    if result is None:
        raise RuntimeError("provider produced no assistant message")
    return result
