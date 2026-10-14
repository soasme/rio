"""Journal executions, observations, timers, controls, and run endings."""

from __future__ import annotations

import math
import statistics
import time
from typing import cast

from rio.agent.shapes import (
    EventKind,
    Execution,
    InboxMessage,
    Meta,
    Projection,
    RunResult,
    ValidatorResult,
)
from rio.agent.state import Limits
from rio.agent.store import Store, encode
from rio.truncate import truncate_bytes_tail


def observation(message_id: str, kind: str, payload: dict, seq: int) -> InboxMessage:
    return {"id": message_id, "kind": kind, "payload": payload, "seq": seq, "consumed": False}


class RuntimeLedger:
    def __init__(self, store: Store, limits: Limits):
        self.store = store
        self.limits = limits

    @property
    def data(self) -> Projection:
        return cast(Projection, self.store.data)

    @property
    def meta(self) -> Meta:
        return self.data["meta"]

    def _patch(self, collection: str, key: str, event: EventKind, /, **fields) -> None:
        record = self.data.get(collection, {}).get(key, {})
        self.store.commit(event, {collection: {key: {**record, **fields}}})

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
        self._patch(collection, key, "owner_record", **fields)

    def truncate_output(self, cell_id: int, output: dict[str, str]) -> None:
        """Replace the given streams with final text, such as a tail and a pointer to more."""
        key = str(cell_id)
        execution = self.data["executions"][key]
        self._patch(
            "executions",
            key,
            "output_truncated",
            output={**execution["output"], **output},
            truncated=True,
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
        self._patch("executions", key, "execution_started", phase="started", started=time.time())

    def append_output(self, cell_id: int, chunks: dict[str, str]) -> None:
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

    def mark_handled(self, note_id: str, outcome: str, *, resolution: int | None = None) -> None:
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
