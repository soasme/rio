"""Report program status to the terminal with OSC 7501."""

from __future__ import annotations

import os
import sys
from typing import TextIO


def enabled(stream: TextIO) -> bool:
    """Return whether to report; RIO_PROGRAM_STATUS=0|1 overrides detection."""
    override = os.environ.get("RIO_PROGRAM_STATUS")
    if override in ("0", "1"):
        return override == "1"
    return stream.isatty() and os.environ.get("TERM") != "dumb"


def report(state: str, stream: TextIO | None = None) -> None:
    """Write one program status record for Rio."""
    stream = stream or sys.stderr
    if enabled(stream):
        stream.write(f"\x1b]7501;state={state}:app=rio\x1b\\")
        stream.flush()
