"""Cell-boundary commit protocol. External work belongs to the owner, never the reducer."""

from __future__ import annotations

import copy
import math
import statistics
import time
import uuid
from collections.abc import Iterable
from dataclasses import asdict
from typing import cast

from rio.agent.prompt import schema, system
from rio.agent.shapes import (
    Execution,
    InboxMessage,
    Meta,
    Projection,
    Receipt,
    RunResult,
    State,
    Turn,
    ValidatorResult,
)
from rio.agent.state import NOTE, CellKind, InvalidPatch, Limits, admit
from rio.agent.store import Store, encode
from rio.truncate import truncate_bytes_tail

# Bump when the model-facing step protocol changes; saved turns hold the old prompt.
STATE_SCHEMA = "rio.state/3"


class RunStopped(RuntimeError):
    """A run cannot continue because its budget or lifecycle disallows another turn."""


def by_name(kinds: Iterable[CellKind]) -> dict[str, CellKind]:
    named = {kind.name: kind for kind in kinds}
    if "note" in named:
        raise ValueError("note is a built-in Cell kind")
    # All kinds share one cell schema in the step tool, so a field means one thing.
    fields = dict(NOTE)
    for kind in named.values():
        for field, spec in kind.fields.items():
            if field in ("kind", "id", "previous_id") or fields.setdefault(field, spec) != spec:
                raise ValueError(f"Cell kind {kind.name} redefines field {field}")
    return named


def observation(message_id: str, kind: str, payload: dict, seq: int) -> InboxMessage:
    return {"id": message_id, "kind": kind, "payload": payload, "seq": seq, "consumed": False}


class Session:
    def __init__(self, store: Store, kinds: Iterable[CellKind] = ()):
        self.store = store
        if not store.data:
            raise ValueError("Create a Session before opening it")
        if self.meta["state"]["schema"] != STATE_SCHEMA:
            raise ValueError(
                f"Session uses {self.meta['state']['schema']}, but this Rio needs "
                f"{STATE_SCHEMA}; start a new session"
            )
        self.external_wakeup = self.meta["external_wakeup"]
        self.limits = Limits(**self.meta["limits"])
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
        kinds = by_name(kinds)
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
                    "usage": {"rounds": 0, "attempts": 0, "tokens": 0, "unknown_usage": 0},
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
        return cls(store, kinds.values())

    @property
    def data(self) -> Projection:
        return cast(Projection, self.store.data)

    @property
    def meta(self) -> Meta:
        return self.data["meta"]

    @property
    def state(self) -> State:
        return copy.deepcopy(self.meta["state"])

    @property
    def system(self) -> str:
        return system(self.kinds.values()) + self.meta.get("instructions", "")

    @property
    def schema(self) -> dict:
        return schema(self.kinds.values())

    def pending(self) -> list[Execution]:
        return [e for e in self.data["executions"].values() if e["phase"] != "finished"]

    def unresolved(self) -> list[int]:
        return [
            int(key)
            for key, e in self.data["executions"].items()
            if e.get("uncertain") and key not in self.data["resolutions"]
        ]

    def receive(self, message_id: str, kind: str, payload: dict) -> None:
        if (
            not isinstance(message_id, str)
            or not message_id
            or message_id.startswith(("execution:", "control:", "timer:", "idle:"))
        ):
            raise ValueError(
                "External message IDs must be nonempty and outside reserved namespaces"
            )
        old = self.data["inbox"].get(message_id)
        if old:
            if old["kind"] != kind or old["payload"] != payload:
                raise ValueError("Conflicting message identity")
            return
        if len(encode(payload).encode()) > self.limits.output_bytes:
            raise ValueError("Observation exceeds output budget")
        if (
            sum(not m["consumed"] for m in self.data["inbox"].values())
            >= self.limits.inbox_messages
        ):
            raise ValueError("Inbox full; sender must retry")
        self.store.commit(
            "inbox_received",
            {
                "inbox": {
                    message_id: observation(message_id, kind, payload, self.store.sequence + 1)
                }
            },
        )

    def prepare_turn(self) -> Turn:
        if self.meta["terminal"]:
            raise RunStopped("Terminal runs cannot resume")
        for turn in self.data["turns"].values():
            if turn["receipt"] is None:
                return copy.deepcopy(turn)
        if self.store.journal_bytes >= self.limits.storage_bytes:
            raise RunStopped("Session storage budget exhausted")
        usage = self.meta["usage"]
        if usage["rounds"] >= self.limits.rounds or usage["tokens"] >= self.limits.tokens:
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
                "meta": {"usage": {**usage, "rounds": usage["rounds"] + 1}},
                "turns": {turn_id: turn},
            },
        )
        return copy.deepcopy(turn)

    def attempt(self, turn_id: str) -> Turn:
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

    def response(self, turn_id: str, response: dict, tokens: int, *, known: bool = True) -> None:
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
        state, new = admit(
            turn["state"],
            ops,
            self.data["executions"],
            self.limits.context_bytes // 2,
            self.kinds,
        )
        runs = [cell for cell in new if cell["kind"] != "note"]
        if len(self.pending()) + len(self.unresolved()) + len(runs) > self.limits.pending_cells:
            raise InvalidPatch("Too much outstanding work or uncertainty")
        for cell in new:
            if cell.get("role") in ("cancel", "resolution"):
                target = str(cell["target_cell_id"])
                if target not in self.data["executions"]:
                    raise InvalidPatch("Control target must be a recorded execution")
                if cell["role"] == "resolution":
                    if cell["target_cell_id"] not in self.unresolved():
                        raise InvalidPatch(
                            "Resolution notes apply only to unresolved UnknownExecution. "
                            "For an ordinary error, omit the resolution note; the next accepted "
                            "step consumes the error and resumes dispatch."
                        )
                    for ref in cell["evidence_refs"]:
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
        receipt: Receipt = {
            "turn_id": turn_id,
            "revision": state["revision"],
            "cell_ids": [c["id"] for c in new],
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
        controls = {
            str(c["id"]): {"cell": c, "handled": False}
            for c in new
            if c.get("role") in ("cancel", "resolution", "conclusion")
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

    def invalid_idle(self) -> None:
        seq = self.store.sequence + 1
        message_id = f"idle:{seq}"
        self.store.commit(
            "invalid_idle",
            {
                "meta": {"idle": self.meta.get("idle", 0) + 1},
                "inbox": {
                    message_id: observation(
                        message_id,
                        "invalid_idle",
                        {"reason": "No work or wake-up source remains; conclude or request work"},
                        seq,
                    )
                },
            },
        )

    def request_work(self) -> None:
        if self.pending():
            self.store.commit("work_requested", {"meta": {"idle": 0}})

    def record(self, collection: str, key: str, fields: dict) -> None:
        """Merge owner-defined fields into an owner collection, outside the core records."""
        if collection == "output_chunks" or collection in Projection.__annotations__:
            raise ValueError(f"{collection} is a Session collection")
        old = self.data.get(collection, {}).get(key, {})
        self.store.commit("owner_record", {collection: {key: {**old, **fields}}})

    def truncate_output(self, cell_id: int, output: dict[str, str]) -> None:
        """Replace the given streams with final text, such as a tail and a pointer to more."""
        key = str(cell_id)
        execution = self.data["executions"][key]
        self.store.commit(
            "output_truncated",
            {
                "executions": {
                    key: {
                        **execution,
                        "output": {**execution["output"], **output},
                        "truncated": True,
                    }
                }
            },
        )

    def record_validator_result(self, key: str, result: ValidatorResult) -> None:
        self.store.commit("validator_result", {"validator_results": {key: result}})

    def authorize(self, cell_id: int) -> None:
        key = str(cell_id)
        execution = self.data["executions"][key]
        pending = self.pending()
        if (
            self.meta["terminal"]
            or self.meta["pause"]
            or execution["phase"] != "queued"
            or pending[0]["cell_id"] != cell_id
            or any(not c["handled"] for c in self.data["controls"].values())
        ):
            raise ValueError("Dispatch is blocked")
        self.store.commit(
            "execution_started",
            {"executions": {key: {**execution, "phase": "started", "started": time.time()}}},
        )

    def output(self, cell_id: int, chunks: dict[str, str]) -> None:
        """Append text to owner-named output streams, keeping each stream's tail."""
        key = str(cell_id)
        execution = self.data["executions"][key]
        if execution["phase"] != "started":
            raise ValueError("Output requires a started execution")
        limit = self.limits.output_bytes
        truncated = execution["truncated"] or any(
            len((execution["output"].get(name, "") + text).encode()) > limit
            for name, text in chunks.items()
        )
        chunks = {name: truncate_bytes_tail(text, limit) for name, text in chunks.items() if text}
        if chunks or truncated != execution["truncated"]:
            self.store.commit(
                "execution_output",
                {
                    "output_chunks": {
                        key: {"output": chunks, "truncated": truncated, "limit": limit}
                    }
                },
            )

    def finish(self, cell_id: int, result: dict) -> None:
        key = str(cell_id)
        execution = self.data["executions"][key]
        if execution["phase"] == "finished":
            return
        seq = self.store.sequence + 1
        mid = f"execution:{cell_id}"
        unknown = result.get("type") == "UnknownExecution"
        result = {**result, "cell_id": cell_id, "evidence_refs": [f"session:event:{seq}"]}
        execution = {
            **execution,
            "phase": "finished",
            "finished": time.time(),
            "result": result,
            "uncertain": unknown,
        }
        changes = {
            "executions": {key: execution},
            "inbox": {mid: observation(mid, "execution", result, seq)},
        }
        if result.get("type") not in ("success", "cancelled"):
            changes["meta"] = {"pause": mid}
        self.store.commit("execution_finished", changes, identity=mid)

    def register_timer(self, request_id: str, deadline: float, payload: dict) -> None:
        if (
            not isinstance(request_id, str)
            or not request_id
            or type(deadline) not in (int, float)
            or not math.isfinite(deadline)
            or not isinstance(payload, dict)
        ):
            raise ValueError("Timer requires a stable ID, finite deadline, and object payload")
        if self.meta["terminal"]:
            raise ValueError("Run has ended")
        record = {"id": request_id, "deadline": deadline, "payload": payload, "fired": False}
        old = self.data["timers"].get(request_id)
        if old:
            if old["deadline"] != deadline or old["payload"] != payload:
                raise ValueError("Conflicting timer generation")
            return
        if len(encode(payload).encode()) > self.limits.output_bytes:
            raise ValueError("Timer payload exceeds budget")
        if sum(not t["fired"] for t in self.data["timers"].values()) >= self.limits.pending_cells:
            raise ValueError("Too many timers")
        self.store.commit("timer_registered", {"timers": {request_id: record}})

    def fire_timers(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        for key, timer in list(self.data["timers"].items()):
            if not timer["fired"] and timer["deadline"] <= now:
                mid = f"timer:{key}"
                self.store.commit(
                    "timer_fired",
                    {
                        "timers": {key: {**timer, "fired": True}},
                        "inbox": {
                            mid: observation(
                                mid, "timer", timer["payload"], self.store.sequence + 1
                            )
                        },
                    },
                    identity=mid,
                )

    def handled(self, note_id: str, outcome: str, *, resolution: int | None = None) -> None:
        control = self.data["controls"][note_id]
        changes = {"controls": {note_id: {**control, "handled": True, "outcome": outcome}}}
        if resolution is not None:
            changes["resolutions"] = {str(resolution): int(note_id)}
        if outcome != "accepted":
            mid = f"control:{note_id}"
            changes["inbox"] = {
                mid: observation(
                    mid, "control_rejected", {"reason": outcome}, self.store.sequence + 1
                )
            }
        self.store.commit("control_handled", changes, identity=f"control:{note_id}")

    def end(
        self,
        result: RunResult,
        reason: str,
        *,
        cleanup_error: str | None = None,
        note_id: str | None = None,
    ) -> None:
        if self.meta["terminal"]:
            return
        changes = {
            "meta": {
                "terminal": {
                    "result": result,
                    "reason": reason,
                    "uncertainty": self.unresolved(),
                    "cleanup_error": cleanup_error,
                }
            },
            "metrics": {
                "commit_p50_seconds": statistics.median(self.store.commit_seconds or [0]),
                "commit_max_seconds": max(self.store.commit_seconds, default=0),
                "rebuild_seconds": self.store.rebuild_seconds,
                "journal_bytes": self.store.journal_bytes,
            },
        }
        if note_id is not None:
            changes["controls"] = {
                note_id: {**self.data["controls"][note_id], "handled": True, "outcome": "accepted"}
            }
        self.store.commit("run_ended", changes, identity="terminal")
