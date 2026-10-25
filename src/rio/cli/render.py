"""Human transcript of committed durable session events and streamed drafts."""

from __future__ import annotations

import shlex

from rio.ai import ProviderEvent, ProviderRetryEvent, ProviderToolCallDeltaEvent

_OUTPUT_CHARS = 8_000
_BODY_KEYS = ("source", "text")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}


class StepDraft:
    """Read Cell bodies from a `step` call's JSON arguments while they stream.

    `feed` returns `(op index, text)` pieces: code source and note text as they arrive,
    a cmd's joined argv once its array closes. Other fields are skipped.
    """

    def __init__(self) -> None:
        # One [container, key or index] frame per open object or array.
        self._stack: list[list] = []
        self._expect_key = False
        self._string: list[str] | None = None
        self._is_key = False
        self._escape: str | None = None
        self._high: int | None = None
        self._argv: list[str] = []

    def feed(self, chunk: str) -> list[tuple[int, str]]:
        out: list[tuple[int, str]] = []
        for char in chunk:
            top = self._stack[-1] if self._stack else None
            if self._string is not None:
                self._string_char(char, out)
            elif char == "{":
                self._stack.append(["obj", None])
                self._expect_key = True
            elif char == "[":
                self._stack.append(["arr", 0])
                self._argv = []
            elif char in "}]" and top is not None:
                index = self._argv_index()
                if char == "]" and index is not None:
                    out.append((index, shlex.join(self._argv)))
                self._stack.pop()
            elif char == "," and top is not None:
                if top[0] == "arr":
                    top[1] += 1
                else:
                    self._expect_key = True
            elif char == ":":
                self._expect_key = False
            elif char == '"':
                self._string = []
                self._is_key = top is not None and top[0] == "obj" and self._expect_key
        return out

    def _cell_index(self, field: tuple[str, ...], depth: int) -> int | None:
        path = tuple(frame[1] for frame in self._stack)
        if (
            len(path) == depth
            and path[0] == "ops"
            and isinstance(path[1], int)
            and path[2] == "cell"
            and path[3] in field
        ):
            return path[1]
        return None

    def _body_index(self) -> int | None:
        return self._cell_index(_BODY_KEYS, 4)

    def _argv_index(self) -> int | None:
        return self._cell_index(("argv",), 5)

    def _string_char(self, char: str, out: list[tuple[int, str]]) -> None:
        assert self._string is not None
        if self._escape is None:
            if char == "\\":
                self._escape = ""
                return
            if char == '"':
                self._end_string()
                return
            self._emit(char, out)
        elif self._escape == "" and char != "u":
            self._escape = None
            self._emit(_ESCAPES.get(char, char), out)
        else:
            self._escape += char
            if len(self._escape) < 5:
                return
            code = int(self._escape[1:], 16)
            self._escape = None
            if 0xD800 <= code < 0xDC00:
                self._high = code
                return
            if 0xDC00 <= code < 0xE000 and self._high is not None:
                code = 0x10000 + ((self._high - 0xD800) << 10) + (code - 0xDC00)
            self._high = None
            self._emit(chr(code) if not 0xD800 <= code < 0xE000 else "\ufffd", out)

    def _emit(self, text: str, out: list[tuple[int, str]]) -> None:
        assert self._string is not None
        index = None if self._is_key else self._body_index()
        if index is None:
            self._string.append(text)
        elif out and out[-1][0] == index:
            out[-1] = (index, out[-1][1] + text)
        else:
            out.append((index, text))

    def _end_string(self) -> None:
        assert self._string is not None
        text = "".join(self._string)
        if self._is_key:
            self._stack[-1][1] = text
        elif self._argv_index() is not None:
            self._argv.append(text)
        self._string = None


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
        # Bodies shown while the model streamed, by op index.
        self._drafts: dict[int, str] = {}

    def preview(self, event: ProviderEvent) -> None:
        """Show Cell bodies as the model writes them; only committed events are final."""
        if isinstance(event, ProviderRetryEvent):
            self._end_draft()
        elif isinstance(event, ProviderToolCallDeltaEvent):
            for index, text in self._step.feed(event.delta):
                self._draft(index, text)

    def render(self, event: dict) -> None:
        kind, changes = event["type"], event["changes"]
        if kind == "patch_accepted":
            drafts = [self._drafts[index] for index in sorted(self._drafts)]
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
        if index not in self._drafts:
            self._close_line()
            if self._has_entries:
                print()
            print("• ", end="")
            self._has_entries = self._line_open = True
            self._drafts[index] = ""
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
        self._drafts = {}

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
