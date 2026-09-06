"""Live execution state and step-stream widgets."""

import json

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from rio_coding.tui.state import TuiState


class StepStream(Vertical):
    """Width-aware transcript with a transient working row below its history."""

    DEFAULT_CSS = """
    StepStream { overflow-x: hidden; }
    StepStream RichLog { height: 1fr; overflow-x: hidden; }
    StepStream Static { height: auto; padding-top: 1; }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.entries: list[tuple[Text, bool]] = []

    def compose(self) -> ComposeResult:
        yield RichLog(wrap=True, markup=False, min_width=1, max_lines=10000)
        yield Static("", markup=False)

    @property
    def lines(self):
        return self.query_one(RichLog).lines

    def write(self, text: Text, *, continuation: bool = False) -> None:
        self.entries.append((text, continuation))
        del self.entries[:-1000]
        self.redraw()

    def clear(self) -> None:
        self.entries.clear()
        self.query_one(RichLog).clear()

    def on_resize(self) -> None:
        self.call_after_refresh(self.redraw)

    def redraw(self) -> None:
        log = self.query_one(RichLog)
        at_end = log.is_vertical_scroll_end
        scroll_y = log.scroll_y
        log.clear()
        width = max(1, log.scrollable_content_region.width - 4)
        for index, (text, continuation) in enumerate(self.entries):
            if index and not continuation:
                log.write("", scroll_end=False)
            for number, line in enumerate(text.wrap(self.app.console, width, overflow="fold")):
                prefix = (
                    ("  └ " if number == 0 else "    ")
                    if continuation
                    else ("• " if number == 0 else "  ")
                )
                log.write(Text(prefix) + line, scroll_end=False)
        if at_end:
            log.scroll_end(animate=False)
        else:
            log.scroll_to(y=scroll_y, animate=False)

    def show_working(self, elapsed: int | None, key: str, action: str | None) -> None:
        status = self.query_one(Static)
        status.display = elapsed is not None
        if elapsed is not None:
            minutes, seconds = divmod(elapsed, 60)
            duration = f"{minutes}m {seconds}s" if minutes else f"{seconds}s"
            text = f"• Working ({duration} • {key} to interrupt)"
            if action:
                text += "\n  └ " + action
            status.update(Text(text, overflow="fold"))


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
