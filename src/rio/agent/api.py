"""Small standard-library-only API available to independent uv scripts."""

from __future__ import annotations

import contextlib
import json
import os
import socket
import sqlite3
from typing import TypedDict


class Record(TypedDict):
    # Store.events names the event kind "type"; this read-only view calls it "kind".
    seq: int
    kind: str
    changes: dict


def timer(request_id: str, deadline: float, payload: dict | None = None) -> None:
    """Register one absolute Unix deadline. Reuse its ID only for identical redelivery."""
    request = {"id": request_id, "deadline": deadline, "payload": payload or {}}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.connect(os.environ["RIO_TIMER_SOCKET"])
        connection.sendall((json.dumps(request) + "\n").encode())
        with connection.makefile("r") as response:
            result = json.loads(response.readline())
    if "error" in result:
        raise RuntimeError(result["error"])


def records(after: int = 0) -> list[Record]:
    """Retrieve committed history as data, without evaluating any historical code."""
    with contextlib.closing(sqlite3.connect(os.environ["RIO_SESSION_URI"], uri=True)) as db:
        return [
            {"seq": seq, "kind": kind, "changes": json.loads(changes)}
            for seq, kind, changes in db.execute(
                "SELECT seq, kind, changes FROM events WHERE seq > ? ORDER BY seq", (after,)
            )
        ]
