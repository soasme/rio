"""The one table behind the hotkeys, the command palette, and `/help`.

`RioTuiApp.BINDINGS`, the Ctrl+K palette, the `/` popover, and the help text are
all derived from `COMMANDS`, so a command cannot reach one of them and miss the
others.
"""

from dataclasses import dataclass

#: Keys Textual names in full and a reader expects as a symbol.
KEY_NAMES = {"left_square_bracket": "[", "right_square_bracket": "]", "comma": ","}


@dataclass(frozen=True)
class Command:
    """One `/name`, with the hotkey and app action that mean the same thing."""

    name: str
    description: str
    key: str = ""
    action: str = ""
    #: Other names the same command answers to, offered alongside it.
    aliases: tuple[str, ...] = ()

    @property
    def hotkey(self):
        """The first bound key as a reader sees it: `ctrl+n` becomes `Ctrl+N`."""
        if not self.key:
            return ""
        parts = self.key.split(",")[0].split("+")
        return "+".join(KEY_NAMES.get(part, part.capitalize()) for part in parts)


COMMANDS = (
    Command("new", "New session", "ctrl+n", "new_session"),
    Command("sessions", "Resume a session", "ctrl+r", "resume", aliases=("resume",)),
    Command("next", "Next session", "ctrl+right_square_bracket,ctrl+tab", "next_session"),
    Command("prev", "Previous session", "ctrl+left_square_bracket", "previous_session"),
    Command("close", "Close session", "ctrl+w", "close_session"),
    Command("sidebar", "Toggle sidebar", "ctrl+b", "sidebar"),
    Command("settings", "Preferences", "ctrl+comma", "settings"),
    Command("theme", "Show or set the theme"),
    Command("model", "Choose model"),
    Command("scoped-models", "Choose quick-cycle models"),
    Command("provider", "Choose provider"),
    Command("local", "Manage local backends"),
    Command("thinking", "Thinking level"),
    Command("files", "Find project files", "ctrl+f", "files"),
    Command("diff", "Review changes"),
    Command("shell", "Enter shell mode"),
    Command("skills", "Use a skill"),
    Command("skill", "Insert a skill into the prompt"),
    Command("prompts", "Prompt templates"),
    Command("context", "Project context files"),
    Command("resources", "Loaded resources and diagnostics"),
    Command("tools", "Browse available tools"),
    Command("system", "Show the active instructions"),
    Command("checkpoints", "Restore state", aliases=("tree",)),
    Command("state", "Inspect execution state"),
    Command("session", "Session info and stats"),
    Command("export", "Export the session"),
    Command("name", "Rename session"),
    Command("login", "Sign in"),
    Command("logout", "Sign out"),
    Command("reload", "Reload resources"),
    Command("clear", "Clear conversation"),
    Command("cancel", "Interrupt run"),
    Command("hotkeys", "Keyboard shortcuts"),
    Command("help", "Keyboard and commands", aliases=("?",)),
    Command("quit", "Quit", "ctrl+d", "quit", aliases=("exit",)),
)

#: Description by name and alias: the built-in half of the `/` popover.
BUILTIN = {
    name: command.description for command in COMMANDS for name in (command.name, *command.aliases)
}

#: Displayed hotkey by name and alias, for the commands that have one.
HOTKEYS = {
    name: command.hotkey
    for command in COMMANDS
    if command.key
    for name in (command.name, *command.aliases)
}

#: App action by name, for the commands a hotkey also reaches.
ACTIONS = {command.name: command.action for command in COMMANDS if command.action}


def hotkey_bindings():
    """`App.BINDINGS` entries for every command with a hotkey."""
    return [
        (command.key, command.action, command.description) for command in COMMANDS if command.key
    ]


def palette_choices():
    """`(label, submitted text)` rows for the palette, hotkeys in a last column."""
    name_width = max(len(c.name) for c in COMMANDS)
    description_width = max(len(c.description) for c in COMMANDS)
    rows = []
    for command in COMMANDS:
        row = f"/{command.name:<{name_width}} · {command.description:<{description_width}}"
        rows.append(
            (f"{row} · {command.hotkey}" if command.key else row.rstrip(), "/" + command.name)
        )
    return rows


def help_message():
    """The `/help` answer, listing every hotkey the table defines."""
    keys = "\n".join(
        f"- `{command.hotkey}` · {command.description}" for command in COMMANDS if command.key
    )
    return (
        "## rio\n\n"
        "Enter sends; Shift+Enter adds a line. Type `/` for commands or `!` for shell mode. "
        "Up/Down browse prompt history. Escape interrupts the active run, and Ctrl+C "
        "interrupts a shell command.\n\n"
        "- `Ctrl+K` · Commands\n" + keys
    )
