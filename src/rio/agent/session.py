"""Cell-boundary commit protocol. External work belongs to the owner, never the reducer."""

from __future__ import annotations

import copy
import time
import uuid
from collections.abc import Iterable
from dataclasses import asdict
from typing import cast

from rio.agent.ledger import RuntimeLedger
from rio.agent.prompt import schema, system
from rio.agent.shapes import (
    ControlRequest,
    Projection,
    Receipt,
    State,
    Turn,
)
from rio.agent.state import CONTROLS, NOTE, CellKind, InvalidPatch, Limits, admit
from rio.agent.store import Store, encode

# Bump when the model-facing step protocol changes; saved turns hold the old prompt.
STATE_SCHEMA = "rio.state/4"


class RunStopped(RuntimeError):
    """A run cannot continue because its budget or lifecycle disallows another turn."""


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

    def __init__(self, store: Store, kinds: Iterable[CellKind] = ()):
        if not store.data:
            raise ValueError("Create a Session before opening it")
        meta = cast(Projection, store.data)["meta"]
        if meta["state"]["schema"] != STATE_SCHEMA:
            raise ValueError(
                f"Session uses {meta['state']['schema']}, but this Rio needs "
                f"{STATE_SCHEMA}; start a new session"
            )
        self.external_wakeup = meta["external_wakeup"]
        super().__init__(store, Limits(**meta["limits"]))
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
        state: State = {
            "schema": STATE_SCHEMA,
            "id": uuid.uuid4().hex,
            "revision": 0,
            "goal": goal,
            "cells": [],
            "runtime": {"next_cell_id": 1, "cells": {}, "inbox": [], "usage": {}},
        }
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
                    "usage": {"turns": 0, "attempts": 0, "tokens": 0, "unknown_usage": 0},
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
        return copy.deepcopy(self.meta["state"])

    @property
    def system(self) -> str:
        return system(self.kinds.values()) + self.meta.get("instructions", "")

    @property
    def schema(self) -> dict:
        return schema(self.kinds.values())

    def prepare_turn(self) -> Turn:
        if self.meta["terminal"]:
            raise RunStopped("Terminal runs cannot resume")
        for turn in self.data["turns"].values():
            if turn["receipt"] is None:
                return copy.deepcopy(turn)
        if self.store.journal_bytes >= self.limits.storage_bytes:
            raise RunStopped("Session storage budget exhausted")
        usage = self.meta["usage"]
        if usage["turns"] >= self.limits.turns or usage["tokens"] >= self.limits.tokens:
            raise RunStopped("Model budget exhausted")
        state = self.state
        state["revision"] += 1
        selected = {str(c["id"]) for c in state["cells"]}
        unresolved = set(self.unresolved())
        state["runtime"]["cells"] = {
            key: execution
            for key, execution in self.data["executions"].items()
            if key in selected or execution["phase"] != "finished" or int(key) in unresolved
        }
        for key, execution in state["runtime"]["cells"].items():
            if key in self.data["resolutions"]:
                execution["resolved_by"] = self.data["resolutions"][key]
        # Selected output competes with new observations. Keep receipts compact; full records
        # stay available through read-only SQLite retrieval, even after context pruning.
        delivered = []
        state["runtime"]["inbox"] = []
        state["runtime"]["usage"] = dict(usage)
        context_bytes = len(encode(state).encode())
        for message in self.data["inbox"].values():
            if message["consumed"]:
                continue
            compact = {k: v for k, v in message.items() if k != "consumed"}
            message_bytes = len(encode(compact).encode()) + bool(delivered)
            if context_bytes + message_bytes > self.limits.context_bytes:
                break
            state["runtime"]["inbox"].append(compact)
            delivered.append(message["id"])
            context_bytes += message_bytes
        if context_bytes > self.limits.context_bytes:
            raise RunStopped("Runtime facts exceed reserved context")
        turn_id = uuid.uuid4().hex
        turn: Turn = {
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
        self.store.commit(
            "turn_prepared",
            {
                "meta": {
                    "state": state,
                    "usage": {**usage, "turns": usage["turns"] + 1},
                },
                "turns": {turn_id: turn},
            },
        )
        return copy.deepcopy(turn)

    def start_attempt(self, turn_id: str) -> Turn:
        turn = self.data["turns"][turn_id]
        if turn["receipt"] or self.meta["terminal"]:
            raise RunStopped("Turn already accepted or run ended")
        if turn["attempts"] >= self.limits.invalid_attempts:
            raise RunStopped("Model retry budget exhausted")
        usage = self.meta["usage"]
        if usage["tokens"] >= self.limits.tokens:
            raise RunStopped("Token budget exhausted")
        changes = {"attempts": turn["attempts"] + 1, "response": None}
        # Until a complete response commits, usage for this attempt is unknown.
        self.store.commit(
            "model_attempt",
            {
                "turns": {turn_id: changes},
                "meta": {
                    "usage": {
                        **usage,
                        "attempts": usage["attempts"] + 1,
                        "unknown_usage": usage["unknown_usage"] + 1,
                    }
                },
            },
        )
        return copy.deepcopy(self.data["turns"][turn_id])

    def record_response(
        self, turn_id: str, response: dict, tokens: int, *, known: bool = True
    ) -> None:
        usage = self.meta["usage"]
        self.store.commit(
            "model_response",
            {
                "turns": {turn_id: {"response": response}},
                "meta": {
                    "usage": {
                        **usage,
                        "tokens": usage["tokens"] + tokens,
                        "unknown_usage": usage["unknown_usage"] - int(known),
                    }
                },
            },
        )

    def reject(self, turn_id: str, reason: str) -> None:
        turn = self.data["turns"][turn_id]
        self.store.commit(
            "patch_rejected",
            {
                "turns": {
                    turn_id: {
                        "error": reason,
                        "response": None,
                        "retry_at": time.time() + min(2 ** turn["attempts"], 30),
                    }
                },
            },
        )

    def _admit_resolution(self, control: ControlRequest) -> None:
        if control["target_cell_id"] not in self.unresolved():
            raise InvalidPatch(
                "Resolution applies only to unresolved UnknownExecution. "
                "For an ordinary error, omit the resolution control; the next accepted "
                "step consumes the error and resumes dispatch."
            )
        for ref in control["evidence_refs"]:
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

    def accept(self, turn_id: str, ops: list) -> Receipt:
        turn = self.data["turns"][turn_id]
        if turn["receipt"] is not None:
            if turn["ops"] != ops:
                raise InvalidPatch("Conflicting submission for an accepted turn")
            return copy.deepcopy(turn["receipt"])
        if self.meta["terminal"]:
            raise InvalidPatch("Run has ended")
        if turn["state"]["revision"] != self.meta["state"]["revision"]:
            raise InvalidPatch("Stale turn: State changed after this turn was prepared")
        state, new, requests = admit(
            turn["state"],
            ops,
            self.data["executions"],
            self.limits.context_bytes // 2,
            self.kinds,
        )
        runs = [cell for cell in new if cell["kind"] != "note"]
        if len(self.pending()) + len(self.unresolved()) + len(runs) > self.limits.pending_cells:
            raise InvalidPatch("Too much outstanding work or uncertainty")
        for control in requests:
            target = control.get("target_cell_id")
            targets = "target_cell_id" in CONTROLS[control["type"]].fields
            if targets and str(target) not in self.data["executions"]:
                raise InvalidPatch(
                    f"{control['type']} control: Cell {target} has no recorded execution; "
                    "target an accepted runnable Cell"
                )
            if check := self.CONTROL_CHECKS.get(control["type"]):
                check(self, control)
        # Controls never leave the journal, so ids count up from the recorded ones.
        first = len(self.data["controls"]) + 1
        controls = {
            str(first + index): {"control": control, "handled": False}
            for index, control in enumerate(requests)
        }
        receipt: Receipt = {
            "turn_id": turn_id,
            "revision": state["revision"],
            "cell_ids": [c["id"] for c in new],
            "control_ids": [int(key) for key in controls],
            "event": self.store.sequence + 1,
        }
        executions = {
            str(c["id"]): {
                "cell_id": c["id"],
                "phase": "queued",
                "output": {},
                "truncated": False,
                "accepted": self.store.sequence + 1,
            }
            for c in runs
        }
        messages = {mid: {**self.data["inbox"][mid], "consumed": True} for mid in turn["delivered"]}
        pause = self.meta["pause"]
        self.store.commit(
            "patch_accepted",
            {
                "meta": {
                    "state": {
                        **{
                            key: value
                            for key, value in state.items()
                            if key != "runtime" and value != turn["state"][key]
                        },
                        "runtime": {
                            key: value
                            for key, value in state["runtime"].items()
                            if value != turn["state"]["runtime"][key]
                        },
                    },
                    "pause": None if pause in messages else pause,
                },
                "cells": {str(c["id"]): c for c in new},
                "executions": executions,
                "controls": controls,
                "inbox": messages,
                "turns": {turn_id: {"receipt": receipt, "ops": ops}},
            },
            identity=f"patch:{turn_id}",
        )
        return receipt
