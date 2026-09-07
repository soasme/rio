"""Searchable choices, file search, previews, and compact preferences."""

import asyncio
from dataclasses import replace
from pathlib import Path

from rich.syntax import Syntax
from rich.text import Text
from textual import work
from textual.containers import HorizontalGroup, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Markdown, OptionList, Select, Static
from textual.widgets.option_list import Option
from textual_diff_view import DiffView


class Picker(ModalScreen[str | None]):
    BINDINGS = [("escape", "dismiss(None)", "Back")]

    def __init__(self, title, choices):
        super().__init__()
        self.heading, self.choices = title, list(choices)

    def compose(self):
        with Vertical(classes="dialog picker"):
            yield Label(self.heading, classes="dialog-title")
            yield Input(placeholder="Search", id="search")
            yield OptionList(id="choices")

    def on_mount(self):
        self.filter("")
        self.query_one(Input).focus()

    def filter(self, value):
        options = self.query_one(OptionList)
        options.clear_options()
        options.add_options(
            [
                Option(Text(label), id=str(index))
                for index, (label, _) in enumerate(self.choices)
                if value.casefold() in label.casefold()
            ]
        )
        if options.option_count:
            options.highlighted = 0

    def on_input_changed(self, event):
        self.filter(event.value)

    def on_key(self, event):
        if self.query_one(Input).has_focus and event.key in {"up", "down"}:
            options = self.query_one(OptionList)
            if event.key == "up":
                options.action_cursor_up()
            else:
                options.action_cursor_down()
            event.stop()
            event.prevent_default()

    def on_input_submitted(self):
        options = self.query_one(OptionList)
        if options.option_count:
            self.dismiss(
                self.choices[int(options.get_option_at_index(options.highlighted or 0).id)][1]
            )

    def on_option_list_option_selected(self, event):
        self.dismiss(self.choices[int(event.option.id)][1])


class FilePicker(Picker):
    """Fuzzy project paths in a compact popover above the prompt."""

    BINDINGS = [("ctrl+o", "preview", "Preview")]

    def action_preview(self):
        options = self.query_one(OptionList)
        if options.option_count:
            index = int(options.get_option_at_index(options.highlighted or 0).id)
            self.app.push_screen(Preview(self.cwd / self.choices[index][1]))

    def __init__(self, cwd):
        super().__init__("Project files", [])
        self.cwd = Path(cwd)

    def on_mount(self):
        super().on_mount()
        self.index()

    @work
    async def index(self):
        import os

        def walk():
            paths = []
            for directory, dirs, files in os.walk(self.cwd):
                dirs[:] = [
                    d
                    for d in dirs
                    if d not in {".git", ".venv", ".worktrees", "node_modules", "__pycache__"}
                ]
                paths.extend(str((Path(directory) / file).relative_to(self.cwd)) for file in files)
                if len(paths) >= 20000:
                    break
            return sorted(paths)

        self.choices = [(path, path) for path in await asyncio.to_thread(walk)]
        self.filter(self.query_one(Input).value)

    def filter(self, value):
        def score(path):
            characters = iter(path.casefold())
            return all(char in characters for char in value.casefold())

        options = self.query_one(OptionList)
        options.clear_options()
        options.add_options(
            [
                Option(Text(path), id=str(index))
                for index, (path, _) in enumerate(self.choices)
                if score(path)
            ][:100]
        )
        if options.option_count:
            options.highlighted = 0


class Preferences(ModalScreen):
    BINDINGS = [("escape", "dismiss(None)", "Cancel")]

    def __init__(self, settings):
        super().__init__()
        self.settings = settings

    def compose(self):
        with Vertical(classes="dialog preferences"):
            yield Label("Settings", classes="dialog-title")
            with VerticalScroll(id="preference-fields"):
                yield Label("Appearance")
                yield Select(
                    [(name, name) for name in self.app.available_themes],
                    value=self.settings.theme,
                    allow_blank=False,
                    id="theme",
                )
                yield Checkbox("Conversation column", self.settings.column, id="column")
                yield Checkbox(
                    "Show plan and project sidebar", self.settings.sidebar, id="sidebar-setting"
                )
                yield Checkbox("Show thinking blocks", self.settings.thoughts, id="thoughts")
                yield Checkbox(
                    "Bell when a turn finishes", self.settings.notifications, id="notifications"
                )
                yield Label("Diff view")
                yield Select(
                    [(name, name) for name in ("auto", "unified", "split")],
                    value=self.settings.diff_layout,
                    allow_blank=False,
                    id="diff-layout",
                )
            with HorizontalGroup():
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event):
        if event.button.id == "cancel":
            self.dismiss(None)
        else:
            changes = {
                name: self.query_one("#" + name, Checkbox).value
                for name in ("column", "thoughts", "notifications")
            }
            self.dismiss(
                replace(
                    self.settings,
                    **changes,
                    sidebar=self.query_one("#sidebar-setting", Checkbox).value,
                    theme=self.query_one("#theme", Select).value,
                    diff_layout=self.query_one("#diff-layout", Select).value,
                )
            )


class Preview(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Close")]

    def __init__(self, path):
        super().__init__()
        self.path = Path(path)

    def compose(self):
        with VerticalScroll(classes="dialog preview"):
            yield Label(str(self.path), markup=False)
            try:
                text = self.path.read_bytes()[:200000].decode()
            except (OSError, UnicodeError) as error:
                yield Static(Text(str(error)))
            else:
                if self.path.suffix in {".md", ".markdown"}:
                    yield Markdown(text)
                else:
                    yield Static(
                        Syntax(text, Syntax.guess_lexer(str(self.path), text), line_numbers=True)
                    )


class Changes(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Close"), ("s", "staged", "Staged / working tree")]

    def __init__(self, cwd):
        super().__init__()
        self.cwd = cwd
        self.staged = False

    def compose(self):
        with VerticalScroll(classes="dialog preview", id="changes"):
            yield Label("Working tree", id="changes-title")
            yield Vertical(id="diffs")

    def on_mount(self):
        self.load()

    def action_staged(self):
        self.staged = not self.staged
        self.load()

    async def git(self, *args):
        process = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=self.cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            output, _ = await process.communicate()
            return output.decode("utf-8", "replace")
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    @work(exclusive=True)
    async def load(self):
        self.query_one("#changes-title", Label).update(
            "Staged changes" if self.staged else "Working tree"
        )
        container = self.query_one("#diffs")
        await container.remove_children()
        paths = await self.git("diff", "--name-only", "-z", *(["--cached"] if self.staged else []))
        for path in filter(None, paths.split("\0")):
            before = await self.git("show", f"HEAD:{path}" if self.staged else f":{path}")
            if self.staged:
                after = await self.git("show", f":{path}")
            else:
                try:
                    after = (Path(self.cwd) / path).read_text()
                except (OSError, UnicodeError):
                    after = ""
            layout = self.app.settings.diff_layout
            await container.mount(
                DiffView(
                    path, path, before, after, split=layout == "split", auto_split=layout == "auto"
                )
            )
        if not container.children:
            await container.mount(Static("No changes"))
