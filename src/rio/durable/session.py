"""Cell-boundary commit protocol. External work belongs to the owner, never the reducer."""

from __future__ import annotations

import copy
import math
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from rio.durable.state import InvalidPatch, Limits, admit
from rio.durable.store import Store, encode


def observation(message_id: str, kind: str, payload: dict, seq: int) -> dict:
    return {"id": message_id, "kind": kind, "payload": payload, "seq": seq, "consumed": False}


class Session:
    def __init__(self, store: Store):
        self.store = store
        if not store.data:
            raise ValueError("Create a Session before opening it")
        self.limits = Limits(**self.meta["limits"])

    @classmethod
    def create(
        cls,
        store: Store,
        goal: str,
        cwd: Path,
        *,
        limits: Limits | None = None,
        validators: tuple[tuple[str, ...], ...] = (),
        model_config: dict | None = None,
    ) -> Session:
        if store.data:
            raise ValueError("Session already exists")
        limits = limits or Limits()
        state = {
            "schema": "rio.state/1",
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
                    "cwd": str(cwd.resolve()),
                    "limits": asdict(limits),
                    "validators": [list(v) for v in validators],
                    "model_config": model_config or {},
                    "terminal": None,
                    "pause": None,
                    "invalid": 0,
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
        return cls(store)

    @property
    def data(self) -> dict:
        return self.store.data

    @property
    def meta(self) -> dict:
        return self.data["meta"]

    @property
    def state(self) -> dict:
        return copy.deepcopy(self.meta["state"])

    def pending(self) -> list[dict]:
        return [e for e in self.data["executions"].values() if e["phase"] != "finished"]

    def unresolved(self) -> list[int]:
        return [
            int(key)
            for key, e in self.data["executions"].items()
            if e.get("uncertain") and key not in self.data["resolutions"]
        ]

    def receive(self, message_id: str, kind: str, payload: dict) -> None:
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

    def prepare_turn(self, system: str, tool_schema: dict) -> dict:
        for turn in self.data["turns"].values():
            if turn["receipt"] is None:
                return copy.deepcopy(turn)
        if self.meta["terminal"]:
            raise ValueError("Terminal runs cannot resume")
        if self.store.journal_bytes >= self.limits.storage_bytes:
            raise ValueError("Session storage budget exhausted")
        usage = self.meta["usage"]
        if usage["rounds"] >= self.limits.rounds or usage["tokens"] >= self.limits.tokens:
            raise ValueError("Model budget exhausted")
        state = self.state
        state["revision"] += 1
        selected = {str(c["id"]) for c in state["cells"]}
        state["runtime"]["cells"] = {
            key: {k: v for k, v in execution.items() if k != "worker"}
            for key, execution in self.data["executions"].items()
            if key in selected or execution["phase"] != "finished" or int(key) in self.unresolved()
        }
        outputs = state["runtime"]["cells"]
        per_output = min(
            self.limits.output_bytes, self.limits.context_bytes // 8 // max(1, len(outputs))
        )
        for execution in outputs.values():
            for name in ("output", "stderr"):
                raw = execution[name].encode()
                if len(raw) > per_output:
                    execution[name] = raw[:per_output].decode(errors="ignore")
                    execution["context_truncated"] = True
        # Selected output competes with new observations. Keep receipts compact; full records
        # stay available through read-only SQLite retrieval, even after context pruning.
        delivered = []
        state["runtime"]["inbox"] = []
        state["runtime"]["usage"] = dict(usage)
        for message in self.data["inbox"].values():
            if message["consumed"]:
                continue
            compact = {k: v for k, v in message.items() if k != "consumed"}
            if (
                len(encode(state).encode()) + len(encode(compact).encode())
                > self.limits.context_bytes
            ):
                break
            state["runtime"]["inbox"].append(compact)
            delivered.append(message["id"])
        if len(encode(state).encode()) > self.limits.context_bytes:
            raise ValueError("Runtime facts exceed reserved context")
        turn_id = uuid.uuid4().hex
        turn = {
            "id": turn_id,
            "state": state,
            "delivered": delivered,
            "system": system,
            "tool_schema": tool_schema,
            "model_config": self.meta["model_config"],
            "attempts": 0,
            "retry_at": 0,
            "error": None,
            "receipt": None,
            "response": None,
        }
        self.store.commit(
            "turn_prepared",
            {
                "meta": {"state": state, "usage": {**usage, "rounds": usage["rounds"] + 1}},
                "turns": {turn_id: turn},
            },
        )
        return copy.deepcopy(turn)

    def attempt(self, turn_id: str) -> dict:
        turn = self.data["turns"][turn_id]
        if turn["receipt"] or self.meta["terminal"]:
            raise ValueError("Turn already accepted or run ended")
        if turn["attempts"] >= self.limits.invalid_attempts:
            raise ValueError("Model retry budget exhausted")
        usage = self.meta["usage"]
        if usage["tokens"] >= self.limits.tokens:
            raise ValueError("Token budget exhausted")
        turn = {**turn, "attempts": turn["attempts"] + 1, "response": None}
        # Until a complete response commits, usage for this attempt is unknown.
        self.store.commit(
            "model_attempt",
            {
                "turns": {turn_id: turn},
                "meta": {
                    "usage": {
                        **usage,
                        "attempts": usage["attempts"] + 1,
                        "unknown_usage": usage["unknown_usage"] + 1,
                    }
                },
            },
        )
        return copy.deepcopy(turn)

    def response(self, turn_id: str, response: dict, tokens: int, *, known: bool = True) -> None:
        turn = self.data["turns"][turn_id]
        usage = self.meta["usage"]
        self.store.commit(
            "model_response",
            {
                "turns": {turn_id: {**turn, "response": response}},
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
                        **turn,
                        "error": reason,
                        "response": None,
                        "retry_at": time.time() + min(2 ** turn["attempts"], 30),
                    }
                },
                "meta": {"invalid": self.meta["invalid"] + 1},
            },
        )

    def accept(self, turn_id: str, patch: list) -> dict:
        turn = self.data["turns"][turn_id]
        if turn["receipt"] is not None:
            if turn["patch"] != patch:
                raise InvalidPatch("Conflicting submission for an accepted turn")
            return copy.deepcopy(turn["receipt"])
        if self.meta["terminal"]:
            raise InvalidPatch("Run has ended")
        state, new = admit(
            turn["state"], patch, self.data["executions"], self.limits.context_bytes // 2
        )
        new_code = [cell for cell in new if cell["kind"] == "code"]
        if len(self.pending()) + len(self.unresolved()) + len(new_code) > self.limits.pending_cells:
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
                            "patch consumes the error and resumes dispatch."
                        )
                    for ref in cell["evidence_refs"]:
                        if not isinstance(ref, str) or not ref.startswith("session:event:"):
                            raise InvalidPatch("Evidence must reference recorded Session events")
                        try:
                            seq = int(ref.rsplit(":", 1)[1])
                        except ValueError:
                            raise InvalidPatch("Invalid evidence reference") from None
                        row = self.store.db.execute(
                            "SELECT kind FROM events WHERE seq = ?", (seq,)
                        ).fetchone()
                        if row is None or row[0] not in (
                            "execution_finished",
                            "inbox_received",
                            "validator_result",
                        ):
                            raise InvalidPatch("Evidence must reference a recorded observation")
        receipt = {
            "turn_id": turn_id,
            "revision": state["revision"],
            "cell_ids": [c["id"] for c in new],
            "event": self.store.sequence + 1,
        }
        executions = {
            str(c["id"]): {
                "cell_id": c["id"],
                "phase": "queued",
                "output": "",
                "stderr": "",
                "truncated": False,
                "accepted": self.store.sequence + 1,
            }
            for c in new_code
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
                    "state": state,
                    "pause": None if pause in messages else pause,
                    "invalid": 0,
                },
                "cells": {str(c["id"]): c for c in new},
                "executions": executions,
                "controls": controls,
                "inbox": messages,
                "turns": {turn_id: {**turn, "receipt": receipt, "patch": patch}},
            },
            identity=f"patch:{turn_id}",
        )
        return receipt

    def authorize(self, cell_id: int, worker: dict) -> None:
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
            {
                "executions": {
                    key: {**execution, "phase": "started", "worker": worker, "started": time.time()}
                }
            },
        )

    def output(self, cell_id: int, stdout: str = "", stderr: str = "") -> None:
        key = str(cell_id)
        execution = self.data["executions"][key]
        if execution["phase"] != "started":
            raise ValueError("Output requires a started execution")
        changed = dict(execution)
        for name, text in (("output", stdout), ("stderr", stderr)):
            raw = (changed[name] + text).encode()
            changed[name] = raw[: self.limits.output_bytes].decode(errors="ignore")
            changed["truncated"] |= len(raw) > self.limits.output_bytes
        if changed != execution:
            self.store.commit("execution_output", {"executions": {key: changed}})

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
            "worker_stopped": result.get("worker_stopped", False),
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
        result: str,
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
            }
        }
        if note_id is not None:
            changes["controls"] = {
                note_id: {**self.data["controls"][note_id], "handled": True, "outcome": "accepted"}
            }
        self.store.commit("run_ended", changes, identity="terminal")

    def import_legacy(self, path: Path) -> None:
        """Archive a JSONL journal once as data. Import never schedules historical code."""
        from hashlib import sha256

        from rio.coding.session_store.jsonl import entries_from_json_lines

        raw = path.read_bytes()
        identity = sha256(raw).hexdigest()
        if identity in self.data.get("legacy", {}):
            return
        if len(raw) + self.store.journal_bytes > self.limits.storage_bytes:
            raise ValueError("Legacy history exceeds storage budget")
        entries = entries_from_json_lines(raw.decode().splitlines())
        self.store.commit(
            "legacy_imported",
            {"legacy": {identity: [entry.model_dump(mode="json") for entry in entries]}},
            identity=f"legacy:{identity}",
        )
