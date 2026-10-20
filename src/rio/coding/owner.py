"""Supervised worker processes for runnable Cells and validators, with conservative recovery."""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import dataclasses
import json
import os
import signal
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Coroutine, Mapping
from pathlib import Path
from typing import Self

import psutil

import rio.coding.worker as worker
from rio.agent.runner import Owner, RecoveryFailure
from rio.agent.session import Session
from rio.agent.shapes import RecordModel, ValidatorResult
from rio.agent.store import StorageFailure
from rio.coding.cells import COMMANDS
from rio.truncate import format_size, tail_notice, truncate_bytes_tail, truncate_tail

# Disk budget for the full logs of truncated Cell output, shared by an owner's Cells.
LOG_BYTES = 64 * 1024 * 1024


class Stream:
    """One Cell output stream: a bounded tail, running totals, and a log within a budget."""

    def __init__(self, path: Path, tail_bytes: int) -> None:
        self.path = path
        self.log = path.open("wb")
        self.tail_bytes = tail_bytes
        self.tail = ""
        self.total_bytes = 0
        self.logged_bytes = 0
        self.newlines = 0
        self.ends_with_newline = False

    def append(self, text: str, budget: int) -> int:
        """Record `text`, writing at most `budget` bytes to the log. Returns bytes logged."""
        if not text:
            return 0
        encoded = text.encode()
        # Keep 4 spare bytes so a clipped tail always exceeds the shown limit.
        self.tail = truncate_bytes_tail(self.tail + text, self.tail_bytes + 4)
        self.total_bytes += len(encoded)
        self.newlines += text.count("\n")
        self.ends_with_newline = text.endswith("\n")
        logged = encoded[:budget].decode("utf-8", "ignore").encode()
        self.log.write(logged)
        self.logged_bytes += len(logged)
        return len(logged)


class WorkerIdentity(RecordModel):
    # A "workers" record keyed "cell:<id>" or "validator:<key>". pid and birth are added once
    # the gated supervisor is spawned, and stopped once its process group is gone. A validator
    # worker keeps its result before it is stopped, so recovery reuses it instead of rerunning.
    token: str
    pid: int | None = None
    birth: float | None = None
    stopped: bool = False
    result: ValidatorResult | None = None


def stop_worker(identity: WorkerIdentity) -> None:
    """Verify a durable identity before signaling; never trust a recycled PID."""
    pid = identity.pid
    if pid is None:
        # A supervisor spawned before identity commit remains gated and cannot run code.
        matches = []
        for process in psutil.process_iter(["cmdline", "create_time"]):
            if identity.token in (process.info["cmdline"] or []):
                matches.append(process)
    else:
        try:
            process = psutil.Process(pid)
        except psutil.NoSuchProcess:
            # A group can outlive its leader. Fail closed if orphaned members remain.
            for candidate in psutil.process_iter():
                try:
                    if (
                        os.getpgid(candidate.pid) == pid
                        and candidate.status() != psutil.STATUS_ZOMBIE
                    ):
                        raise RecoveryFailure("Worker leader lost while its process group survives")
                except (ProcessLookupError, psutil.NoSuchProcess):
                    pass
            return
        if process.create_time() != identity.birth:
            raise RecoveryFailure("Worker PID was reused; cannot establish local termination")
        matches = [process]
    for process in matches:
        try:
            if os.getpgid(process.pid) != process.pid:
                raise RecoveryFailure("Worker left its recorded process group")
            # Capture descendants too, including ones which created their own process group.
            children = process.children(recursive=True)
            for child in children:
                with contextlib.suppress(psutil.NoSuchProcess):
                    child.kill()
            os.killpg(process.pid, signal.SIGKILL)
            # asyncio owns waitpid for our supervisor. Poll identity/status without reaping
            # it from this thread; psutil.wait_procs would race asyncio's child watcher.
            deadline = time.monotonic() + 5
            alive = [*children, process]
            while alive:
                survivors = []
                for candidate in alive:
                    with contextlib.suppress(psutil.NoSuchProcess):
                        if candidate.is_running() and candidate.status() != psutil.STATUS_ZOMBIE:
                            survivors.append(candidate)
                alive = survivors
                if alive and time.monotonic() >= deadline:
                    raise RecoveryFailure("Unable to stop local worker descendants")
                if alive:
                    time.sleep(0.01)
        except (ProcessLookupError, psutil.NoSuchProcess):
            pass


class ProcessOwner(Owner):
    """Run each Cell and validator argv in a gated worker process group.

    Workers start in ``owner_config["cwd"]`` when the Session sets it. ``commands`` maps each
    runnable Cell kind to the argv for one Cell version; it may write files into a scratch
    directory. Worker identities are journaled in the "workers" owner collection.
    """

    def __init__(
        self,
        session: Session,
        commands: Mapping[str, Callable[[dict, Path], list[str]]] = COMMANDS,
        log_bytes: int = LOG_BYTES,
    ) -> None:
        super().__init__(session)
        self.commands = commands
        # Logs live in the owner's temporary directory and are removed when the owner exits.
        self.log_budget = log_bytes
        self.process: asyncio.subprocess.Process | None = None
        self.key = ""
        self.identity: WorkerIdentity | None = None
        self.server: asyncio.Server | None = None
        self.temporary: tempfile.TemporaryDirectory | None = None
        self.socket_path = ""

    async def __aenter__(self) -> Self:
        await super().__aenter__()
        self.temporary = tempfile.TemporaryDirectory(prefix="rio-owner-")
        self.socket_path = str(Path(self.temporary.name) / "timer.sock")
        self.server = await asyncio.start_unix_server(self._timer, path=self.socket_path)
        return self

    async def __aexit__(self, *args: object) -> None:
        try:
            await super().__aexit__(*args)
        finally:
            if self.server:
                self.server.close()
                await self.server.wait_closed()
            if self.temporary:
                self.temporary.cleanup()

    async def _timer(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = json.loads(await reader.readline())
            self.session.register_timer(request["id"], request["deadline"], request["payload"])
            response: dict[str, object] = {"ok": True}
        except (ValueError, KeyError, TypeError) as exc:
            response = {"error": str(exc)}
        except StorageFailure:
            writer.close()
            return
        writer.write((json.dumps(response) + "\n").encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def recover(self) -> None:
        # Cleanup is required even for terminal runs. No dispatch happens before this finishes.
        if os.name != "posix":
            raise RecoveryFailure("Durable worker containment currently requires POSIX")
        for key, identity in list(self.session.store.data.get("workers", {}).items()):
            if not identity.get("stopped"):
                await asyncio.to_thread(stop_worker, WorkerIdentity.model_validate(identity))
                self.session.record("workers", key, {"stopped": True})
            kind, _, name = key.partition(":")
            if kind == "validator" and name not in self.session.data.validator_results:
                self.session.record_validator_result(
                    name,
                    ValidatorResult.model_validate(
                        identity.get("result")
                        or {"passed": False, "exit": None, "output": "Unknown validator execution"}
                    ),
                )
        # The base Owner finishes started Cells as UnknownExecution.
        await super().recover()

    def clear(self) -> None:
        super().clear()
        self.process = self.identity = None

    async def stop_active(self) -> None:
        await self._stop()
        process = self.process
        await super().stop_active()
        if process:
            await process.wait()

    def launch(self, cell_id: int) -> Coroutine[None, None, None]:
        self.session.authorize(cell_id)
        self._register(f"cell:{cell_id}")
        return self._execute(cell_id)

    def _register(self, key: str) -> None:
        """Journal a worker token before spawning, so recovery can find a gated supervisor."""
        self.key, self.identity = key, WorkerIdentity(token=uuid.uuid4().hex)
        self.session.record("workers", key, self.identity.model_dump(exclude_unset=True))

    async def _stop(self) -> None:
        """Stop the current worker and mark it, so recovery never signals a reused PID."""
        identity, self.identity = self.identity, None
        if identity:
            await asyncio.to_thread(stop_worker, identity)
            if not self.session.store.failed:
                self.session.record("workers", self.key, {"stopped": True})

    async def _spawn(
        self, command: list[str], env: dict | None = None
    ) -> asyncio.subprocess.Process:
        """Start a gated worker in its own process group; it waits for "start" on stdin."""
        assert self.identity is not None
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(Path(worker.__file__)),
            self.identity.token,
            *command,
            cwd=self.session.meta.owner_config.get("cwd"),
            env=env,
            start_new_session=True,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        birth = psutil.Process(process.pid).create_time()
        self.identity = self.identity.model_copy(update={"pid": process.pid, "birth": birth})
        self.session.record("workers", self.key, self.identity.model_dump(exclude_unset=True))
        return process

    async def validate(self, key: str, validator: object) -> ValidatorResult:
        """Run a validator argv; exit status 0 passes."""
        self._register(f"validator:{key}")
        if not isinstance(validator, list) or not all(isinstance(arg, str) for arg in validator):
            raise ValueError("Validator must be an argv list of strings")
        process = await self._spawn(validator)
        result = None
        try:
            assert process.stdin is not None
            process.stdin.write(b"start\n")
            await process.stdin.drain()
            output = ""

            def append(name: str, text: str) -> None:
                nonlocal output
                output = truncate_bytes_tail(output + text, self.session.limits.output_bytes)

            exit_code = await self._read_output(process, append)
            result = {"passed": exit_code == 0, "exit": exit_code, "output": output}
        except TimeoutError:
            result = {
                "passed": False,
                "exit": None,
                "output": "Validator interrupted; inspect effects before retrying",
            }
        finally:
            if result is not None and not self.session.store.failed:
                self.session.record("workers", self.key, {"result": result})
            await self._stop()
            await process.wait()
        return ValidatorResult.model_validate(result)

    async def _read_output(
        self,
        process: asyncio.subprocess.Process,
        append: Callable[[str, str], None],
        flush: Callable[[], None] | None = None,
    ) -> int | None:
        """Decode worker streams and flush pending output even while the worker is quiet."""
        decoders = {
            name: codecs.getincrementaldecoder("utf-8")("replace") for name in ("stdout", "stderr")
        }
        assert process.stdout is not None
        deadline = time.monotonic() + self.session.limits.flush_seconds
        line_task = asyncio.create_task(process.stdout.readline())
        try:
            async with asyncio.timeout(self.session.limits.execution_seconds):
                while True:
                    timeout = max(0, deadline - time.monotonic()) if flush else None
                    done, _ = await asyncio.wait({line_task}, timeout=timeout)
                    if done:
                        line = line_task.result()
                        if not line:
                            return None
                        message = json.loads(line)
                        if "exit" in message:
                            return message["exit"]
                        for name, decoder in decoders.items():
                            if name in message:
                                append(name, decoder.decode(bytes.fromhex(message[name])))
                        line_task = asyncio.create_task(process.stdout.readline())
                    if flush and time.monotonic() >= deadline:
                        flush()
                        deadline = time.monotonic() + self.session.limits.flush_seconds
        finally:
            line_task.cancel()
            try:
                with contextlib.suppress(asyncio.CancelledError):
                    await line_task
            finally:
                for name, decoder in decoders.items():
                    append(name, decoder.decode(b"", final=True))

    async def _cell_output(self, cell_id: int, process: asyncio.subprocess.Process) -> int | None:
        assert self.temporary is not None
        limit = self.session.limits.output_bytes
        buffers = {"stdout": "", "stderr": ""}
        streams: dict[str, Stream] = {}

        def append(name: str, text: str) -> None:
            self.log_budget -= streams[name].append(text, self.log_budget)
            buffers[name] = truncate_bytes_tail(buffers[name] + text, limit)

        def flush() -> None:
            self.session.append_output(cell_id, buffers)
            buffers.update(stdout="", stderr="")

        try:
            with contextlib.ExitStack() as stack:
                for name in buffers:
                    path = Path(self.temporary.name) / f"cell-{cell_id}-{name}.log"
                    streams[name] = Stream(path, limit)
                    stack.callback(streams[name].log.close)
                return await self._read_output(process, append, flush)
        finally:
            # Flush even on timeout/read failure; close logs before keeping or deleting them.
            try:
                if not self.session.store.failed:
                    flush()
            finally:
                self._retain_output(cell_id, streams)

    def _retain_output(self, cell_id: int, streams: dict[str, Stream]) -> None:
        """Link truncated output to its log; delete the logs of output shown whole."""
        limit = self.session.limits.output_bytes
        truncated = {}
        for name, stream in streams.items():
            truncation = truncate_tail(stream.tail, max_bytes=limit)
            total_lines = stream.newlines + (
                stream.total_bytes > 0 and not stream.ends_with_newline
            )
            truncation = dataclasses.replace(
                truncation, total_bytes=stream.total_bytes, total_lines=total_lines
            )
            if truncation.truncated and not self.session.store.failed:
                truncated[name] = truncation.content + tail_notice(truncation, log_pointer(stream))
            else:
                self.log_budget += stream.logged_bytes
                stream.path.unlink()
        if truncated:
            self.session.truncate_output(cell_id, truncated)

    def _cell_env(self) -> dict[str, str]:
        env = {
            **os.environ,
            "RIO_TIMER_SOCKET": self.socket_path,
            "RIO_SESSION_URI": self.session.store.path.as_uri() + "?mode=ro",
            "PYTHONPATH": os.pathsep.join(
                [str(Path(__file__).resolve().parents[2])]
                + ([os.environ["PYTHONPATH"]] if "PYTHONPATH" in os.environ else [])
            ),
        }
        if "VIRTUAL_ENV" in env:
            # The active environment is the prepared project environment: make `uv run` use it
            # without re-locking or re-syncing, so a habitual `uv run` cannot upgrade it.
            env.update(UV_NO_SYNC="1", UV_PROJECT_ENVIRONMENT=env["VIRTUAL_ENV"])
        return env

    async def _execute(self, cell_id: int) -> None:
        session = self.session
        cell = session.data.cells[str(cell_id)]
        assert self.temporary is not None
        env = self._cell_env()
        try:
            command = self.commands[cell.kind](
                cell.model_dump(exclude_unset=True), Path(self.temporary.name)
            )
            self.process = process = await self._spawn(command, env)
            assert process.stdin is not None
            process.stdin.write(b"start\n")
            await process.stdin.drain()
            exit_code = await self._cell_output(cell_id, process)
            # Stop descendants before reporting completion, even when the script left them behind.
            await self._stop()
            await process.wait()
            if exit_code is None:
                result_type = "UnknownExecution"
            elif exit_code == 0:
                result_type = "success"
            else:
                result_type = "error"
            session.finish(cell_id, {"type": result_type, "exit_code": exit_code})
        except (TimeoutError, OSError) as exc:
            await self._stop()
            if self.process is not None:
                await self.process.wait()
            session.finish(
                cell_id,
                {
                    "type": "UnknownExecution" if self.process else "error",
                    "reason": str(exc) or "execution timeout",
                },
            )


def log_pointer(stream: Stream) -> str:
    """Where the notice sends the reader for more of a truncated stream."""
    if stream.logged_bytes == stream.total_bytes:
        return f"Full output: {stream.path}"
    if stream.logged_bytes:
        return (
            f"First {format_size(stream.logged_bytes)} of {format_size(stream.total_bytes)}, "
            f"log budget reached: {stream.path}"
        )
    return "Full output not kept: log budget reached"
