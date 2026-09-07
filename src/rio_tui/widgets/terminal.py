"""An inline interactive PTY whose output remains in the conversation."""

import asyncio
import codecs
import contextlib
import fcntl
import os
import pty
import signal
import struct
import termios

import pyte
from rich.style import Style
from rich.text import Text
from textual.binding import Binding
from textual.message import Message
from textual.widgets import Static


class ShellTerminal(Static, can_focus=True):
    BINDINGS = [
        Binding("escape", "leave", "Prompt", priority=True),
        Binding("ctrl+c", "interrupt", "Interrupt", priority=True),
    ]

    class Finished(Message):
        def __init__(self, terminal, status):
            super().__init__()
            self.terminal, self.status = terminal, status

    def __init__(self, command, cwd):
        super().__init__(classes="conversation-block shell-terminal")
        self.command, self.cwd = command, cwd
        self.process = None
        self.fd = None
        self.buffer = pyte.HistoryScreen(80, 24, history=1000)
        self.parser = pyte.Stream(self.buffer)
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.finished = False
        self.output = ""
        self.border_title = "$ " + command

    def redraw_terminal(self):
        rows = [
            *self.buffer.history.top,
            *(self.buffer.buffer[y] for y in range(self.buffer.cursor.y + 1)),
        ]
        text = Text()
        for row in rows[-1000:]:
            for x in range(self.buffer.columns):
                char = row[x]

                def color(value):
                    if value == "default":
                        return None
                    if len(value) == 6 and all(c in "0123456789abcdef" for c in value):
                        return "#" + value
                    return value.replace("brown", "yellow").replace("bright", "bright_")

                text.append(
                    char.data,
                    Style(
                        color=color(char.fg),
                        bgcolor=color(char.bg),
                        bold=char.bold,
                        italic=char.italics,
                        underline=char.underscore,
                        reverse=char.reverse,
                    ),
                )
            text.append("\n")
        text.rstrip()
        self.output = text.plain
        self.update(text)

    def resize_terminal(self):
        columns = max(20, self.content_size.width)
        self.buffer.resize(24, columns)
        if self.fd is not None:
            with contextlib.suppress(OSError):
                fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, columns, 0, 0))

    def on_resize(self):
        self.resize_terminal()
        self.redraw_terminal()

    async def run(self):
        ready = asyncio.get_running_loop().create_future()
        self.call_after_refresh(lambda: ready.set_result(None) if not ready.done() else None)
        await ready
        master, slave = pty.openpty()
        os.set_blocking(master, False)
        self.fd = master
        self.resize_terminal()
        cwd_read, cwd_write = os.pipe()
        os.set_blocking(cwd_read, False)
        loop = asyncio.get_running_loop()

        def drain():
            try:
                data = os.read(master, 32768)
            except BlockingIOError:
                return
            except OSError:
                data = b""
            if data:
                self.parser.feed(self.decoder.decode(data))
                self.redraw_terminal()
            else:
                loop.remove_reader(master)

        try:
            self.process = await asyncio.create_subprocess_exec(
                os.environ.get("SHELL") or "/bin/sh",
                "-c",
                self.command + f"\n_rio_status=$?\npwd -P >&{cwd_write}\nexit $_rio_status",
                pass_fds=(cwd_write,),
                cwd=self.cwd,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
                env={**os.environ, "TERM": "xterm-256color"},
            )
            os.close(slave)
            slave = None
            loop.add_reader(master, drain)
            os.close(cwd_write)
            cwd_write = None
            status = await self.process.wait()
            drain()
            with contextlib.suppress(OSError):
                location = os.read(cwd_read, 65536).decode().strip()
                if location and os.path.isdir(location):
                    self.cwd = location
            self.set_class(status != 0, "error")
            self.border_subtitle = f"exit {status}"
            self.post_message(self.Finished(self, status))
        finally:
            loop.remove_reader(master)
            self.fd = None
            if self.process is not None and self.process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
                await self.process.wait()
            os.close(master)
            os.close(cwd_read)
            if cwd_write is not None:
                os.close(cwd_write)
            if slave is not None:
                os.close(slave)
            self.finished = True
            self.set_class(True, "finished")

    def send(self, text):
        if self.fd is not None:
            with contextlib.suppress(OSError):
                os.write(self.fd, text.encode())

    def on_key(self, event):
        if self.finished:
            return
        special = {
            "enter": "\r",
            "backspace": "\x7f",
            "tab": "\t",
            "up": "\x1b[A",
            "down": "\x1b[B",
            "left": "\x1b[D",
            "right": "\x1b[C",
        }
        value = special.get(event.key, event.character)
        if event.key.startswith("ctrl+") and len(event.key) == 6:
            value = chr(ord(event.key[-1]) & 31)
        if value:
            self.send(value)
            event.stop()
            event.prevent_default()

    def on_paste(self, event):
        self.send(event.text)
        event.stop()

    def action_interrupt(self):
        if self.process and self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGINT)

    def action_leave(self):
        self.screen.query_one("#editor").focus()
