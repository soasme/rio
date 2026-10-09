"""Serial uv and command execution, immediate controls, and conservative recovery."""

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

from rio.agent import worker
from rio.agent.session import Session
from rio.agent.store import StorageFailure


class RecoveryFailure(RuntimeError):
    pass


def stop_worker(identity: dict) -> None:
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
        self.identity: dict | None = None
        self.server: asyncio.Server | None = None
        self.temporary: tempfile.TemporaryDirectory | None = None
        self.socket_path = ""

    async def __aenter__(self):
        if os.name != "posix":
            raise RecoveryFailure("Durable worker containment currently requires POSIX")
        await self.recover()
        self.temporary = tempfile.TemporaryDirectory(prefix="rio-owner-")
        self.socket_path = str(Path(self.temporary.name) / "timer.sock")
        self.server = await asyncio.start_unix_server(self._timer, path=self.socket_path)
        return self

    async def __aexit__(self, *args):
        try:
            await self.stop_active()
        finally:
            if self.server:
                self.server.close()
                await self.server.wait_closed()
            if self.temporary:
                self.temporary.cleanup()

    async def _timer(self, reader, writer):
        try:
            request = json.loads(await reader.readline())
            deadline = request["deadline"]
            if (
                not isinstance(request["id"], str)
                or not request["id"]
                or not isinstance(deadline, (int, float))
                or not math.isfinite(deadline)
            ):
                raise ValueError("Timer requires a stable ID and finite deadline")
            self.session.register_timer(request["id"], deadline, request["payload"])
            response = {"ok": True}
        except (ValueError, KeyError) as exc:
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
        for execution in list(self.session.data["executions"].values()):
            if execution.get("worker") and not execution.get("worker_stopped"):
                await asyncio.to_thread(stop_worker, execution["worker"])
                if execution["phase"] == "finished":
                    self.session.store.commit(
                        "worker_cleanup",
                        {
                            "executions": {
                                str(execution["cell_id"]): {**execution, "worker_stopped": True}
                            }
                        },
                    )
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
                self.session.store.commit(
                    "validator_interrupted",
                    {
                        "validation_workers": {
                            key: {
                                **record,
                                "result": {"exit": None, "output": "Unknown validator execution"},
                            }
                        }
                    },
                )
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

    async def fail(self, reason: str) -> None:
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
        self.session.end("failure", reason, cleanup_error=error)

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
                    if target == self.active_id:
                        await self.stop_active()
                    else:
                        await asyncio.to_thread(stop_worker, execution["worker"])
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
                await self.fail(cell["text"])
                self.session.handled(key, "accepted")
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
            self.session.store.commit(
                "validator_result",
                {"validator_results": {str(index): {"command": command, **result}}},
            )
            if result["exit"] != 0:
                return f"Required validator {index} failed: {result['output']}"
        self.session.fire_timers()
        if any(not m["consumed"] for m in self.session.data["inbox"].values()):
            return "Observations arrived during validation"
        return None

    async def _validate(self, key: str, command: list[str]) -> dict:
        previous = self.session.data.get("validation_workers", {}).get(key)
        if previous:
            return previous["result"]
        identity = {"token": uuid.uuid4().hex}
        record = {"worker": identity, "result": None}
        self.session.store.commit("validator_started", {"validation_workers": {key: record}})
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(Path(worker.__file__)),
            identity["token"],
            "--command",
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
            record = {**record, "worker": identity}
            self.session.store.commit("validator_registered", {"validation_workers": {key: record}})
            process.stdin.write(b"start\n")
            await process.stdin.drain()
            output = bytearray()
            exit_code = None
            async with asyncio.timeout(self.session.limits.execution_seconds):
                while line := await process.stdout.readline():
                    message = json.loads(line)
                    if "exit" in message:
                        exit_code = message["exit"]
                        break
                    for name in ("stdout", "stderr"):
                        if name in message:
                            output.extend(
                                bytes.fromhex(message[name])[
                                    : max(0, self.session.limits.output_bytes - len(output))
                                ]
                            )
            result = {"exit": exit_code, "output": output.decode(errors="replace")}
        except TimeoutError:
            result = {
                "exit": None,
                "output": "Validator interrupted; inspect effects before retrying",
            }
        finally:
            await asyncio.to_thread(stop_worker, identity)
            await process.wait()
        self.session.store.commit(
            "validator_finished", {"validation_workers": {key: {**record, "result": result}}}
        )
        return result

    async def tick(self) -> None:
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

    async def _execute(self, cell_id: int) -> None:
        session = self.session
        cell = session.data["cells"][str(cell_id)]
        env = {
            **os.environ,
            "RIO_TIMER_SOCKET": self.socket_path,
            "RIO_SESSION_URI": session.store.path.as_uri() + "?mode=ro",
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        }
        if session.limits.cell_memory_bytes:
            env["RIO_CELL_MEMORY_BYTES"] = str(session.limits.cell_memory_bytes)
        try:
            if cell["kind"] == "cmd":
                command = ["--command", *cell["argv"]]
            else:
                script = Path(self.temporary.name) / f"cell-{cell_id}.py"
                script.write_text(cell["source"], encoding="utf-8")
                command = [str(script)]
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
            execution = session.data["executions"][str(cell_id)]
            session.store.commit(
                "worker_registered",
                {"executions": {str(cell_id): {**execution, "worker": self.identity}}},
            )
            process.stdin.write(b"start\n")
            await process.stdin.drain()
            decoders = {
                name: codecs.getincrementaldecoder("utf-8")("replace")
                for name in ("stdout", "stderr")
            }
            buffers = {"stdout": "", "stderr": ""}
            discarded = False
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
                                    text = decoders[name].decode(bytes.fromhex(message[name]))
                                    combined = buffers[name] + text
                                    discarded |= len(combined) > session.limits.output_bytes
                                    buffers[name] = combined[: session.limits.output_bytes]
                            line_task = asyncio.create_task(process.stdout.readline())
                        if time.monotonic() >= deadline:
                            session.output(cell_id, buffers["stdout"], buffers["stderr"])
                            buffers = {"stdout": "", "stderr": ""}
                            deadline = time.monotonic() + session.limits.flush_seconds
                finally:
                    line_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await line_task
            session.output(cell_id, buffers["stdout"], buffers["stderr"])
            if discarded:
                execution = session.data["executions"][str(cell_id)]
                session.store.commit(
                    "output_truncated",
                    {"executions": {str(cell_id): {**execution, "truncated": True}}},
                )
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
            session.finish(
                cell_id,
                {
                    "type": "UnknownExecution" if self.process else "error",
                    "reason": str(exc) or "execution timeout",
                    "worker_stopped": True,
                },
            )
