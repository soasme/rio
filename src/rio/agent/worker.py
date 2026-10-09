"""Gated POSIX worker. No user code runs until its identity is committed by the owner.

The supervisor stays alive until acknowledgment, keeping the process group ID reserved.
Losing the owner's pipe stops the whole group, including ordinary child processes.
"""

from __future__ import annotations

import json
import os
import resource
import selectors
import signal
import subprocess
import sys


def main() -> None:
    if sys.stdin.readline() != "start\n":
        return
    command = sys.argv[3:] if sys.argv[2] == "--command" else ["uv", "run", "--script", sys.argv[2]]
    memory = int(os.environ.pop("RIO_CELL_MEMORY_BYTES", 0))

    def limit_memory() -> None:
        # Applies to uv and everything it starts; the supervisor stays unlimited.
        if memory:
            resource.setrlimit(resource.RLIMIT_AS, (memory, memory))

    try:
        child = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=limit_memory,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"stderr": str(exc).encode().hex()}), flush=True)
        print(json.dumps({"exit": 127}), flush=True)
        sys.stdin.readline()
        return
    selector = selectors.DefaultSelector()
    selector.register(sys.stdin, selectors.EVENT_READ, "owner")
    selector.register(child.stdout, selectors.EVENT_READ, "stdout")
    selector.register(child.stderr, selectors.EVENT_READ, "stderr")
    open_streams = 2
    while open_streams:
        for key, _ in selector.select():
            if key.data == "owner":
                os.killpg(os.getpgrp(), signal.SIGKILL)
            chunk = os.read(key.fileobj.fileno(), 4096)
            if not chunk:
                selector.unregister(key.fileobj)
                open_streams -= 1
            else:
                print(json.dumps({key.data: chunk.hex()}), flush=True)
    print(json.dumps({"exit": child.wait()}), flush=True)
    sys.stdin.readline()  # Keep group identity alive until the owner commits the result.
    os.killpg(os.getpgrp(), signal.SIGKILL)


if __name__ == "__main__":
    try:
        main()
    finally:
        os.killpg(os.getpgrp(), signal.SIGKILL)
