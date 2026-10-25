"""Cell-boundary commit protocol. External work belongs to the owner, never the reducer."""

from __future__ import annotations

import copy
import time
import uuid
from collections.abc import Iterable
from dataclasses import asdict

from rio.agent.ledger import RuntimeLedger, inbox_view, runtime_id
from rio.agent.prompt import schema, system
from rio.agent.shapes import (
    ControlRequest,
    Observation,
    Receipt,
    RuntimeCell,
    State,
    Turn,
)
from rio.agent.state import CONTROLS, NOTE, CellKind, InvalidPatch, Limits, admit, zero_usage
from rio.agent.store import Store, encode
from rio.truncate import truncate_bytes_tail

# Bump when the model-facing step protocol changes; saved turns hold the old prompt.
STATE_SCHEMA = "rio.state/6"
# Longest wait between failed provider requests.
PROVIDER_BACKOFF_SECONDS = 120


class RunStopped(RuntimeError):
    """A run cannot continue because its budget or lifecycle disallows another turn."""


def size(value: object) -> int:
    """Bytes of a value in the model-facing JSON."""
    return len(encode(value).encode())


def shares(sizes: list[int], budget: int) -> list[int]:
    """Max-min fair caps: sizes within an equal share stay whole; the rest split what is left."""
    caps = list(sizes)
    order = sorted(range(len(sizes)), key=sizes.__getitem__)
    for rank, index in enumerate(order):
        share = budget // (len(order) - rank)
        if sizes[index] > share:
            for other in order[rank:]:
                caps[other] = share
            break
        budget -= sizes[index]
    return caps


def json_tail(text: str, cap: int) -> str:
    """The longest tail whose JSON string body fits in `cap` bytes."""
    tail = truncate_bytes_tail(text, cap)
    while (excess := size(tail) - 2 - cap) > 0:
        tail = truncate_bytes_tail(tail, len(tail.encode()) - excess)
    return tail


def context_notice(seq: int, cell_ids: list[int]) -> Observation:
    """An observation, recorded only in its turn_prepared event, naming Cells left out."""
    return Observation.model_validate(
        {
            "id": runtime_id("context", seq),
            "kind": "context",
            "payload": {
                "cell_ids": cell_ids,
                "reason": "Runtime records of these Cells exceed the context budget; remove Cells",
            },
            "seq": seq,
        }
    )


def by_name(kinds: Iterable[CellKind]) -> dict[str, CellKind]:
    """The built-in note, then the caller's kinds, by name."""
    named = {kind.name: kind for kind in kinds}
    if "note" in named:
        raise ValueError("note is a built-in Cell kind")
    named = {"note": NOTE, **named}
    # All kinds share one cell schema in the step tool, so a field means one thing.
    fields: dict[str, dict] = {}
    for kind in named.values():
        for field, spec in kind.fields.items():
            if field in ("kind", "id", "previous_id") or fields.setdefault(field, spec) != spec:
                raise ValueError(f"Cell kind {kind.name} redefines field {field}")
    return named


class Session(RuntimeLedger):
    """Create sessions and admit model turns over the runtime ledger."""

    def __init__(self, store: Store, kinds: Iterable[CellKind] = ()) -> None:
        if not store.data:
            raise ValueError("Create a Session before opening it")
        meta = store.projection.meta
        if meta.state.schema_version != STATE_SCHEMA:
            raise ValueError(
                f"Session uses {meta.state.schema_version}, but this Rio needs "
                f"{STATE_SCHEMA}; start a new session"
            )
        self.external_wakeup = meta.external_wakeup
        super().__init__(store, Limits(**meta.limits))
        self.kinds = by_name(kinds)

    @classmethod
    def create(
        cls,
        store: Store,
        goal: str,
        *,
        kinds: Iterable[CellKind] = (),
        limits: Limits | None = None,
        validators: Iterable = (),
        owner_config: dict | None = None,
        model_config: dict | None = None,
        instructions: str = "",
        external_wakeup: bool = False,
    ) -> Session:
        if store.data:
            raise ValueError("Session already exists")
        kinds = list(kinds)
        by_name(kinds)
        limits = limits or Limits()
        state: State = State.model_validate(
            {
                "schema": STATE_SCHEMA,
                "id": uuid.uuid4().hex,
                "revision": 0,
                "goal": goal,
                "cells": [],
                "stale": [],
                "runtime": {"next_cell_id": 1, "cells": {}, "inbox": [], "usage": zero_usage()},
            }
        )
        if len(encode(state).encode()) > limits.context_bytes // 2:
            raise ValueError("Goal exceeds reserved context")
        store.commit(
            "session_created",
            {
                "meta": {
                    "state": state,
                    "limits": asdict(limits),
                    "external_wakeup": external_wakeup,
                    "validators": list(validators),
                    "owner_config": owner_config or {},
                    "model_config": model_config or {},
                    "instructions": instructions,
                    "terminal": None,
                    "pause": None,
                    "usage": zero_usage(),
                    "idle": 0,
                },
                "cells": {},
                "executions": {},
                "inbox": {},
                "turns": {},
                "controls": {},
                "timers": {},
                "resolutions": {},
                "validator_results": {},
            },
        )
        return cls(store, kinds)

    @property
    def state(self) -> State:
        return copy.deepcopy(self.meta.state)

    @property
    def system(self) -> str:
        return system(self.kinds.values()) + self.meta.instructions

    @property
    def schema(self) -> dict:
        return schema(self.kinds.values())

    def _runtime_cell(self, key: str, output: dict[str, str]) -> RuntimeCell:
        """A copy of an execution showing `output`, which tails its recorded streams."""
        execution = self.data.executions[key]
        cell: RuntimeCell = RuntimeCell.model_validate(
            {**execution.model_dump(exclude_unset=True), "output": output}
        )
        if key in self.data.resolutions:
            cell.resolved_by = self.data.resolutions[key]
        if omitted := {
            name: count
            for name, text in execution.output.items()
            if (count := len(text.encode()) - len(output[name].encode()))
        }:
            cell.omitted = omitted
        return cell

    def _fit_runtime(self, state: State) -> list[str]:
        """Fill state's runtime cells and inbox within the context budget.

        Returns the ids of the inbox messages delivered.
        """
        executions = self.data.executions
        selected = {str(c.id) for c in state.cells if c.id not in state.stale}
        unresolved = {str(cell_id) for cell_id in self.unresolved()}
        outstanding = [
            key for key, e in executions.items() if e.phase != "finished" or key in unresolved
        ]
        shown = [key for key in executions if key in selected or key in outstanding]
        # Reserve each record with all output omitted, plus the oldest message so the inbox
        # always drains. If that overflows, show only outstanding work, then none, with a
        # notice naming the Cells left out.
        limit = self.limits.context_bytes
        waiting = [inbox_view(m) for m in self.data.inbox.values() if not m.consumed]
        for kept in (shown, outstanding, []):
            outputs = {key: dict.fromkeys(executions[key].output, "") for key in kept}
            state.runtime.cells = {key: self._runtime_cell(key, outputs[key]) for key in kept}
            missing = [int(key) for key in shown if key not in kept]
            state.runtime.inbox = (
                [context_notice(self.store.sequence + 1, missing)] if missing else []
            )
            context_bytes = size(state)
            head = size(waiting[0]) + bool(missing) if waiting else 0
            if context_bytes + head <= limit:
                break
        else:
            raise RunStopped("Goal and Cells exceed context without runtime facts")
        delivered = []
        for message in waiting:
            message_bytes = size(message) + bool(state.runtime.inbox)
            if context_bytes + message_bytes > limit:
                break
            state.runtime.inbox.append(message)
            delivered.append(message.id)
            context_bytes += message_bytes
        # Output fills the rest: streams within an equal share stay whole, larger ones keep
        # a tail. The journal keeps the recorded output; see prompt.EXECUTION.
        streams = [(key, name) for key in kept for name in executions[key].output]
        texts = [executions[key].output[name] for key, name in streams]
        caps = shares([size(text) - 2 for text in texts], limit - context_bytes)
        for (key, name), text, cap in zip(streams, texts, caps, strict=True):
            outputs[key][name] = json_tail(text, cap)
        state.runtime.cells = {key: self._runtime_cell(key, outputs[key]) for key in kept}
        return delivered

    def prepare_turn(self) -> Turn:
        if self.meta.terminal:
            raise RunStopped("Terminal runs cannot resume")
        for turn in self.data.turns.values():
            if turn.receipt is None:
                return copy.deepcopy(turn)
        if self.store.journal_bytes >= self.limits.storage_bytes:
            raise RunStopped("Session storage budget exhausted")
        usage = self.meta.usage
        if usage.turns >= self.limits.turns or usage.tokens >= self.limits.tokens:
            raise RunStopped("Model budget exhausted")
        state = self.state
        state.revision += 1
        state.runtime.usage = usage.model_copy(deep=True)
        delivered = self._fit_runtime(state)
        turn_id = uuid.uuid4().hex
        turn = Turn.model_validate(
            {
                "id": turn_id,
                "state": state,
                "delivered": delivered,
                "system": self.system,
                "tool_schema": self.schema,
                "attempts": 0,
                "retry_at": 0,
                "error": None,
                "receipt": None,
                "response": None,
            }
        )
        self.store.commit(
            "turn_prepared",
            {
                "meta": {
                    "state": state,
                    "usage": {**usage.model_dump(), "turns": usage.turns + 1},
                },
                "turns": {turn_id: turn},
            },
        )
        return copy.deepcopy(turn)

    def start_attempt(self, turn_id: str) -> Turn:
        turn = self.data.turns[turn_id]
        if turn.receipt or self.meta.terminal:
            raise RunStopped("Turn already accepted or run ended")
        if turn.attempts >= self.limits.invalid_attempts:
            raise RunStopped("Model retry budget exhausted")
        usage = self.meta.usage
        if usage.tokens >= self.limits.tokens:
            raise RunStopped("Token budget exhausted")
        changes = {"attempts": turn.attempts + 1, "response": None}
        # Until a complete response commits, usage for this attempt is unknown.
        self.store.commit(
            "model_attempt",
            {
                "turns": {turn_id: changes},
                "meta": {
                    "usage": {
                        **usage.model_dump(),
                        "attempts": usage.attempts + 1,
                        "unknown_usage": usage.unknown_usage + 1,
                    }
                },
            },
        )
        return copy.deepcopy(self.data.turns[turn_id])

    def record_response(
        self, turn_id: str, response: dict, tokens: int, *, known: bool = True
    ) -> None:
        usage = self.meta.usage
        self.store.commit(
            "model_response",
            {
                "turns": {turn_id: {"response": response}},
                "meta": {
                    "usage": {
                        **usage.model_dump(),
                        "tokens": usage.tokens + tokens,
                        "unknown_usage": usage.unknown_usage - int(known),
                    }
                },
            },
        )

    def reject(self, turn_id: str, reason: str) -> None:
        turn = self.data.turns[turn_id]
        self.store.commit(
            "patch_rejected",
            {
                "turns": {
                    turn_id: {
                        "error": reason,
                        "response": None,
                        "retry_at": time.time() + min(2**turn.attempts, 30),
                    }
                },
            },
        )

    def provider_failed(
        self,
        turn_id: str,
        error: str,
        *,
        retryable: bool = True,
        retry_after: float | None = None,
    ) -> None:
        """Record a failed provider request; it does not use the invalid-step budget.

        The runner owns provider retries: each failure and its backoff is journaled
        here. A provider's Retry-After lengthens the backoff, within the same cap.
        """
        turn = self.data.turns[turn_id]
        failures = (turn.provider_failures or 0) + 1
        backoff = min(max(2**failures, retry_after or 0), PROVIDER_BACKOFF_SECONDS)
        self.store.commit(
            "provider_failed",
            {
                "turns": {
                    turn_id: {
                        # Refund the attempt; its usage stays unknown.
                        "attempts": turn.attempts - 1,
                        "provider_failures": failures,
                        "provider_error": error,
                        "response": None,
                        "retry_at": time.time() + backoff,
                    }
                },
            },
        )
        if not retryable:
            raise RunStopped(f"Provider request failed: {error}")
        if failures >= self.limits.provider_attempts:
            raise RunStopped(f"Provider retry budget exhausted: {error}")

    def _admit_resolution(self, control: ControlRequest) -> None:
        if control.target_cell_id not in self.unresolved():
            raise InvalidPatch(
                "Resolution applies only to unresolved UnknownExecution. "
                "For an ordinary error, omit the resolution control; the next accepted "
                "step consumes the error and resumes dispatch."
            )
        assert control.evidence_refs is not None
        for ref in control.evidence_refs:
            if not isinstance(ref, str) or not ref.startswith("session:event:"):
                raise InvalidPatch("Evidence must reference recorded Session events")
            try:
                seq = int(ref.rsplit(":", 1)[1])
            except ValueError:
                raise InvalidPatch("Invalid evidence reference") from None
            if self.store.event_kind(seq) not in (
                "execution_finished",
                "inbox_received",
                "validator_result",
            ):
                raise InvalidPatch("Evidence must reference a recorded observation")

    # Accept-time checks for state.CONTROLS that need recorded Session state.
    CONTROL_CHECKS = {"resolution": _admit_resolution}

    def accept(self, turn_id: str, ops: object) -> Receipt:
        turn = self.data.turns[turn_id]
        if turn.receipt is not None:
            if turn.ops != ops:
                raise InvalidPatch("Conflicting submission for an accepted turn")
            return copy.deepcopy(turn.receipt)
        if self.meta.terminal:
            raise InvalidPatch("Run has ended")
        if turn.state.revision != self.meta.state.revision:
            raise InvalidPatch("Stale turn: State changed after this turn was prepared")
        state, new, requests = admit(
            turn.state,
            ops,
            self.data.executions,
            self.limits.context_bytes // 2,
            self.kinds,
        )
        runs = [cell for cell in new if cell.kind != "note"]
        if len(self.pending()) + len(self.unresolved()) + len(runs) > self.limits.pending_cells:
            raise InvalidPatch("Too much outstanding work or uncertainty")
        if runs and any(control.type == "conclusion" for control in requests):
            raise InvalidPatch(
                "A conclusion ends the run before Cells added in this step can run; "
                "conclude in a later step, after their observations arrive"
            )
        for control in requests:
            target = control.target_cell_id
            targets = "target_cell_id" in CONTROLS[control.type].fields
            if targets and str(target) not in self.data.executions:
                raise InvalidPatch(
                    f"{control.type} control: Cell {target} has no recorded execution; "
                    "target an accepted runnable Cell"
                )
            if check := self.CONTROL_CHECKS.get(control.type):
                check(self, control)
        # Controls never leave the journal, so ids count up from the recorded ones.
        first = len(self.data.controls) + 1
        controls = {
            str(first + index): {"control": control, "handled": False}
            for index, control in enumerate(requests)
        }
        receipt: Receipt = Receipt.model_validate(
            {
                "turn_id": turn_id,
                "revision": state.revision,
                "cell_ids": [c.id for c in new],
                "control_ids": [int(key) for key in controls],
                "event": self.store.sequence + 1,
            }
        )
        executions = {
            str(c.id): {
                "cell_id": c.id,
                "phase": "queued",
                "output": {},
                "truncated": False,
                "accepted": self.store.sequence + 1,
            }
            for c in runs
        }
        messages = {
            mid: {**self.data.inbox[mid].model_dump(), "consumed": True} for mid in turn.delivered
        }
        pause = self.meta.pause
        current = state.model_dump(by_alias=True, exclude_unset=True)
        previous = turn.state.model_dump(by_alias=True, exclude_unset=True)
        self.store.commit(
            "patch_accepted",
            {
                "meta": {
                    "state": {
                        **{
                            key: value
                            for key, value in current.items()
                            if key != "runtime" and value != previous[key]
                        },
                        "runtime": {
                            key: value
                            for key, value in current["runtime"].items()
                            if value != previous["runtime"][key]
                        },
                    },
                    "pause": None if pause in messages else pause,
                },
                "cells": {str(c.id): c for c in new},
                "executions": executions,
                "controls": controls,
                "inbox": messages,
                "turns": {turn_id: {"receipt": receipt, "ops": ops}},
            },
            identity=f"patch:{turn_id}",
        )
        return receipt
