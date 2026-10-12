"""Serial Cell execution, immediate controls, and conservative recovery."""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import json
import math
import os
import signal
import sys
import tempfile
import time
import uuid
from pathlib import Path

import psutil

import rio.agent.worker as worker
from rio.agent.session import Session
from rio.agent.shapes import ValidatorResult, WorkerIdentity
from rio.agent.store import StorageFailure
from rio.truncate import tail_notice, truncate_bytes_tail, truncate_tail


class RecoveryFailure(RuntimeError):
    pass


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


class Owner:
    def __init__(self, session: Session):
        self.session = session
        self.task: asyncio.Task | None = None
        self.process: asyncio.subprocess.Process | None = None
        self.active_id: int | None = None
        self.identity: WorkerIdentity | None = None
        self.server: asyncio.Server | None = None
        self.temporary: tempfile.TemporaryDirectory | None = None
        self.socket_path = ""
        # Set by every commit and by the worker task ending; cleared at the start of tick().
        self.changed = asyncio.Event()

    async def __aenter__(self):
        cells = self.session.data["cells"].values()
        missing = {c["kind"] for c in cells} - {"note", *self.session.kinds}
        if missing:
            raise ValueError(f"Open this Session with its Cell kinds: {', '.join(sorted(missing))}")
        self.session.store.on_commit = self.changed.set
        await self.recover()
        self.temporary = tempfile.TemporaryDirectory(prefix="rio-owner-")
        self.socket_path = str(Path(self.temporary.name) / "timer.sock")
        self.server = await asyncio.start_unix_server(self._timer, path=self.socket_path)
        return self

    async def __aexit__(self, *args):
        try:
            await self.stop_active()
        finally:
            self.session.store.on_commit = None
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
        for execution in list(self.session.data["executions"].values()):
            if execution.get("worker") and not execution.get("worker_stopped"):
                await asyncio.to_thread(stop_worker, execution["worker"])
                if execution["phase"] == "finished":
                    self.session.worker_stopped(execution["cell_id"])
                    continue
                self.session.finish(
                    execution["cell_id"],
                    {
                        "type": "UnknownExecution",
                        "reason": "worker_lost_before_result_commit",
                        "worker_stopped": True,
                    },
                )
        for key, record in list(self.session.data.get("validation_workers", {}).items()):
            if record["result"] is None:
                await asyncio.to_thread(stop_worker, record["worker"])
                self.session.interrupt_validator(key)
        if not self.session.meta["terminal"]:
            self.session.fire_timers()
            await self.controls()

    async def stop_active(self) -> None:
        if self.identity:
            await asyncio.to_thread(stop_worker, self.identity)
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        if self.process:
            await self.process.wait()
        self.task = self.process = self.identity = self.active_id = None

    async def fail(self, reason: str, *, note_id: str | None = None) -> None:
        error = None
        try:
            await self.stop_active()
        except RecoveryFailure as exc:
            error = str(exc)
        for execution in list(self.session.pending()):
            if execution["phase"] == "queued":
                self.session.finish(execution["cell_id"], {"type": "cancelled"})
            else:
                self.session.finish(
                    execution["cell_id"],
                    {"type": "UnknownExecution", "reason": reason, "worker_stopped": error is None},
                )
        self.session.end("failure", reason, cleanup_error=error, note_id=note_id)

    async def controls(self) -> None:
        controls = sorted(
            self.session.data["controls"].items(),
            key=lambda pair: (pair[1]["cell"].get("role") != "cancel", int(pair[0])),
        )
        for key, control in controls:
            if control["handled"]:
                continue
            cell = control["cell"]
            role = cell["role"]
            if role == "cancel":
                target = cell["target_cell_id"]
                execution = self.session.data["executions"][str(target)]
                if execution["phase"] == "started":
                    # recover() finishes leftover workers, so a started execution is the active one.
                    assert target == self.active_id
                    await self.stop_active()
                    self.session.finish(
                        target,
                        {
                            "type": "UnknownExecution",
                            "reason": "cancelled_after_launch",
                            "worker_stopped": True,
                        },
                    )
                elif execution["phase"] == "queued":
                    self.session.finish(target, {"type": "cancelled"})
                self.session.handled(key, "accepted")
            elif role == "resolution":
                self.session.handled(key, "accepted", resolution=cell["target_cell_id"])
            elif cell["result"] == "failure":
                await self.fail(cell["text"], note_id=key)
            else:
                reason = await self._success_blocker(key)
                if reason:
                    self.session.handled(key, reason)
                else:
                    self.session.end("success", cell["text"], note_id=key)
            if self.session.meta["terminal"]:
                break

    async def _success_blocker(self, note_id: str) -> str | None:
        self.session.fire_timers()
        if (
            self.session.pending()
            or self.session.unresolved()
            or any(not t["fired"] for t in self.session.data["timers"].values())
        ):
            return "Success requires no pending work or unresolved uncertainty"
        if any(not m["consumed"] for m in self.session.data["inbox"].values()):
            return "New observations must be consumed before success"
        # Validators execute afresh at the conclusion, using the same supervised worker path.
        # The API uses argv lists, never model-editable shell commands.
        for index, command in enumerate(self.session.meta["validators"]):
            result = await self._validate(f"{note_id}:{index}", command)
            self.session.record_validator_result(index, command, result)
            if result["exit"] != 0:
                return f"Required validator {index} failed: {result['output']}"
        self.session.fire_timers()
        if any(not m["consumed"] for m in self.session.data["inbox"].values()):
            return "Observations arrived during validation"
        return None

    async def _validate(self, key: str, command: list[str]) -> ValidatorResult:
        previous = self.session.data.get("validation_workers", {}).get(key)
        if previous:
            return previous["result"]
        identity: WorkerIdentity = {"token": uuid.uuid4().hex}
        self.session.start_validator(key, identity)
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(Path(worker.__file__)),
            identity["token"],
            *command,
            cwd=self.session.meta["cwd"],
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        identity = {
            **identity,
            "pid": process.pid,
            "birth": psutil.Process(process.pid).create_time(),
        }
        try:
            self.session.register_validator(key, identity)
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
            result = {"exit": exit_code, "output": output}
        except TimeoutError:
            result = {
                "exit": None,
                "output": "Validator interrupted; inspect effects before retrying",
            }
        finally:
            await asyncio.to_thread(stop_worker, identity)
            await process.wait()
        self.session.finish_validator(key, result)
        return result

    async def wait(self, until: float = math.inf, request: asyncio.Task | None = None) -> None:
        """Wait for a commit, the worker task ending, the next timer or `until`, or `request`.

        Wake sources: worker output and results, timer registrations and inbox input all
        commit; timer deadlines and retry backoff are deadlines; the model call is `request`.
        """
        timers = (t["deadline"] for t in self.session.data["timers"].values() if not t["fired"])
        deadline = min([until, *timers])
        timeout = None if deadline == math.inf else max(0, deadline - time.time())
        changed = asyncio.create_task(self.changed.wait())
        try:
            await asyncio.wait(
                {changed, *([request] if request else [])},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            changed.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await changed

    async def tick(self) -> None:
        # Anything that changes after this point wakes the next wait().
        self.changed.clear()
        if self.task and self.task.done():
            await self.task
            self.task = self.process = self.identity = self.active_id = None
        if self.session.store.failed:
            raise StorageFailure("Owner must recover storage")
        self.session.fire_timers()
        await self.controls()
        if self.session.meta["terminal"] or self.session.meta["pause"] or self.task:
            return
        pending = self.session.pending()
        if pending:
            cell_id = pending[0]["cell_id"]
            self.active_id = cell_id
            self.identity = {"token": uuid.uuid4().hex}
            self.session.authorize(cell_id, self.identity)
            self.task = asyncio.create_task(self._execute(cell_id))
            self.task.add_done_callback(lambda _: self.changed.set())

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
            command = session.kinds[cell["kind"]].command(cell, Path(self.temporary.name))
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(Path(worker.__file__)),
                self.identity["token"],
                *command,
                cwd=session.meta["cwd"],
                env=env,
                start_new_session=True,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self.process = process
            self.identity = {
                **self.identity,
                "pid": process.pid,
                "birth": psutil.Process(process.pid).create_time(),
            }
            session.register_worker(cell_id, self.identity)
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
                                    session.output(cell_id, buffers["stdout"], buffers["stderr"])
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
                        session.output(cell_id, buffers["stdout"], buffers["stderr"])
                finally:
                    # Keep the tail, and point to the full output on disk.
                    truncated = {}
                    for name, key in (("stdout", "output"), ("stderr", "stderr")):
                        if name not in files:
                            continue
                        path = Path(files[name].name)
                        truncation = truncate_tail(
                            path.read_text(encoding="utf-8"), max_bytes=session.limits.output_bytes
                        )
                        if truncation.truncated and not session.store.failed:
                            truncated[key] = truncation.content + tail_notice(truncation, str(path))
                        else:
                            path.unlink()
                    if truncated:
                        session.truncate_output(cell_id, truncated)
            # Stop descendants before reporting completion, even when the script left them behind.
            await asyncio.to_thread(stop_worker, self.identity)
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
                    "worker_stopped": True,
                },
            )
        except (TimeoutError, OSError) as exc:
            await asyncio.to_thread(stop_worker, self.identity)
            if self.process is not None:
                await self.process.wait()
            session.finish(
                cell_id,
                {
                    "type": "UnknownExecution" if self.process else "error",
                    "reason": str(exc) or "execution timeout",
                    "worker_stopped": True,
                },
            )
