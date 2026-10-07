"""An append-only SQLite journal and its disposable, pure projection."""

from __future__ import annotations

import copy
import fcntl
import json
import sqlite3
import time
from pathlib import Path


class StorageFailure(RuntimeError):
    """Admission must stop; reopen the journal before making further decisions."""


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def reduce_event(projection: dict, changes: dict) -> dict:
    """Fold committed records without files, workers, clocks, or model calls."""
    result = projection.copy()
    for collection, records in changes.items():
        result[collection] = {**result.get(collection, {}), **copy.deepcopy(records)}
    return result


class Store:
    """One local owner, one writer, and no authoritative cache or snapshot."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path.resolve()
        self.lock = path.with_suffix(path.suffix + ".lock").open("a+b")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise StorageFailure("Session already has an owner") from None
        self.failed = False
        self.commit_seconds: list[float] = []
        self.journal_bytes = 0
        started = time.monotonic()
        try:
            self.db = sqlite3.connect(path, isolation_level=None)
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
            for seq, changes in self.db.execute("SELECT seq, changes FROM events ORDER BY seq"):
                self.journal_bytes += len(changes.encode())
                self.data = reduce_event(self.data, json.loads(changes))
                self.sequence = seq
        except Exception:
            self.close()
            raise
        self.rebuild_seconds = time.monotonic() - started

    def commit(self, kind: str, changes: dict, *, identity: str | None = None) -> int:
        if self.failed:
            raise StorageFailure("Discard this owner and reopen the journal")
        seq = self.sequence + 1
        identity = identity or f"event:{seq}"
        raw = encode(changes)
        # Compute before entering the transaction; publish only after COMMIT returns.
        candidate = reduce_event(self.data, changes)
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
        return seq

    def events(self, after: int = 0) -> list[dict]:
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
