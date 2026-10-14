"""Supervised worker processes for runnable Cells and validators, with conservative recovery."""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import json
import os
import signal
import sys
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import NotRequired, TypedDict

import psutil

import rio.coding.worker as worker
from rio.agent.runner import Owner, RecoveryFailure
from rio.agent.session import Session
from rio.agent.shapes import ValidatorResult
from rio.agent.store import StorageFailure
from rio.coding.cells import COMMANDS
from rio.truncate import tail_notice, truncate_bytes_tail, truncate_tail


class WorkerIdentity(TypedDict):
    # A "workers" record keyed "cell:<id>" or "validator:<key>". pid and birth are added once
    # the gated supervisor is spawned, and stopped once its process group is gone. A validator
    # worker keeps its result before it is stopped, so recovery reuses it instead of rerunning.
    token: str
    pid: NotRequired[int]
    birth: NotRequired[float]
    stopped: NotRequired[bool]
    result: NotRequired[ValidatorResult]


def stop_worker(identity: WorkerIdentity) -> None:
    """Verify a durable identity before signaling; never trust a recycled PID."""
    pid = identity.get("pid")
    if pid is None:
        # A supervisor spawned before identity commit remains gated and cannot run code.
        matches = []
        for process in psutil.process_iter(["cmdline", "create_time"]):
            if identity["token"] in (process.info["cmdline"] or []):
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
        if process.create_time() != identity["birth"]:
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
    ):
        super().__init__(session)
        self.commands = commands
        self.process: asyncio.subprocess.Process | None = None
        self.key = ""
        self.identity: WorkerIdentity | None = None
        self.server: asyncio.Server | None = None
        self.temporary: tempfile.TemporaryDirectory | None = None
        self.socket_path = ""

    async def __aenter__(self):
        await super().__aenter__()
        self.temporary = tempfile.TemporaryDirectory(prefix="rio-owner-")
        self.socket_path = str(Path(self.temporary.name) / "timer.sock")
        self.server = await asyncio.start_unix_server(self._timer, path=self.socket_path)
        return self

    async def __aexit__(self, *args):
        try:
            await super().__aexit__(*args)
        finally:
            if self.server:
                self.server.close()
                await self.server.wait_closed()
            if self.temporary:
                self.temporary.cleanup()

    async def _timer(self, reader, writer):
        try:
            request = json.loads(await reader.readline())
            self.session.register_timer(request["id"], request["deadline"], request["payload"])
            response = {"ok": True}
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
        for key, identity in list(self.session.data.get("workers", {}).items()):
            if not identity.get("stopped"):
                await asyncio.to_thread(stop_worker, identity)
                self.session.record("workers", key, {"stopped": True})
            kind, _, name = key.partition(":")
            if kind == "validator" and name not in self.session.data["validator_results"]:
                self.session.record_validator_result(
                    name,
                    identity.get("result")
                    or {"passed": False, "exit": None, "output": "Unknown validator execution"},
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

    def launch(self, cell_id: int) -> Awaitable[None]:
        self.session.authorize(cell_id)
        self._register(f"cell:{cell_id}")
        return self._execute(cell_id)

    def _register(self, key: str) -> None:
        """Journal a worker token before spawning, so recovery can find a gated supervisor."""
        self.key, self.identity = key, {"token": uuid.uuid4().hex}
        self.session.record("workers", key, self.identity)

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
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(Path(worker.__file__)),
            self.identity["token"],
            *command,
            cwd=self.session.meta["owner_config"].get("cwd"),
            env=env,
            start_new_session=True,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        birth = psutil.Process(process.pid).create_time()
        self.identity = {**self.identity, "pid": process.pid, "birth": birth}
        self.session.record("workers", self.key, self.identity)
        return process

    async def validate(self, key: str, validator: object) -> ValidatorResult:
        """Run a validator argv; exit status 0 passes."""
        self._register(f"validator:{key}")
        process = await self._spawn(validator)
        result = None
        try:
            process.stdin.write(b"start\n")
            await process.stdin.drain()
            output = ""
            decoders = {
                name: codecs.getincrementaldecoder("utf-8")("replace")
                for name in ("stdout", "stderr")
            }
            exit_code = None
            async with asyncio.timeout(self.session.limits.execution_seconds):
                while line := await process.stdout.readline():
                    message = json.loads(line)
                    if "exit" in message:
                        exit_code = message["exit"]
                        break
                    for name in ("stdout", "stderr"):
                        if name in message:
                            text = decoders[name].decode(bytes.fromhex(message[name]))
                            output = truncate_bytes_tail(
                                output + text, self.session.limits.output_bytes
                            )
            for decoder in decoders.values():
                output = truncate_bytes_tail(
                    output + decoder.decode(b"", final=True), self.session.limits.output_bytes
                )
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
        return result

    async def _execute(self, cell_id: int) -> None:
        session = self.session
        cell = session.data["cells"][str(cell_id)]
        env = {
            **os.environ,
            "RIO_TIMER_SOCKET": self.socket_path,
            "RIO_SESSION_URI": session.store.path.as_uri() + "?mode=ro",
            "PYTHONPATH": os.pathsep.join(
                [str(Path(__file__).resolve().parents[2])]
                + ([os.environ["PYTHONPATH"]] if "PYTHONPATH" in os.environ else [])
            ),
        }
        try:
            command = self.commands[cell["kind"]](cell, Path(self.temporary.name))
            self.process = process = await self._spawn(command, env)
            process.stdin.write(b"start\n")
            await process.stdin.drain()
            decoders = {
                name: codecs.getincrementaldecoder("utf-8")("replace")
                for name in ("stdout", "stderr")
            }
            buffers = {"stdout": "", "stderr": ""}
            files = {}
            try:
                with contextlib.ExitStack() as stack:
                    for name in buffers:
                        files[name] = stack.enter_context(
                            tempfile.NamedTemporaryFile(
                                "w",
                                encoding="utf-8",
                                prefix="rio-output-",
                                suffix=".log",
                                delete=False,
                            )
                        )
                    deadline = time.monotonic() + session.limits.flush_seconds
                    exit_code = None
                    async with asyncio.timeout(session.limits.execution_seconds):
                        line_task = asyncio.create_task(process.stdout.readline())
                        try:
                            while True:
                                done, _ = await asyncio.wait(
                                    {line_task}, timeout=max(0, deadline - time.monotonic())
                                )
                                if done:
                                    line = line_task.result()
                                    if not line:
                                        break
                                    message = json.loads(line)
                                    if "exit" in message:
                                        exit_code = message["exit"]
                                        break
                                    for name in buffers:
                                        if name in message:
                                            text = decoders[name].decode(
                                                bytes.fromhex(message[name])
                                            )
                                            files[name].write(text)
                                            combined = buffers[name] + text
                                            buffers[name] = truncate_bytes_tail(
                                                combined, session.limits.output_bytes
                                            )
                                    line_task = asyncio.create_task(process.stdout.readline())
                                if time.monotonic() >= deadline:
                                    session.output(cell_id, buffers)
                                    buffers = {"stdout": "", "stderr": ""}
                                    deadline = time.monotonic() + session.limits.flush_seconds
                        finally:
                            line_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await line_task
            finally:
                # Flush even on timeout/read failure; close handles before reading the tail.
                for name in buffers:
                    buffers[name] += decoders[name].decode(b"", final=True)
                try:
                    if not session.store.failed:
                        session.output(cell_id, buffers)
                finally:
                    # Keep the tail, and point to the full output on disk.
                    truncated = {}
                    for name in buffers:
                        if name not in files:
                            continue
                        path = Path(files[name].name)
                        truncation = truncate_tail(
                            path.read_text(encoding="utf-8"), max_bytes=session.limits.output_bytes
                        )
                        if truncation.truncated and not session.store.failed:
                            notice = tail_notice(truncation, str(path))
                            truncated[name] = truncation.content + notice
                        else:
                            path.unlink()
                    if truncated:
                        session.truncate_output(cell_id, truncated)
            # Stop descendants before reporting completion, even when the script left them behind.
            await self._stop()
            await process.wait()
            session.finish(
                cell_id,
                {
                    "type": "success"
                    if exit_code == 0
                    else "error"
                    if exit_code is not None
                    else "UnknownExecution",
                    "exit_code": exit_code,
                },
            )
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
