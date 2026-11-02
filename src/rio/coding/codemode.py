"""Run one code Cell in Small Python: builtins only, the working directory and /tmp mounted."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import small_python

from rio.coding.api import records, timer


def _write(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def _records(after: int = 0) -> list[dict]:
    return [record.model_dump() for record in records(after)]


def run(source: str, filename: str) -> int:
    """Execute source and return the process exit code."""
    with (
        tempfile.TemporaryDirectory(prefix="rio-codemode-") as scratch,
        small_python.Runtime(std=False, output=_write) as runtime,
    ):
        runtime.mount(os.getcwd())
        runtime.mount(scratch, "/tmp")
        runtime.define("timer", timer)
        runtime.define("records", _records)
        try:
            runtime.exec(source, filename)
        except small_python.Error as exc:
            if exc.type == "SystemExit":
                code = exc.exception.code if isinstance(exc.exception, SystemExit) else 1
                return code if isinstance(code, int) else 0 if code is None else 1
            print(f'File "{exc.filename}", line {exc.lineno}\n{exc}', file=sys.stderr)
            return 1
    return 0


def main() -> None:
    script = Path(sys.argv[1])
    sys.exit(run(script.read_text(encoding="utf-8"), script.name))


if __name__ == "__main__":
    main()
