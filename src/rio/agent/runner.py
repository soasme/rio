"""Fixed model rounds with persisted attempts, usage, retries, and run endings."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from dataclasses import replace

from rio.agent.owner import Owner
from rio.agent.prompt import STEP
from rio.agent.session import Session, observation
from rio.agent.shapes import JournalEvent
from rio.agent.state import InvalidPatch
from rio.agent.store import encode
from rio.ai.events import AssistantDoneEvent, AssistantErrorEvent
from rio.ai.messages import AssistantMessage, UserMessage, raw_tool_arguments
from rio.ai.provider import ModelProvider


async def run(
    session: Session,
    provider: ModelProvider,
    model: str,
    publish: Callable[[JournalEvent], None] | None = None,
) -> bool:
    published = 0

    def flush():
        nonlocal published
        for event in session.store.events(published):
            if publish:
                publish(event)
            published = event["seq"]

    async with Owner(session) as owner:
        flush()
        while not session.meta["terminal"]:
            await owner.tick()
            flush()
            if session.meta["terminal"]:
                break
            unaccepted = any(t["receipt"] is None for t in session.data["turns"].values())
            # Successful completions wait for the queue to drain; a failure pauses it and wakes.
            batching = session.pending() and not session.meta["pause"]
            new_input = any(
                not m["consumed"] and not (batching and m["kind"] == "execution")
                for m in session.data["inbox"].values()
            )
            if session.data["turns"] and not unaccepted and not new_input:
                wakeup = (
                    owner.task
                    or batching
                    or any(not t["fired"] for t in session.data["timers"].values())
                )
                if wakeup or session.limits.external_wakeup:
                    await owner.wait()
                    continue
                if session.meta.get("idle", 0) >= session.limits.invalid_attempts:
                    await owner.fail("Repeated invalid idle decisions")
                    break
                seq = session.store.sequence + 1
                message_id = f"idle:{seq}"
                session.store.commit(
                    "invalid_idle",
                    {
                        "meta": {"idle": session.meta.get("idle", 0) + 1},
                        "inbox": {
                            message_id: observation(
                                message_id,
                                "invalid_idle",
                                {
                                    "reason": (
                                        "No work or wake-up source remains; "
                                        "conclude or request work"
                                    )
                                },
                                seq,
                            )
                        },
                    },
                )
            try:
                turn = session.prepare_turn(session.system, session.schema)
                if turn["response"] is None:
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
                        turn["id"],
                        response.model_dump(mode="json"),
                        max(0, tokens),
                        known=tokens > 0,
                    )
                else:
                    response = AssistantMessage.model_validate(turn["response"])
                if response.stop_reason in ("error", "aborted"):
                    raise InvalidPatch(response.error_message or "Provider failed")
                if len(response.tool_calls) != 1 or response.tool_calls[0].name != "step":
                    raise InvalidPatch("Call exactly one step tool")
                arguments = response.tool_calls[0].arguments
                if raw_tool_arguments(arguments) is not None or set(arguments) != {"ops"}:
                    raise InvalidPatch("step requires one complete ops argument")
                session.accept(turn["id"], arguments["ops"])
                if session.pending():
                    session.store.commit("work_requested", {"meta": {"idle": 0}})
                await owner.controls()
            except InvalidPatch as exc:
                session.reject(turn["id"], str(exc))
            except ValueError as exc:
                await owner.fail(str(exc))
            flush()
        flush()
    return session.meta["terminal"]["result"] == "success"


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
