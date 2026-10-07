"""Human transcript of committed durable session events."""

from __future__ import annotations

import shlex

_OUTPUT_CHARS = 8_000


class PlainEventRenderer:
    """Show code, notes, and streaming results without internal cell metadata."""

    def __init__(self) -> None:
        self._has_entries = False
        self._line_open = False
        self._stream: tuple[str, str] | None = None
        self._displayed: dict[tuple[str, str], int] = {}
        self._output_chars: dict[str, int] = {}
        self._truncated: set[str] = set()

    def render(self, event: dict) -> None:
        kind, changes = event["type"], event["changes"]
        if kind == "patch_accepted":
            # Only new versions, in allocated ID order; State also contains old cells.
            for cell in sorted(changes["cells"].values(), key=lambda cell: cell["id"]):
                if cell["kind"] == "cmd":
                    body = shlex.join(cell["argv"])
                else:
                    body = cell["source"] if cell["kind"] == "code" else cell["text"]
                self._message(body)
        elif kind in ("execution_output", "output_truncated", "execution_finished"):
            for key, execution in changes["executions"].items():
                self._output(key, execution)
                if kind == "execution_finished":
                    result = execution["result"]
                    text = result["type"]
                    if result.get("exit_code") is not None:
                        text += f" (exit {result['exit_code']})"
                    if result.get("reason"):
                        text += f": {result['reason']}"
                    self._result(text)
        elif kind == "patch_rejected":
            for turn in changes["turns"].values():
                self._message(f"Retry: {turn['error']}")
        elif kind == "run_ended":
            terminal = changes["meta"]["terminal"]
            self._message(f"{terminal['result'].capitalize()}: {terminal['reason']}")

    def _output(self, key: str, execution: dict) -> None:
        for name, label in (("output", ""), ("stderr", "stderr: ")):
            stream = (key, name)
            text = execution[name]
            previous = self._displayed.get(stream, 0)
            self._displayed[stream] = len(text)
            delta = text[previous:]
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
