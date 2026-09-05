"""Live execution state and step-stream widgets."""

import json

from rich.text import Text
from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from rio_coding.tui.state import TuiState


class StepStream(RichLog):
    def __init__(self, **kwargs):
        super().__init__(wrap=True, markup=False, max_lines=3000, **kwargs)


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
