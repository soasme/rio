"""Prompt, completion popover, shell mode, and compact session information."""

import shlex
from pathlib import Path

from rich.text import Text
from textual.binding import Binding
from textual.containers import HorizontalGroup, VerticalGroup
from textual.message import Message
from textual.widgets import Label, OptionList, TextArea
from textual.widgets.option_list import Option

COMMANDS = {
    "new": "New session",
    "sessions": "Resume a session",
    "settings": "Preferences",
    "model": "Choose model",
    "provider": "Choose provider",
    "thinking": "Thinking level",
    "files": "Find project files",
    "diff": "Review changes",
    "shell": "Enter shell mode",
    "skills": "Use a skill",
    "prompts": "Prompt templates",
    "checkpoints": "Restore state",
    "state": "Inspect execution state",
    "name": "Rename session",
    "login": "Sign in",
    "logout": "Sign out",
    "reload": "Reload resources",
    "clear": "Clear conversation",
    "cancel": "Interrupt run",
    "help": "Keyboard and commands",
    "quit": "Quit",
}


#: Longest description rendered beside a name in the `/` popover.
DESCRIPTION_WIDTH = 60


class Editor(TextArea):
    BINDINGS = [
        Binding("enter", "send", "Send", priority=True),
        Binding("shift+enter,ctrl+j", "insert_line", "New line", priority=True),
        Binding("tab", "complete", "Complete", priority=True, show=False),
        Binding("up", "previous", "History", show=False),
        Binding("down", "next", "History", show=False),
        Binding("escape", "escape", "Cancel", priority=True),
    ]

    class Submit(Message):
        def __init__(self, text, shell):
            super().__init__()
            self.text, self.shell = text, shell

    class FindFile(Message):
        pass

    def __init__(self):
        super().__init__(id="editor", soft_wrap=True, tab_behavior="indent")
        self.sent = {False: [], True: []}
        self.history_index = 0
        self.draft = ""

    @property
    def prompt(self):
        return self.parent.parent

    def action_send(self):
        if self.prompt.choices:
            self.action_complete()
            return
        text = self.text.strip()
        if text:
            history = self.sent[self.prompt.shell]
            history.append(text)
            del history[:-500]
            self.history_index = len(history)
            self.post_message(self.Submit(text, self.prompt.shell))
            self.clear()

    def action_insert_line(self):
        self.insert("\n")

    def action_complete(self):
        choices = self.prompt.choices
        if choices:
            index = self.prompt.query_one(OptionList).highlighted or 0
            self.text = choices[index] + " "
            self.move_cursor(self.document.end)
            self.prompt.hide_completion()
        elif "@" in self.text or self.prompt.shell:
            self.post_message(self.FindFile())

    def action_previous(self):
        if self.prompt.choices:
            self.prompt.query_one(OptionList).action_cursor_up()
        elif self.cursor_location[0] == 0:
            self.recall(-1)
        else:
            self.action_cursor_up()

    def action_next(self):
        if self.prompt.choices:
            self.prompt.query_one(OptionList).action_cursor_down()
        elif self.cursor_location[0] == self.document.line_count - 1:
            self.recall(1)
        else:
            self.action_cursor_down()

    def recall(self, direction):
        history = self.sent[self.prompt.shell]
        if self.history_index == len(history):
            self.draft = self.text
        self.history_index = max(0, min(len(history), self.history_index + direction))
        self.text = history[self.history_index] if self.history_index < len(history) else self.draft
        self.move_cursor(self.document.end)

    def action_escape(self):
        if self.prompt.choices:
            self.prompt.hide_completion()
        elif self.prompt.shell:
            self.prompt.set_shell(False)
        else:
            self.screen.action_interrupt()


class Prompt(VerticalGroup):
    def __init__(self, cwd):
        super().__init__(id="prompt")
        self.cwd = Path(cwd)
        self.shell = False
        self.choices = []
        #: Loaded skills and prompt templates, kept alongside `COMMANDS` so the
        #: popover offers everything `/<name>` can actually resolve to.
        self.resource_commands = {}

    @property
    def commands(self):
        """Every `/<name>` the popover offers, built-ins first."""
        return {**COMMANDS, **self.resource_commands}

    def set_resource_commands(self, commands):
        """Replace the loaded-resource half of the completion list."""
        self.resource_commands = {
            name: description for name, description in commands.items() if name not in COMMANDS
        }

    def compose(self):
        yield OptionList(id="completion")
        with HorizontalGroup(id="prompt-box"):
            yield Label("❯", id="prompt-marker")
            yield Editor()
        with HorizontalGroup(id="info-bar"):
            yield Label("rio", id="model-info")
            yield Label(str(self.cwd), id="cwd-info", markup=False)
            yield Label("", id="run-info", markup=False)
            yield Label("", id="mode-info", markup=False)

    def on_mount(self):
        self.hide_completion()

    def set_shell(self, enabled):
        self.shell = enabled
        self.set_class(enabled, "shell-mode")
        self.query_one("#prompt-marker", Label).update("$" if enabled else "❯")
        editor = self.query_one(Editor)
        editor.history_index = len(editor.sent[enabled])
        editor.focus()

    def on_text_area_changed(self, event):
        text = event.text_area.text
        if text == "!" and not self.shell:
            event.text_area.clear()
            self.set_shell(True)
            return
        commands = self.commands
        if not self.shell and text.startswith("/") and " " not in text and "\n" not in text:
            prefix = text[1:].lower()
            self.choices = [
                "/" + command for command in commands if command.lower().startswith(prefix)
            ]
        else:
            self.choices = []
        options = self.query_one(OptionList)
        options.clear_options()
        options.add_options(
            [
                Option(Text(f"{command:16} {_summarize(commands[command[1:]])}"))
                for command in self.choices
            ]
        )
        options.display = bool(self.choices)
        if self.choices:
            options.highlighted = 0

    def on_option_list_option_selected(self, event):
        self.query_one(Editor).action_complete()
        self.query_one(Editor).focus()

    def hide_completion(self):
        self.choices = []
        self.query_one(OptionList).display = False

    def insert_path(self, path):
        editor = self.query_one(Editor)
        prefix = "" if self.shell or editor.text.endswith("@") else "@"
        editor.insert(prefix + shlex.quote(str(path)) + " ")
        editor.focus()

    def show_status(self, session, status):
        self.query_one("#model-info", Label).update(Text(str(session.model)))
        self.query_one("#run-info", Label).update(Text(status))
        self.query_one("#mode-info", Label).update(
            Text(getattr(session, "thinking_level", "") or "")
        )


def _summarize(description):
    """Shorten a skill or template description to one popover line."""
    text = " ".join((description or "").split())
    if len(text) <= DESCRIPTION_WIDTH:
        return text
    return text[: DESCRIPTION_WIDTH - 1].rstrip() + "\u2026"
