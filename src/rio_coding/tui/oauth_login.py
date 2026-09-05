"""Browser authentication with automatic callback and manual redirect entry."""

import asyncio

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from rio_coding.auth_commands import login_provider
from rio_coding.oauth_types import OAuthLoginCallbacks


class OAuthLoginScreen(ModalScreen[str | Exception | None]):
    BINDINGS = [("escape", "cancel", "Cancel login")]
    CSS = """
    OAuthLoginScreen { align: center middle; }
    #oauth-login { width: 86; max-width: 95%; height: auto; max-height: 95%;
                   padding: 1 2; border: solid $primary; background: $surface; }
    #oauth-url { margin: 1 0; }
    """

    def __init__(self, provider_name: str):
        super().__init__()
        self.provider_name = provider_name
        self._input_future = None
        self._login_worker = None
        self._pending_input = None
        self._allow_empty = False

    def compose(self) -> ComposeResult:
        with Vertical(id="oauth-login"):
            yield Static(f"Login: {self.provider_name}")
            yield Static("Starting authentication…", id="oauth-status")
            yield Static("", id="oauth-url", markup=False)
            yield Input(
                placeholder="Paste full redirect URL or authorization code", id="oauth-code"
            )
            yield Static(
                "The dialog closes automatically when login completes. If the browser has\n"
                "finished but this stays open, paste its redirect URL above and press Enter.\n"
                "Escape cancels login."
            )

    def on_mount(self):
        self.query_one(Input).focus()
        self._login_worker = self.authenticate()

    def on_unmount(self):
        if self._input_future is not None and not self._input_future.done():
            self._input_future.cancel()
        if self._login_worker is not None:
            self._login_worker.cancel()

    def on_input_submitted(self, event: Input.Submitted):
        event.stop()
        value = event.value.strip()
        if not value and not self._allow_empty:
            return
        event.input.value = ""
        if self._input_future is not None and not self._input_future.done():
            self._input_future.set_result(value)
        else:
            self._pending_input = value

    async def manual_input(self):
        if self._pending_input is not None:
            value, self._pending_input = self._pending_input, None
            return value
        self._input_future = asyncio.get_running_loop().create_future()
        return await self._input_future

    def show_auth(self, info):
        self.query_one("#oauth-url", Static).update(Text(info.url))
        self.show_progress(info.instructions or "Complete authentication in your browser.")

    def show_device_code(self, info):
        self.query_one("#oauth-url", Static).update(Text(info.verification_uri))
        self.show_progress(f"Enter this code in your browser: {info.user_code}")

    def show_progress(self, message):
        self.query_one("#oauth-status", Static).update(Text(message))

    async def prompt(self, value):
        self.show_progress(value.message)
        self._allow_empty = value.allow_empty
        try:
            return await self.manual_input()
        finally:
            self._allow_empty = False

    async def select(self, value):
        from rio_coding.tui.widgets import CommandPicker

        return await self.app.ui_bridge._dialog(
            CommandPicker(value.message, [(option.label, option.id) for option in value.options]),
            None,
        )

    @work
    async def authenticate(self):
        try:
            result = await login_provider(
                self.provider_name,
                callbacks=OAuthLoginCallbacks(
                    on_auth=self.show_auth,
                    on_device_code=self.show_device_code,
                    on_prompt=self.prompt,
                    on_select=self.select,
                    on_progress=self.show_progress,
                    on_manual_code_input=self.manual_input,
                ),
            )
        except Exception as error:
            self.dismiss(error)
        else:
            self.dismiss(result)

    def action_cancel(self):
        if self._login_worker is not None:
            self._login_worker.cancel()
        self.dismiss(None)
