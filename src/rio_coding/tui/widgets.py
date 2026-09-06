"""Live execution state and step-stream widgets."""

import json

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from rio_coding.tui.formatting import (
    DIVIDER_INDICATOR,
    RESULT_PREFIX,
    TURN_INDICATOR,
    TURN_PREFIX,
    human_elapsed,
    prefixed_lines,
)
from rio_coding.tui.state import StepStreamItem, TuiState


class TranscriptLog(RichLog):
    """Transcript rendering and tool-result expansion state."""

    def __init__(self, **kwargs):
        super().__init__(wrap=True, markup=False, min_width=1, max_lines=None, **kwargs)
        self.entries: list[tuple[Text | StepStreamItem, bool]] = []
        self._tool_results_expanded = False
        self._line_counts: list[int] = []
        self._rendered_width = 0

    def display_text(self, item: Text | StepStreamItem) -> Text:
        if isinstance(item, Text):
            return item
        text = item.text
        if item.tool_target is not None and not self._tool_results_expanded:
            name, _, output = text.partition(": ")
            truncated = output.endswith("\n… output truncated")
            if truncated:
                output = output.removesuffix("\n… output truncated")
            lines = len(output.splitlines())
            count = f"{lines}{'+' if truncated else ''} {'line' if lines == 1 else 'lines'}"
            target = f": {item.tool_target}" if item.tool_target else ""
            error = ", error" if item.role == "error" else ""
            key = self.app.settings.keybindings.toggle_tool_results
            text = f"{name}{target} ({count}{error}, {key} to expand)"
        style = self.app.settings.resolved_theme.role_styles[item.role].body
        return Text(text, style=style)

    def write_entry(self, text: Text | StepStreamItem, *, continuation: bool = False) -> None:
        self.entries.append((text, continuation))
        evicted = len(self.entries) > 1000
        if evicted:
            del self.entries[0]
        width = self.scrollable_content_region.width
        if not width:
            return
        if width != self._rendered_width:
            self.redraw()
            return
        at_end = self.is_vertical_scroll_end
        scroll_y = self.scroll_y
        removed = self._line_counts.pop(0) if evicted else 0
        lines = self._entry_lines(text, continuation, len(self.entries) > 1, width)
        self._line_counts.append(len(lines))
        # Trim complete logical entries, never an arbitrary fixed line budget.
        self.max_lines = sum(self._line_counts)
        super().write(Text("\n").join(lines), width=width, scroll_end=False)
        if at_end:
            self.scroll_end(animate=False)
        elif removed:
            self.scroll_to(y=max(0, scroll_y - removed), animate=False)

    def _entry_lines(
        self, text: Text | StepStreamItem, continuation: bool, gap: bool, width: int
    ) -> list[Text]:
        lines = prefixed_lines(
            self.display_text(text),
            self.app.console,
            width,
            RESULT_PREFIX if continuation else TURN_PREFIX,
        )
        return ([Text("")] if gap and not continuation else []) + lines

    def toggle_tool_results(self) -> None:
        self._tool_results_expanded = not self._tool_results_expanded
        self.redraw()

    def clear(self) -> None:
        self.entries.clear()
        self._line_counts.clear()
        super().clear()

    def on_resize(self) -> None:
        self.call_after_refresh(self.redraw)

    def redraw(self) -> None:
        width = self.scrollable_content_region.width
        if not width:
            return
        at_end = self.is_vertical_scroll_end
        scroll_y = self.scroll_y
        super().clear()
        self.max_lines = None
        self._line_counts.clear()
        self._rendered_width = width
        for index, (text, continuation) in enumerate(self.entries):
            lines = self._entry_lines(text, continuation, index > 0, width)
            self._line_counts.append(len(lines))
            super().write(Text("\n").join(lines), width=width, scroll_end=False)
        if at_end:
            self.scroll_end(animate=False)
        else:
            self.scroll_to(y=scroll_y, animate=False)


class StepStream(Vertical):
    """Width-aware transcript with a working status row below its history."""

    DEFAULT_CSS = """
    StepStream { overflow-x: hidden; }
    StepStream RichLog { height: 1fr; overflow-x: hidden; }
    StepStream Static { height: auto; padding-top: 1; }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._working: tuple[int | None, str, str | None, bool] = (None, "", None, False)

    def compose(self) -> ComposeResult:
        yield TranscriptLog()
        yield Static("", markup=False)

    @property
    def lines(self):
        return self.query_one(TranscriptLog).lines

    @property
    def entries(self):
        return self.query_one(TranscriptLog).entries

    def write(self, text: Text | StepStreamItem, *, continuation: bool = False) -> None:
        self.query_one(TranscriptLog).write_entry(text, continuation=continuation)

    def clear(self) -> None:
        self.query_one(TranscriptLog).clear()

    def on_resize(self) -> None:
        self.call_after_refresh(self._render_working)

    def show_working(
        self, elapsed: int | None, key: str, action: str | None, *, finished: bool = False
    ) -> None:
        self._working = elapsed, key, action, finished
        self._render_working()

    def _render_working(self) -> None:
        elapsed, key, action, finished = self._working
        status = self.query_one(Static)
        status.display = elapsed is not None
        width = self.content_region.width
        if elapsed is None or not width:
            return
        if finished:
            header = f"{DIVIDER_INDICATOR} Worked for {human_elapsed(elapsed)} "
            header += DIVIDER_INDICATOR * max(1, width - len(header))
            lines = Text(header).wrap(self.app.console, width, overflow="fold")
        else:
            header = f"Working ({human_elapsed(elapsed)} {TURN_INDICATOR} {key} to interrupt)"
            lines = prefixed_lines(Text(header), self.app.console, width, TURN_PREFIX)
            if action:
                lines.extend(prefixed_lines(Text(action), self.app.console, width, RESULT_PREFIX))
        status.update(Text("\n").join(lines))


class StateSidebar(Static):
    def show_state(self, state: TuiState, *, model: str, footprint: int) -> None:
        status = "Running" if state.running else "Idle"
        self.update(
            f"{model}\n{status} · step {state.step} · queued {state.queued}\n"
            f"Next prompt: ~{footprint:,} tokens\n\nExecution state\n"
            + json.dumps(state.state, indent=2, ensure_ascii=False)
        )


class CommandPicker(ModalScreen[str | None]):
    """Keyboard-accessible selection returning the corresponding slash command."""

    CSS = """
    CommandPicker { align: center middle; }
    #picker-box { width: 70; max-width: 95%; height: 70%; border: solid $primary;
                  background: $surface; padding: 1; }
    #picker-options { height: 1fr; }
    """
    BINDINGS = [("escape", "dismiss_picker", "Back")]

    def __init__(self, title: str, options: list[tuple[str, str]]) -> None:
        super().__init__()
        self.picker_title = title
        self.options = options

    def compose(self) -> ComposeResult:
        from textual.containers import Vertical

        with Vertical(id="picker-box"):
            yield Label(self.picker_title)
            yield OptionList(
                *(Option(Text(label), id=str(i)) for i, (label, _) in enumerate(self.options)),
                id="picker-options",
            )

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.options[event.option_index][1])

    def action_dismiss_picker(self) -> None:
        self.dismiss(None)
