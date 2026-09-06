"""Interactive Textual frontend for the SKILL.state coding runtime."""

import json
from copy import deepcopy
from dataclasses import replace
from time import monotonic

from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Header, Input

from rio_coding.session import CodingSession
from rio_coding.thinking import THINKING_LEVELS, normalize_thinking_level
from rio_coding.tui.adapter import TuiEventAdapter
from rio_coding.tui.bridge import TuiBridge
from rio_coding.tui.config import TuiSettings, load_tui_settings, save_tui_settings
from rio_coding.tui.local_backends import LocalBackendPickerScreen, LocalBackendScreen
from rio_coding.tui.state import StepStreamItem, TuiState
from rio_coding.tui.terminal_notification import TerminalNotificationController
from rio_coding.tui.terminal_title import TerminalTitleController
from rio_coding.tui.themes import available_tui_theme_names, textual_theme_for_tui_theme
from rio_coding.tui.widgets import CommandPicker, StateSidebar, StepStream


class RioTuiApp(App[None]):
    TITLE = "rio"
    CSS = """
    #local-backend-picker,
    #local-backend-screen,
    #local-configure-screen,
    #local-confirm-screen,
    #local-model-action-screen,
    #local-search-results-screen {
        width: 82;
        max-width: 92%;
        height: auto;
        max-height: 82%;
        padding: 1 2;
        background: $rio-chrome-background;
        border: tall $rio-border;
    }

    #local-backend-picker-title,
    #local-backend-title,
    #local-configure-title,
    #local-confirm-title,
    #local-model-action-title,
    #local-search-results-title {
        height: auto;
        color: $rio-chrome-text;
        text-style: bold;
        margin-bottom: 1;
    }

    #local-backend-picker-help,
    #local-backend-help,
    #local-configure-screen Label,
    #local-confirm-message,
    #local-search-results-help {
        color: $rio-muted-text;
    }

    #local-backend-list,
    #local-backend-status,
    #local-backend-progress,
    #local-model-list,
    #local-action-menu,
    #local-confirm-list,
    #local-choice-list,
    #local-search-results-list,
    #local-configure-screen Input,
    #local-configure-screen Select,
    #local-model-action-input {
        background: $rio-transcript-background;
        border: tall $rio-border;
        margin-top: 1;
    }

    #local-backend-list,
    #local-model-list,
    #local-action-menu,
    #local-confirm-list,
    #local-choice-list,
    #local-search-results-list {
        height: auto;
        max-height: 16;
    }

    #local-model-list,
    #local-action-menu {
        max-height: 10;
    }

    #local-model-list:focus,
    #local-action-menu:focus {
        border: tall $rio-accent;
    }

    #local-model-list.local-section-inactive > ListItem.-highlight,
    #local-action-menu.local-section-inactive > ListItem.-highlight,
    #local-model-list.local-section-inactive > ListItem.-highlight Label,
    #local-action-menu.local-section-inactive > ListItem.-highlight Label {
        background: $rio-transcript-background;
        color: $rio-chrome-text;
    }

    #local-model-section-title,
    #local-action-section-title {
        height: 1;
        margin-top: 1;
        color: $rio-chrome-text;
        text-style: bold;
    }

    #local-backend-progress-bar {
        width: 100%;
        margin-top: 1;
    }

    #local-backend-progress-bar Bar {
        width: 1fr;
    }

    #local-backend-progress-bar Bar > .bar--bar,
    #local-backend-progress-bar Bar > .bar--complete,
    #local-backend-progress-bar Bar > .bar--indeterminate {
        color: $rio-accent;
        background: $rio-border;
    }

    #local-backend-picker-footer,
    #local-backend-footer,
    #local-configure-footer,
    #local-confirm-footer,
    #local-model-action-footer,
    #local-search-results-footer {
        height: 1;
        margin-top: 1;
        color: $rio-muted-text;
    }

    #local-backend-progress {
        min-height: 1;
        color: $rio-muted-text;
    }
    ModalScreen { align: center middle; }
    #body { height: 1fr; }
    #stream { width: 2fr; }
    #sidebar-scroll { width: 1fr; min-width: 25; border-left: solid $primary; }
    #sidebar { padding: 1; height: auto; }
    #above-prompt, #below-prompt, #extension-sidebar { height: auto; }
    #extension-main { width: 2fr; display: none; }
    """
    BINDINGS = [("escape", "cancel_run", "Cancel"), ("ctrl+d", "quit", "Quit")]

    def __init__(
        self,
        session: CodingSession,
        *,
        initial_prompt: str | None = None,
        settings: TuiSettings | None = None,
    ) -> None:
        super().__init__()
        self.session = session
        self.ui_bridge = TuiBridge(self)
        self.initial_prompt = initial_prompt
        self.settings = settings or load_tui_settings(session.config.paths)
        self.adapter = TuiEventAdapter(TuiState(state=deepcopy(session.state)))
        self._busy = False
        self._working_since: float | None = None
        self._terminal_title = TerminalTitleController()
        self._turn_notifications = TerminalNotificationController(self.settings.turn_notification)
        theme = self.settings.resolved_theme
        self.register_theme(textual_theme_for_tui_theme(theme.name))
        self.theme = theme.name
        self.bind(self.settings.keybindings.cancel, "cancel_run", description="Cancel")
        self.bind(self.settings.keybindings.quit, "quit", description="Quit")

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="body"):
            if self.settings.sidebar_position == "left":
                with VerticalScroll(id="sidebar-scroll"):
                    yield StateSidebar(id="sidebar", markup=False)
                    yield Vertical(id="extension-sidebar")
            yield StepStream(id="stream")
            yield Vertical(id="extension-main")
            if self.settings.sidebar_position != "left":
                with VerticalScroll(id="sidebar-scroll"):
                    yield StateSidebar(id="sidebar", markup=False)
                    yield Vertical(id="extension-sidebar")
        yield Vertical(id="above-prompt")
        yield Input(
            placeholder="Ask rio, or /help. Input while running steers the next step.", id="prompt"
        )
        yield Vertical(id="below-prompt")
        yield Footer()

    def _handle_exception(self, error):
        # Textual exposes no public widget-error boundary. Keep extension failures
        # inside their host containers while preserving ordinary app failures.
        if self.ui_bridge.quarantine(error):
            return
        super()._handle_exception(error)

    async def on_event(self, event):
        if (
            isinstance(event, events.Key)
            and not event.is_forwarded
            and event.key not in {"ctrl+c", "ctrl+d"}
            and len(self.screen_stack) <= 1
            and self.ui_bridge.intercept_key(event)
        ):
            event.stop()
            event.prevent_default()
            return
        await super().on_event(event)

    def on_mount(self) -> None:
        self.set_interval(1, self.refresh_working)
        if hasattr(self.session, "extensions"):
            self.session.extensions.set_ui_bridge(self.ui_bridge)
        self.query_one("#sidebar-scroll").display = self.settings.sidebar_position != "off"
        self.refresh_state()
        self.query_one(Input).focus()
        if self.initial_prompt:
            self.submit_prompt(self.initial_prompt)

    def on_unmount(self) -> None:
        self.session.cancel()
        self._terminal_title.restore()

    @property
    def working_elapsed(self) -> int | None:
        if self._working_since is None:
            return None
        return int(monotonic() - self._working_since)

    def refresh_working(self) -> None:
        self.query_one(StepStream).show_working(
            self.working_elapsed,
            self.settings.keybindings.cancel,
            self.adapter.state.active_action,
        )

    def refresh_state(self) -> None:
        if self._busy and self._working_since is None:
            self._working_since = monotonic()
        elif not self._busy:
            self._working_since = None
        self.refresh_working()
        self._terminal_title.update(
            getattr(self.session, "session_title", None), running=self._busy
        )
        self.query_one(StateSidebar).show_state(
            self.adapter.state,
            model=self.session.model,
            footprint=self.session.step_footprint.total_tokens,
        )

    def write_item(self, item: StepStreamItem) -> None:
        style = self.settings.resolved_theme.role_styles[item.role].body
        self.query_one(StepStream).write(
            Text(item.text, style=style), continuation=item.continuation
        )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if text:
            event.input.value = ""
            self.submit_prompt(text)

    def submit_prompt(self, text: str) -> None:
        if text in {"/quit", "/exit"}:
            self.action_quit()
        elif text in {
            "/theme",
            "/model",
            "/provider",
            "/thinking",
            "/skills",
            "/prompts",
            "/sessions",
        }:
            self.show_picker(text)
        elif text.startswith(
            (
                "/theme ",
                "/model ",
                "/provider ",
                "/thinking ",
                "/resume ",
                "/name ",
                "/login ",
                "/logout ",
            )
        ) or text in {"/new", "/reload"}:
            if self._busy:
                self.write_item(StepStreamItem("error", "Cancel the active run first."))
            else:
                self._busy = True
                self.configure_command(text)
        elif text == "/local":
            registry = self.session.extensions.local_backend_registry
            self.push_screen(
                LocalBackendPickerScreen(registry, theme=self.settings.resolved_theme),
                lambda backend: self.call_later(self.open_local_backend, backend),
            )
        elif text == "/help":
            self.write_item(
                StepStreamItem(
                    "custom",
                    "/state · /session · /checkpoints · /restore ID · /cancel · /clear · /quit\n"
                    "/model · /provider · /thinking · /theme · /skills · /prompts · /sessions\n"
                    "/local · /new · /name TITLE · /login PROVIDER · /logout PROVIDER\n"
                    "Send input during a run to steer its next step.",
                )
            )
        elif text in {"/tools", "/system", "/diagnostics"}:
            if text == "/tools":
                output = "\n".join(
                    f"{tool.name}: {tool.description}" for tool in self.session.tools
                )
            elif text == "/system":
                output = self.session.system_prompt
            else:
                output = "\n".join(str(item) for item in self.session.resource_diagnostics)
            self.write_item(StepStreamItem("custom", output or "No diagnostics."))
        elif text == "/state":
            self.write_item(StepStreamItem("custom", json.dumps(self.session.state, indent=2)))
        elif text == "/session":
            self.write_item(
                StepStreamItem(
                    "custom",
                    f"{self.session.session_name}\n{self.session.state_summary}\n"
                    f"Next prompt: ~{self.session.step_footprint.total_tokens:,} tokens",
                )
            )
        elif text == "/cancel":
            self.action_cancel_run()
        elif text == "/clear":
            self.query_one(StepStream).clear()
            self.adapter.state.items.clear()
        elif text == "/checkpoints" or text.startswith("/restore "):
            if self._busy:
                self.write_item(StepStreamItem("error", "Cancel the active run first."))
            else:
                self._busy = True
                self.checkpoint_command(text)
        elif self.dispatch_registered_command(text):
            pass
        elif self._busy:
            self.session.queue_steering_message(text)
            self.adapter.state.queued = self.session.queued_message_count
            self.write_item(StepStreamItem("status", "Queued steering: " + text))
            self.refresh_state()
        else:
            self._busy = True
            self.adapter.state.running = True
            self.write_item(StepStreamItem("user", text))
            self.refresh_state()
            self.run_prompt(text)

    def dispatch_registered_command(self, text: str) -> bool:
        registry = getattr(self.session, "command_registry", None)
        if registry is None or not text.startswith("/"):
            return False
        try:
            result = registry.execute(self.session, text)
            if not result.handled:
                return False
            if result.message:
                self.write_item(StepStreamItem("custom", str(result.message)))
            if result.export_requested:
                self.export_session(result.export_destination, result.export_format)
            elif result.resume_picker_requested:
                self.show_picker("/sessions")
            elif result.tree_picker_requested:
                self.submit_prompt("/checkpoints")
            elif result.login_picker_requested:
                providers = getattr(self.session, "available_providers", ())
                self.push_screen(
                    CommandPicker("Login", [(p, f"/login {p}") for p in providers]),
                    self.picker_selected,
                )
            return True
        except Exception as error:
            self.write_item(StepStreamItem("error", str(error)))
            return True

    @work
    async def export_session(self, destination, format) -> None:
        from pathlib import Path

        from rio_coding.session_export import export_session_artifact

        try:
            path = destination or self.session.cwd / f"rio-session.{format or 'html'}"
            path = Path(path)
            if not path.is_absolute():
                path = self.session.cwd / path
            exported = export_session_artifact(
                await self.session.session_entries(),
                path,
                format=format,
                instructions=self.session.system_prompt,
                tools=self.session.tools,
                model=self.session.model,
                provider=self.session.provider_name,
            )
            self.write_item(StepStreamItem("status", f"Exported {exported}"))
        except Exception as error:
            self.write_item(StepStreamItem("error", str(error)))

    def open_local_backend(self, backend: str | None) -> None:
        if backend is None:
            return
        self.push_screen(
            LocalBackendScreen(
                self.session.extensions.local_backend_registry,
                backend,
                theme=self.settings.resolved_theme,
                on_use=self.use_local_model,
                notify_callback=lambda message, level: self.notify(message),
                is_idle=lambda: not self._busy,
            )
        )

    async def use_local_model(self, provider: str, model: str) -> None:
        if self._busy:
            raise ValueError("Cancel the active run before switching models.")
        await self.session.set_model(model, provider_name=provider)
        self.refresh_state()

    def show_picker(self, command: str) -> None:
        options: list[tuple[str, str]] = []
        if command == "/theme":
            options = [(name, f"/theme {name}") for name in available_tui_theme_names()]
        elif command in {"/model", "/provider", "/thinking"}:
            attribute = {
                "/model": "available_models",
                "/provider": "available_providers",
                "/thinking": "available_thinking_levels",
            }[command]
            fallback = THINKING_LEVELS if command == "/thinking" else ()
            options = [
                (str(name), f"{command} {name}")
                for name in getattr(self.session, attribute, fallback)
            ]
        elif command in {"/skills", "/prompts"}:
            resources = (
                self.session.skills if command == "/skills" else self.session.prompt_templates
            )
            options = [
                (
                    f"{item.name} — {item.description or ''}",
                    f"/skill:{item.name}" if command == "/skills" else f"/{item.name}",
                )
                for item in resources
            ]
        elif command == "/sessions":
            manager = getattr(self.session, "session_manager", None)
            if manager is not None:
                options = [
                    (f"{record.title or record.id} ({record.model})", f"/resume {record.id}")
                    for record in manager.list_sessions(self.session.cwd)
                ]
        if not options:
            self.write_item(StepStreamItem("status", f"No choices available for {command}."))
            return
        self.push_screen(CommandPicker(command[1:].capitalize(), options), self.picker_selected)

    def picker_selected(self, command: str | None) -> None:
        if command is not None:
            self.submit_prompt(command)

    @work
    async def configure_command(self, text: str) -> None:
        command, _, value = text.partition(" ")
        try:
            if command == "/theme":
                theme = textual_theme_for_tui_theme(value)
                self.register_theme(theme)
                self.theme = value
                self.settings = replace(self.settings, theme=value)
                save_tui_settings(self.settings, self.session.config.paths)
            elif command == "/model":
                await self.session.set_model(value)
            elif command == "/provider":
                await self.session.set_provider_name(value)
            elif command == "/thinking":
                await self.session.set_thinking_level(normalize_thinking_level(value))
            elif command == "/name":
                await self.session.set_session_name(value)
            elif command == "/reload":
                await self.session.reload()
            elif command == "/new":
                await self.session.new_session()
            elif command == "/resume":
                await self.session.resume_session(value)
            elif command in {"/login", "/logout"}:
                from rio_coding.auth_commands import login_provider, logout_provider
                from rio_coding.oauth_registry import get_oauth_provider
                from rio_coding.tui.oauth_login import OAuthLoginScreen

                if command == "/login" and get_oauth_provider(value) is not None:
                    result = await self.ui_bridge._dialog(OAuthLoginScreen(value), None)
                    if isinstance(result, Exception):
                        raise result
                    if result is None:
                        self.write_item(StepStreamItem("status", "Login cancelled."))
                        return
                    self.write_item(StepStreamItem("status", result))
                    if hasattr(self.session, "set_provider_name"):
                        await self.session.set_provider_name(value)
                elif command == "/login":
                    with self.suspend():
                        await login_provider(value)
                else:
                    self.write_item(StepStreamItem("status", logout_provider(value)))
            if hasattr(self.session, "extensions"):
                self.session.extensions.set_ui_bridge(self.ui_bridge)
            self.adapter.state.state = deepcopy(self.session.state)
            self.write_item(StepStreamItem("status", f"Applied {text}"))
        except Exception as error:
            self.write_item(StepStreamItem("error", str(error)))
        finally:
            self._busy = False
            self.refresh_state()

    @work
    async def checkpoint_command(self, text: str) -> None:
        try:
            if text == "/checkpoints":
                entries = await self.session.checkpoints()
                if entries:
                    self.push_screen(
                        CommandPicker(
                            "Restore checkpoint", [(e.id, f"/restore {e.id}") for e in entries]
                        ),
                        self.picker_selected,
                    )
                else:
                    self.write_item(StepStreamItem("status", "No checkpoints yet."))
            else:
                event = await self.session.restore(text.split(maxsplit=1)[1])
                for item in self.adapter.consume(event):
                    self.write_item(item)
                self.refresh_state()
        except Exception as error:
            self.write_item(StepStreamItem("error", str(error)))

        finally:
            self._busy = False

    @work
    async def run_prompt(self, text: str) -> None:
        try:
            async for event in self.session.prompt(text):
                for item in self.adapter.consume(event):
                    self.write_item(item)
                self.refresh_state()
        except Exception as error:
            self.write_item(StepStreamItem("error", str(error)))
        finally:
            self._busy = False
            self.adapter.state.running = False
            self.adapter.state.active_action = None
            self.refresh_state()
            self._turn_notifications.notify_turn_finished()

    def action_cancel_run(self) -> None:
        self.session.cancel()

    def action_quit(self) -> None:
        self.session.cancel()
        self.exit()


async def run_tui_app(session: CodingSession, initial_prompt: str | None = None) -> None:
    """Run the terminal frontend inside the caller's event loop."""
    await RioTuiApp(session, initial_prompt=initial_prompt).run_async()
