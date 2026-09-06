"""Translate runtime events to a bounded display stream."""

import json
from copy import deepcopy

from rio_agent.events import (
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
from rio_coding.events import (
    AgentSettledEvent,
    AutoRetryEndEvent,
    AutoRetryStartEvent,
    CodingSessionEvent,
    QueueUpdateEvent,
    SessionRunEndEvent,
    StateRestoredEvent,
)
from rio_coding.tui.state import StepStreamItem, TuiState

_MAX_RESULT_CHARS = 8000


class TuiEventAdapter:
    def __init__(
        self, state: TuiState | None = None, *, toggle_tool_results: str = "ctrl+o"
    ) -> None:
        self.state = state if state is not None else TuiState()
        self.toggle_tool_results = toggle_tool_results
        self._action_target = ""

    def consume(self, event: CodingSessionEvent) -> list[StepStreamItem]:
        items: list[StepStreamItem] = []
        if isinstance(event, RunStartEvent):
            self.state.running = True
        if isinstance(event, StepStartEvent):
            self.state.step = event.step
        if isinstance(
            event,
            (
                StepStartEvent,
                StateUpdateEvent,
                StepEndEvent,
                RunEndEvent,
                SessionRunEndEvent,
                StateRestoredEvent,
            ),
        ):
            self.state.state = deepcopy(event.state)
        if isinstance(event, StateUpdateEvent):
            items.append(StepStreamItem("step", "State delta: " + json.dumps(event.delta)))
        elif isinstance(event, ActionStartEvent):
            target = event.arguments.get("path") or event.arguments.get("command")
            self._action_target = " ".join(target.split())[:120] if isinstance(target, str) else ""
            command = event.arguments.get("command")
            label = (
                command
                if isinstance(command, str) and command.strip()
                else (f"{event.name}({json.dumps(event.arguments, ensure_ascii=False)})")
            )
            self.state.active_action = label
            items.append(StepStreamItem("step", f"Running {label}"))
        elif isinstance(event, ActionEndEvent):
            self.state.active_action = None
            text = event.result.text
            target = f": {self._action_target}" if self._action_target else ""
            self._action_target = ""
            lines = len(text.splitlines())
            count = f"{lines} {'line' if lines == 1 else 'lines'}"
            error = ", error" if event.is_error else ""
            items.append(
                StepStreamItem(
                    "error" if event.is_error else "step",
                    f"{event.name}{target} ({count}{error}, {self.toggle_tool_results} to expand)",
                    continuation=True,
                    full_text=f"{event.name}: {text[:_MAX_RESULT_CHARS]}"
                    + ("\n… output truncated" if len(text) > _MAX_RESULT_CHARS else ""),
                )
            )
        elif isinstance(event, ReasoningDiscardedEvent):
            items.append(StepStreamItem("reasoning", f"Reasoning (discarded): {event.reasoning}"))
        elif isinstance(event, ValidationErrorEvent):
            items.append(
                StepStreamItem(
                    "validation_retry", f"Validation retry {event.attempt}: {event.error}"
                )
            )
        elif isinstance(event, AutoRetryStartEvent):
            items.append(StepStreamItem("status", f"Retrying: {event.error_message}"))
        elif isinstance(event, AutoRetryEndEvent) and not event.success:
            items.append(StepStreamItem("error", event.final_error or "Retry failed"))
        elif isinstance(event, SessionRunEndEvent) and event.answer:
            items.append(StepStreamItem("custom", event.answer))
        elif isinstance(event, QueueUpdateEvent):
            self.state.queued = len(event.steering) + len(event.follow_up)
        elif isinstance(event, StateRestoredEvent):
            items.append(StepStreamItem("status", "Restored checkpoint " + str(event.entry_id)))
        if isinstance(event, (SessionRunEndEvent, AgentSettledEvent)):
            self.state.running = False
            self.state.active_action = None
        self.state.items.extend(items)
        del self.state.items[:-1000]
        return items
