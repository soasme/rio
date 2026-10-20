"""Timer registration and validated history for independent uv scripts."""

from __future__ import annotations

import contextlib
import json
import os
import socket
import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rio.agent.shapes import HistoryRecord


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


def records(after: int = 0) -> list[HistoryRecord]:
    """Return validated history; standalone scripts must declare a pydantic dependency."""
    from rio.agent.shapes import HistoryRecord

    with contextlib.closing(sqlite3.connect(os.environ["RIO_SESSION_URI"], uri=True)) as db:
        return [
            HistoryRecord(seq=seq, kind=kind, changes=json.loads(changes))
            for seq, kind, changes in db.execute(
                "SELECT seq, kind, changes FROM events WHERE seq > ? ORDER BY seq", (after,)
            )
        ]
