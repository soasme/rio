"""Human transcript of committed durable session events and streamed drafts."""

from __future__ import annotations

import json
import shlex

from rio.ai import ProviderEvent, ProviderToolCallDeltaEvent

_OUTPUT_CHARS = 8_000
_BODY_KEYS = ("source", "text")


class StepDraft:
    """Read Cell bodies from a `step` call's JSON arguments while they stream.

    `feed` returns `(body number, text)` pieces: code source and note text as they
    arrive, a cmd's joined argv once its array closes. Other strings are skipped.
    """

    def __init__(self) -> None:
        self._open = ""  # "{" or "[" per open container
        self._last = ""  # last structural character outside strings
        self._key: str | None = None  # a value always directly follows its key
        self._role: str | None = None  # the string being read: key, body, argv or skip
        self._text = ""
        self._escape = ""
        self._argv: list[str] = []
        self._bodies = 0

    def feed(self, chunk: str) -> list[tuple[int, str]]:
        out: list[tuple[int, str]] = []
        for char in chunk:
            if self._role is not None:
                self._read(char, out)
                continue
            if char == '"':
                self._start()
            elif char in "{[":
                self._open += char
                self._argv = []
            elif char in "}]":
                if char == "]" and self._key == "argv":
                    self._emit(shlex.join(self._argv), out)
                    self._key = None
                self._open = self._open[:-1]
            if char in '{[,:"':
                self._last = char
        return out

    def _start(self) -> None:
        container = self._open[-1:]
        if container == "{" and self._last in "{,":
            self._role = "key"
        elif container == "{" and self._key in _BODY_KEYS:
            self._role = "body"
        elif container == "[" and self._key == "argv":
            self._role = "argv"
        else:
            self._role = "skip"
        self._text = ""

    def _read(self, char: str, out: list[tuple[int, str]]) -> None:
        if self._escape:
            self._escape += char
            if not _whole_escape(self._escape):
                return
            char, self._escape = json.loads(f'"{self._escape}"'), ""
        elif char == "\\":
            self._escape = char
            return
        elif char == '"':
            if self._role == "key":
                self._key = self._text
            elif self._role == "argv":
                self._argv.append(self._text)
            self._role = None
            return
        if self._role == "body":
            self._emit(char, out, new=not self._text)
        self._text += char

    def _emit(self, text: str, out: list[tuple[int, str]], *, new: bool = True) -> None:
        if new:
            self._bodies += 1
        index = self._bodies - 1
        if out and out[-1][0] == index:
            out[-1] = (index, out[-1][1] + text)
        else:
            out.append((index, text))


def _whole_escape(escape: str) -> bool:
    """Whether a JSON escape is complete; a high surrogate waits for its low half."""
    if escape[1] != "u":
        return True
    return len(escape) == 12 or len(escape) == 6 and not "\\ud800" <= escape.lower() < "\\udc00"


class PlainEventRenderer:
    """Show code, notes, control reasons, and streaming results without internal metadata."""

    def __init__(self) -> None:
        self._has_entries = False
        self._line_open = False
        self._stream: tuple[str, str] | None = None
        self._displayed: dict[tuple[str, str], int] = {}
        self._output_chars: dict[str, int] = {}
        self._truncated: set[str] = set()
        self._step = StepDraft()
        self._drafts: list[str] = []  # bodies shown while the model streamed

    def preview(self, event: ProviderEvent) -> None:
        """Show Cell bodies as the model writes them; only committed events are final."""
        if isinstance(event, ProviderToolCallDeltaEvent):
            for index, text in self._step.feed(event.delta):
                self._draft(index, text)

    def render(self, event: dict) -> None:
        kind, changes = event["type"], event["changes"]
        if kind == "patch_accepted":
            drafts = self._drafts
            self._end_draft()
            # Only new versions, in allocated ID order; State also contains old cells.
            cells = sorted(changes["cells"].values(), key=lambda cell: cell["id"])
            for position, cell in enumerate(cells):
                if cell["kind"] == "cmd":
                    body = shlex.join(cell["argv"])
                else:
                    body = cell["source"] if cell["kind"] == "code" else cell["text"]
                # A body already streamed in the same position is not repeated.
                if position >= len(drafts) or drafts[position] != body:
                    self._message(body)
            # Controls are requests; run_ended alone reports the final result.
            for key in sorted(changes.get("controls", {}), key=int):
                self._message(changes["controls"][key]["control"]["reason"])
        elif kind in ("execution_output", "output_truncated", "execution_finished"):
            chunks = "output_chunks" in changes
            for key, execution in changes.get(
                "output_chunks", changes.get("executions", {})
            ).items():
                self._output(key, execution, chunks=chunks)
                if kind == "execution_finished":
                    result = execution["result"]
                    text = result["type"]
                    if result.get("exit_code") is not None:
                        text += f" (exit {result['exit_code']})"
                    if result.get("reason"):
                        text += f": {result['reason']}"
                    self._result(text)
        elif kind == "patch_rejected":
            self._end_draft()
            for turn in changes["turns"].values():
                self._message(f"Retry: {turn['error']}")
        elif kind == "provider_failed":
            self._end_draft()
            for turn in changes["turns"].values():
                self._message(f"Provider error: {turn['provider_error']}")
        elif kind == "run_ended":
            terminal = changes["meta"]["terminal"]
            self._message(f"{terminal['result'].capitalize()}: {terminal['reason']}")

    def _output(self, key: str, execution: dict, *, chunks: bool = False) -> None:
        for name, text in execution["output"].items():
            label = "" if name == "stdout" else f"{name}: "
            stream = (key, name)
            previous = self._displayed.get(stream, 0)
            self._displayed[stream] = previous + len(text) if chunks else len(text)
            delta = text if chunks else text[previous:]
            remaining = _OUTPUT_CHARS - self._output_chars.get(key, 0)
            visible = delta[:remaining]
            self._output_chars[key] = self._output_chars.get(key, 0) + len(visible)
            if visible:
                if self._stream != stream:
                    self._close_line()
                    print(f"  └ {label}", end="", flush=True)
                    self._line_open = True
                    self._stream = stream
                # Keep partial lines contiguous across committed snapshots; new lines
                # align beneath the result even when a chunk ends at a newline.
                for line in visible.splitlines(keepends=True):
                    if not self._line_open:
                        print("    ", end="")
                    print(line, end="", flush=True)
                    self._line_open = not line.endswith("\n")
            if (
                len(delta) > remaining or execution.get("truncated")
            ) and key not in self._truncated:
                self._result("[output truncated]")
                self._truncated.add(key)

    def _draft(self, index: int, text: str) -> None:
        stream = ("draft", str(index))
        if index == len(self._drafts):
            self._close_line()
            if self._has_entries:
                print()
            print("• ", end="")
            self._has_entries = self._line_open = True
            self._drafts.append("")
        elif self._stream != stream:
            # Output from running work interrupted this draft; resume on a fresh line.
            self._close_line()
        self._stream = stream
        for line in text.splitlines(keepends=True):
            if not self._line_open:
                print("  ", end="")
            print(line, end="", flush=True)
            self._line_open = not line.endswith("\n")
        self._drafts[index] += text

    def _end_draft(self) -> None:
        self._close_line()
        self._step = StepDraft()
        self._drafts = []

    def _close_line(self) -> None:
        if self._line_open:
            print(flush=True)
        self._line_open = False
        self._stream = None

    def _message(self, text: str) -> None:
        self._close_line()
        if self._has_entries:
            print()
        lines = text.splitlines() or [""]
        print(f"• {lines[0]}", flush=True)
        for line in lines[1:]:
            print(f"  {line}", flush=True)
        self._has_entries = True

    def _result(self, text: str) -> None:
        self._close_line()
        lines = text.splitlines() or [""]
        print(f"  └ {lines[0]}", flush=True)
        for line in lines[1:]:
            print(f"    {line}", flush=True)
