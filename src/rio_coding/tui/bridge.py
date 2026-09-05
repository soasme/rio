"""Interactive dialog bridge for coding extensions."""

import asyncio
from collections.abc import Sequence

from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Input, Label

from rio_coding.extensions.api import NullUiBridge
from rio_coding.tui.widgets import CommandPicker


class TextPrompt(ModalScreen[str | None]):
    BINDINGS = [("escape", "cancel", "Cancel")]
    CSS = """
    TextPrompt { align: center middle; }
    #text-prompt { width: 70; max-width: 95%; height: auto; padding: 1;
                   border: solid $primary; background: $surface; }
    """

    def __init__(self, title: str, placeholder: str):
        super().__init__()
        self.prompt_title = title
        self.placeholder = placeholder

    def compose(self) -> ComposeResult:
        from rich.text import Text
        from textual.containers import Vertical

        with Vertical(id="text-prompt"):
            yield Label(Text(self.prompt_title))
            yield Input(placeholder=self.placeholder)

    def on_mount(self):
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted):
        self.dismiss(event.value)

    def action_cancel(self):
        self.dismiss(None)


class TuiBridge(NullUiBridge):
    def __init__(self, app):
        self.app = app
        self.slots = {}
        self.sections = {}
        self.interceptors = []
        self.main_view = None
        self._render_lock = asyncio.Lock()

    @property
    def has_ui(self):
        return True

    @property
    def theme(self):
        return self.app.settings.resolved_theme

    def notify(self, message, level="info"):
        self.app.notify(
            message, severity={"error": "error", "warning": "warning"}.get(level, "information")
        )

    def get_prompt_text(self):
        return self.app.query_one("#prompt", Input).value

    @property
    def supports_components(self):
        return True

    @property
    def supports_sidebar(self):
        return True

    def _widget(self, content):
        from rich.errors import MarkupError
        from rich.text import Text
        from textual.widget import Widget
        from textual.widgets import Static

        if callable(content):
            widget = content(self.theme)
            if not isinstance(widget, Widget):
                raise TypeError("Extension factory must return a Textual Widget")
            return widget
        text = "\n".join(content)
        try:
            rendered = Text.from_markup(text)
        except MarkupError:
            rendered = Text(text)
        return Static(rendered)

    def set_slot_widget(self, key, content, *, placement="above_prompt"):
        if placement not in {"above_prompt", "below_prompt"}:
            raise ValueError("Unknown extension widget placement")
        try:
            if content is None:
                self.slots.pop(key, None)
            else:
                self.slots[key] = (placement, self._widget(content))
            self.request_render()
        except Exception as error:
            self.notify(f"Extension widget failed: {error}", "error")

    def set_sidebar_section(self, extension_name, key, *, title, content):
        from rich.text import Text
        from textual.containers import Vertical
        from textual.widgets import Static

        try:
            section = Vertical(Static(Text(title, style="bold")), self._widget(content))
            section.styles.height = "auto"
            self.sections[(extension_name, key)] = section
            self.request_render()
        except Exception as error:
            self.notify(f"Extension sidebar failed: {error}", "error")

    def remove_sidebar_section(self, extension_name, key):
        self.sections.pop((extension_name, key), None)
        self.request_render()

    def open_main_view(self, factory):
        from textual.widget import Widget

        if self.main_view is not None:
            self.main_view.close()
        handle = MainView(self)
        self.main_view = handle
        try:
            handle.widget = factory(handle, self.theme)
            if not isinstance(handle.widget, Widget):
                raise TypeError("Extension factory must return a Textual Widget")
            self.request_render()
        except Exception as error:
            handle.close()
            self.notify(f"Extension view failed: {error}", "error")
        return handle

    def register_key_interceptor(self, handler):
        self.interceptors.append(handler)

        def unsubscribe():
            if handler in self.interceptors:
                self.interceptors.remove(handler)

        return unsubscribe

    def intercept_key(self, event):
        for handler in tuple(self.interceptors):
            try:
                if handler(event, self.get_prompt_text()):
                    return True
            except Exception as error:
                self.interceptors.remove(handler)
                self.notify(f"Extension key handler failed: {error}", "error")
        return False

    def quarantine(self, error):
        """Contain asynchronous exceptions raised by extension-owned widgets."""
        from textual.widget import Widget

        roots = [widget for _, widget in self.slots.values()] + list(self.sections.values())
        if self.main_view and self.main_view.widget:
            roots.append(self.main_view.widget)
        traceback = error.__traceback__
        while traceback is not None:
            owner = traceback.tb_frame.f_locals.get("self")
            if isinstance(owner, Widget):
                lineage = [owner, *owner.ancestors]
                if any(root in lineage for root in roots):
                    for root in roots:
                        root.display = False
                    self.clear_components()
                    self.notify(f"Extension widget failed and was removed: {error}", "error")
                    return True
            traceback = traceback.tb_next
        return False

    def clear_components(self):
        self.slots.clear()
        self.sections.clear()
        self.interceptors.clear()
        if self.main_view is not None:
            self.main_view.close()
        self.request_render()

    def request_render(self):
        if self.app.is_running:
            self.app.call_later(self._reconcile)

    async def _reconcile(self):
        async with self._render_lock:
            desired = {
                "#above-prompt": [
                    w for placement, w in self.slots.values() if placement == "above_prompt"
                ],
                "#below-prompt": [
                    w for placement, w in self.slots.values() if placement == "below_prompt"
                ],
                "#extension-sidebar": list(self.sections.values()),
                "#extension-main": [self.main_view.widget]
                if self.main_view and self.main_view.widget
                else [],
            }
            for selector, widgets in desired.items():
                container = self.app.query_one(selector)
                for child in list(container.children):
                    if child not in widgets:
                        await child.remove()
                for widget in widgets:
                    if not widget.is_mounted:
                        await container.mount(widget)
                    widget.refresh(layout=True)
            self.app.query_one("#extension-main").display = self.main_view is not None
            self.app.query_one("#stream").display = self.main_view is None

    async def _dialog(self, screen, timeout):
        result = asyncio.get_running_loop().create_future()

        def completed(value):
            if not result.done():
                result.set_result(value)

        self.app.push_screen(screen, completed)
        try:
            return await asyncio.wait_for(result, timeout)
        except TimeoutError:
            return None
        finally:
            if result.cancelled() and screen.is_mounted:
                screen.dismiss(None)

    async def select(self, title: str, options: Sequence[str], *, timeout=None):
        if not options:
            return None
        return await self._dialog(CommandPicker(title, [(item, item) for item in options]), timeout)

    async def confirm(self, title: str, message: str, *, timeout=None):
        choice = await self.select(f"{title}\n{message}", ["Cancel", "Confirm"], timeout=timeout)
        return choice == "Confirm"

    async def input(self, title: str, placeholder: str = "", *, timeout=None):
        return await self._dialog(TextPrompt(title, placeholder), timeout)


class MainView:
    def __init__(self, bridge):
        self.bridge = bridge
        self.result = asyncio.get_running_loop().create_future()
        self.widget = None

    @property
    def is_open(self):
        return not self.result.done()

    def close(self, result=None):
        if not self.is_open:
            return
        self.result.set_result(result)
        if self.bridge.main_view is self:
            self.bridge.main_view = None
        self.bridge.request_render()

    async def wait(self):
        return await asyncio.shield(self.result)
