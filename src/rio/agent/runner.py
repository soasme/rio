"""Fixed model turns with persisted attempts, usage, retries, and run endings."""

from __future__ import annotations

import asyncio
import contextlib
import math
import time
from collections.abc import Callable, Coroutine
from typing import Literal, Self

from rio.agent.prompt import encode_state, step_tool
from rio.agent.session import RunStopped, Session
from rio.agent.shapes import ControlRequest, JournalEvent, Turn, ValidatorResult
from rio.agent.state import CONTROLS, InvalidPatch
from rio.agent.store import StorageFailure
from rio.ai.messages import AssistantMessage, UserMessage, raw_tool_arguments
from rio.ai.provider import ModelProvider
from rio.ai.retry import is_transient_status


class RecoveryFailure(RuntimeError):
    """Active work could not be stopped, so its effects remain uncertain."""


class ProviderFailure(RuntimeError):
    """The provider, not the model, failed to return a step."""

    def __init__(
        self, error: str, *, retryable: bool = True, retry_after: float | None = None
    ) -> None:
        super().__init__(error)
        self.retryable = retryable
        self.retry_after = retry_after


class Owner:
    """Serial dispatch, controls, conclusions and wake-ups, without an execution environment.

    A note-only agent uses this class directly. Agents with runnable Cell kinds subclass it
    and implement `launch` and `validate`; see `rio.coding.owner.ProcessOwner`.
    """

    def __init__(self, session: Session) -> None:
        self.session = session
        self.task: asyncio.Task | None = None
        self.active_id: int | None = None
        # Set by every commit and by the active task ending; cleared at the start of tick().
        self.changed = asyncio.Event()

    async def __aenter__(self) -> Self:
        cells = self.session.data.cells.values()
        missing = {c.kind for c in cells} - set(self.session.kinds)
        if missing:
            raise ValueError(f"Open this Session with its Cell kinds: {', '.join(sorted(missing))}")
        self.session.store.on_commit = self.changed.set
        await self.recover()
        return self

    async def __aexit__(self, *args: object) -> None:
        try:
            await self.stop_active()
        finally:
            self.session.store.on_commit = None

    def launch(self, cell_id: int) -> Coroutine[None, None, None]:
        """Authorize `cell_id` synchronously and return the awaitable that executes it."""
        raise NotImplementedError("This owner cannot execute runnable Cells")

    async def validate(self, key: str, validator: object) -> ValidatorResult:
        """Run one configured validator spec; `key` identifies this run across restarts."""
        raise NotImplementedError("This owner cannot run validators")

    async def recover(self) -> None:
        # Subclasses stop leftover work first. No dispatch happens before this finishes.
        for execution in self.session.pending():
            if execution.phase == "started":
                self.session.finish(
                    execution.cell_id,
                    {"type": "UnknownExecution", "reason": "owner_lost_before_result_commit"},
                )
        if not self.session.meta.terminal:
            self.session.fire_timers()
            await self.handle_controls()

    def clear(self) -> None:
        self.task = self.active_id = None

    async def stop_active(self) -> None:
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        self.clear()

    async def fail(self, reason: str, *, control_id: str | None = None) -> None:
        error = None
        try:
            await self.stop_active()
        except RecoveryFailure as exc:
            error = str(exc)
        for execution in list(self.session.pending()):
            if execution.phase == "queued":
                self.session.finish(execution.cell_id, {"type": "cancelled"})
            else:
                self.session.finish(
                    execution.cell_id, {"type": "UnknownExecution", "reason": reason}
                )
        self.session.end("failure", reason, cleanup_error=error, control_id=control_id)

    async def handle_controls(self) -> None:
        controls = sorted(
            self.session.data.controls.items(),
            key=lambda pair: (CONTROLS[pair[1].control.type].order, int(pair[0])),
        )
        for key, record in controls:
            if record.handled:
                continue
            control = record.control
            await self.CONTROL_HANDLERS[control.type](self, key, control)
            if self.session.meta.terminal:
                break

    async def _cancel(self, key: str, control: ControlRequest) -> None:
        target = control.target_cell_id
        assert target is not None
        execution = self.session.data.executions[str(target)]
        if execution.phase == "started":
            # recover() finishes leftover work, so a started execution is the active one.
            if target != self.active_id:
                raise RuntimeError(f"Started Cell {target} is not the active execution")
            await self.stop_active()
            self.session.finish(
                target, {"type": "UnknownExecution", "reason": "cancelled_after_launch"}
            )
        elif execution.phase == "queued":
            self.session.finish(target, {"type": "cancelled"})
        self.session.mark_handled(key, "accepted")

    async def _resolve(self, key: str, control: ControlRequest) -> None:
        self.session.mark_handled(key, "accepted", resolution=control.target_cell_id)

    async def _conclude(self, key: str, control: ControlRequest) -> None:
        if control.result == "failure":
            await self.fail(control.reason, control_id=key)
            return
        blocker = await self._success_blocker(key)
        if blocker:
            self.session.mark_handled(key, blocker)
        else:
            self.session.end("success", control.reason, control_id=key)

    # One handler per state.CONTROLS entry.
    CONTROL_HANDLERS = {"cancel": _cancel, "resolution": _resolve, "conclusion": _conclude}

    async def _success_blocker(self, control_id: str) -> str | None:
        self.session.fire_timers()
        if (
            self.session.pending()
            or self.session.unresolved()
            or any(not t.fired for t in self.session.data.timers.values())
        ):
            return "Success requires no pending work or unresolved uncertainty"
        if any(not m.consumed for m in self.session.data.inbox.values()):
            return "New observations must be consumed before success"
        # Validators execute afresh at each conclusion; the model cannot edit their specs.
        # A result recorded before a restart is reused rather than rerun.
        for index, validator in enumerate(self.session.meta.validators):
            key = f"{control_id}:{index}"
            result = self.session.data.validator_results.get(key)
            if result is None:
                result = await self.validate(key, validator)
                self.session.record_validator_result(key, result)
            if not result.passed:
                return f"Required validator {index} failed: {result.output}"
        self.session.fire_timers()
        if any(not m.consumed for m in self.session.data.inbox.values()):
            return "Observations arrived during validation"
        return None

    async def wait(self, until: float = math.inf, request: asyncio.Task | None = None) -> None:
        """Wait for a commit, the active task ending, the next timer or `until`, or `request`.

        Wake sources: execution output and results, timer registrations and inbox input all
        commit; timer deadlines and retry backoff are deadlines; the model call is `request`.
        """
        timers = (t.deadline for t in self.session.data.timers.values() if not t.fired)
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
        await self.handle_controls()
        if self.session.meta.terminal or self.session.meta.pause or self.task:
            return
        pending = self.session.pending()
        if pending:
            cell_id = pending[0].cell_id
            self.task = asyncio.create_task(self.launch(cell_id))
            self.active_id = cell_id
            self.task.add_done_callback(lambda _: self.changed.set())


async def replay(
    owner: Owner,
    publish: Callable[[JournalEvent], None] | None = None,
) -> bool:
    """Recover leftover work and publish a terminal session without a model provider."""
    session = owner.session
    if not session.meta.terminal:
        raise ValueError("Only terminal sessions can be replayed")
    await owner.recover()
    if publish:
        for event in session.store.events():
            publish(event)
    terminal = session.meta.terminal
    assert terminal is not None
    return terminal.result == "success"


async def run(
    owner: Owner,
    provider: ModelProvider,
    model: str,
    publish: Callable[[JournalEvent], None] | None = None,
) -> bool:
    session = owner.session
    published = 0

    def flush() -> None:
        nonlocal published
        for event in session.store.events(published):
            if publish:
                publish(event)
            published = event.seq

    async with owner:
        flush()
        while not session.meta.terminal:
            await owner.tick()
            flush()
            if session.meta.terminal:
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
                try:
                    ops = await _request_ops(owner, turn, provider, model, flush)
                except ProviderFailure as exc:
                    session.provider_failed(
                        turn.id, str(exc), retryable=exc.retryable, retry_after=exc.retry_after
                    )
                else:
                    session.accept(turn.id, ops)
                    session.request_work()
                    await owner.handle_controls()
            except InvalidPatch as exc:
                session.reject(turn.id, str(exc))
            except RunStopped as exc:
                await owner.fail(str(exc))
            flush()
        flush()
    terminal = session.meta.terminal
    assert terminal is not None
    return terminal.result == "success"


def _idle_action(owner: Owner) -> Literal["ready", "wait", "invalid", "fail"]:
    session = owner.session
    turns = session.data.turns.values()
    awaiting_step = not turns or any(t.receipt is None for t in turns)
    # Successful completions wait for the queue to drain; a failure pauses it and wakes.
    batching = bool(session.pending()) and not session.meta.pause
    has_news = any(
        not m.consumed and not (batching and m.kind == "execution")
        for m in session.data.inbox.values()
    )
    if awaiting_step or has_news:
        return "ready"
    has_wake_source = (
        owner.task
        or batching
        or any(not t.fired for t in session.data.timers.values())
        or session.external_wakeup
    )
    if has_wake_source:
        return "wait"
    if session.meta.idle >= session.limits.invalid_attempts:
        return "fail"
    return "invalid"


async def _request_ops(
    owner: Owner,
    turn: Turn,
    provider: ModelProvider,
    model: str,
    flush: Callable[[], None],
) -> object:
    """Return the ops of one complete step call, reusing a saved response after restart."""
    session = owner.session
    if turn.response is not None:
        response = AssistantMessage.model_validate(turn.response)
    else:
        while time.time() < turn.retry_at and not session.meta.terminal:
            await owner.wait(turn.retry_at)
            await owner.tick()
            flush()
        turn = session.start_attempt(turn.id)
        messages = [UserMessage(content=encode_state(turn.state))]
        if turn.error:
            messages.append(UserMessage(content="Rejected step: " + turn.error))
        # No State changes during generation. Owner work only appends runtime records.
        request = asyncio.create_task(
            provider.complete(
                model=model,
                system=turn.system,
                messages=messages,
                tools=[step_tool(turn.tool_schema)],
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
                # No status to classify, so retry within the provider budget.
                raise ProviderFailure(str(exc) or type(exc).__name__) from exc
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
        session.record_response(
            turn.id, response.model_dump(mode="json"), max(0, tokens), known=tokens > 0
        )
    if response.stop_reason == "error":
        details = _error_details(response)
        status = details.get("status_code")
        retry_after = details.get("retry_after_seconds")
        raise ProviderFailure(
            response.error_message or "Provider failed",
            # Only an HTTP error status that retrying cannot fix, such as 401 or 404, is final.
            retryable=not isinstance(status, int) or is_transient_status(status),
            retry_after=float(retry_after) if isinstance(retry_after, int | float) else None,
        )
    if len(response.tool_calls) != 1 or response.tool_calls[0].name != "step":
        raise InvalidPatch("Call exactly one step tool")
    arguments = response.tool_calls[0].arguments
    if raw_tool_arguments(arguments) is not None or set(arguments) != {"ops"}:
        raise InvalidPatch("step requires one complete ops argument")
    return arguments["ops"]


def _error_details(response: AssistantMessage) -> dict:
    """Return the provider error details carrying an HTTP status, if any."""
    for diagnostic in response.diagnostics or ():
        details = diagnostic.details or {}
        if isinstance(details.get("status_code"), int):
            return details
    return {}
