"""An append-only SQLite journal and its disposable, pure projection."""

from __future__ import annotations

import copy
import fcntl
import json
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from rio.agent.shapes import EventKind, JournalEvent
from rio.truncate import truncate_bytes_tail


class StorageFailure(RuntimeError):
    """Admission must stop; reopen the journal before making further decisions."""


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def reduce_event(projection: dict, changes: dict, kind: EventKind) -> dict:
    """Fold committed records without files, workers, clocks, or model calls."""
    result = projection.copy()
    for collection, records in changes.items():
        if collection == "output_chunks":
            executions = result["executions"].copy()
            for key, chunks in records.items():
                old = executions[key]
                executions[key] = {
                    **old,
                    "output": truncate_bytes_tail(
                        old["output"] + chunks["output"], chunks["limit"]
                    ),
                    "stderr": truncate_bytes_tail(
                        old["stderr"] + chunks["stderr"], chunks["limit"]
                    ),
                    "truncated": chunks["truncated"],
                }
            result["executions"] = executions
        elif collection == "meta" and "state" in records:
            old = result.get("meta", {}).get("state", {})
            state = copy.deepcopy(records["state"])
            state["runtime"] = {**old.get("runtime", {}), **state.get("runtime", {})}
            result["meta"] = {
                **result.get("meta", {}),
                **copy.deepcopy(records),
                "state": {**old, **state},
            }
        elif collection == "turns":
            turns = result.get("turns", {}).copy()
            for key, fields in records.items():
                turns[key] = {**turns.get(key, {}), **copy.deepcopy(fields)}
            result["turns"] = turns
        else:
            result[collection] = {**result.get(collection, {}), **copy.deepcopy(records)}
    if kind == "turn_prepared":
        turn = next(iter(changes["turns"].values()))
        result["meta"] = {**result["meta"], "state": copy.deepcopy(turn["state"])}
    return result


class Store:
    """One local owner, one writer, and no authoritative cache or snapshot."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path.resolve()
        # Canonicalize the sidecar too: symlink aliases must share one owner lock.
        # Locking SQLite's own inode conflicts with SQLite locks on macOS.
        self.lock = self.path.with_suffix(self.path.suffix + ".lock").open("a+b")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise StorageFailure("Session already has an owner") from None
        if self.path.exists() and self.path.stat().st_nlink != 1:
            self.lock.close()
            raise StorageFailure(
                "Session owner cannot open a hard-linked database: WAL paths differ"
            )
        self.failed = False
        # Called after each durable commit, so an owner can wake on changes.
        self.on_commit: Callable[[], None] | None = None
        self.commit_seconds: list[float] = []
        self.journal_bytes = 0
        started = time.monotonic()
        try:
            self.db = sqlite3.connect(self.path, isolation_level=None)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            if self.db.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                raise StorageFailure("SQLite WAL is required")
            if self.db.execute("PRAGMA synchronous").fetchone()[0] != 2:
                raise StorageFailure("SQLite synchronous=FULL is required")
            if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise StorageFailure("Session failed SQLite integrity check")
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS events ("
                "seq INTEGER PRIMARY KEY, identity TEXT NOT NULL UNIQUE, "
                "kind TEXT NOT NULL, timestamp REAL NOT NULL, changes TEXT NOT NULL)"
            )
            self.data: dict = {}
            self.sequence = 0
            for seq, kind, changes in self.db.execute(
                "SELECT seq, kind, changes FROM events ORDER BY seq"
            ):
                self.journal_bytes += len(changes.encode())
                self.data = reduce_event(self.data, json.loads(changes), kind)
                self.sequence = seq
        except Exception:
            self.close()
            raise
        self.rebuild_seconds = time.monotonic() - started

    def commit(self, kind: EventKind, changes: dict, *, identity: str | None = None) -> int:
        if self.failed:
            raise StorageFailure("Discard this owner and reopen the journal")
        seq = self.sequence + 1
        identity = identity or f"event:{seq}"
        raw = encode(changes)
        # Compute before entering the transaction; publish only after COMMIT returns.
        candidate = reduce_event(self.data, changes, kind)
        started = time.monotonic()
        try:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?)",
                (seq, identity, kind, time.time(), raw),
            )
            self.db.execute("COMMIT")
        except sqlite3.Error as exc:
            self.failed = True
            self.data = {}  # The commit may have succeeded. Never guess or keep admitting work.
            raise StorageFailure(f"SQLite commit outcome requires recovery: {exc}") from exc
        self.commit_seconds.append(time.monotonic() - started)
        self.journal_bytes += len(raw.encode())
        self.sequence, self.data = seq, candidate
        if self.on_commit:
            self.on_commit()
        return seq

    def event_kind(self, seq: int) -> EventKind | None:
        if self.failed:
            raise StorageFailure("Cannot read from an uncertain connection")
        row = self.db.execute("SELECT kind FROM events WHERE seq = ?", (seq,)).fetchone()
        return row[0] if row else None

    def events(self, after: int = 0) -> list[JournalEvent]:
        if self.failed:
            raise StorageFailure("Cannot publish from an uncertain connection")
        return [
            {"seq": seq, "type": kind, "timestamp": timestamp, "changes": json.loads(raw)}
            for seq, kind, timestamp, raw in self.db.execute(
                "SELECT seq, kind, timestamp, changes FROM events WHERE seq > ? ORDER BY seq",
                (after,),
            )
        ]

    def close(self) -> None:
        if hasattr(self, "db"):
            self.db.close()
        self.lock.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
