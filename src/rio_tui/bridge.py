"""Session-scoped interaction between coding extensions and the application."""

import asyncio
from collections.abc import Sequence

from rich.text import Text
from textual.containers import VerticalGroup
from textual.widget import Widget
from textual.widgets import Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from rio_coding.extensions.api import NullUiBridge


class Question(VerticalGroup):
    def __init__(self, title, choices=None, placeholder="", password=False):
        super().__init__(id="question")
        self.title, self.choices = title, choices
        self.placeholder, self.password = placeholder, password
        self.answer = asyncio.get_running_loop().create_future()

    def compose(self):
        yield Label(Text(self.title))
        if self.choices is None:
            yield Input(placeholder=self.placeholder, password=self.password)
        else:
            yield OptionList(
                *(Option(Text(str(choice)), id=str(i)) for i, choice in enumerate(self.choices))
            )

    def on_mount(self):
        self.query_one(Input if self.choices is None else OptionList).focus()

    def finish(self, value):
        if not self.answer.done():
            self.answer.set_result(value)

    def on_input_submitted(self, event):
        event.stop()
        self.finish(event.value)

    def on_option_list_option_selected(self, event):
        event.stop()
        self.finish(self.choices[int(event.option.id)])

    def on_key(self, event):
        if event.key == "escape":
            self.finish(None)
            event.stop()
            event.prevent_default()


class ViewHandle:
    def __init__(self, bridge):
        self.bridge = bridge
        self.result = asyncio.get_running_loop().create_future()
        self.widget = None

    @property
    def is_open(self):
        return not self.result.done()

    def close(self, result=None):
        if not self.result.done():
            self.result.set_result(result)
            self.bridge.view = None
            self.bridge.request_render()

    async def wait(self):
        return await asyncio.shield(self.result)


class SessionBridge(NullUiBridge):
    def __init__(self, screen):
        self.screen = screen
        self.components = {}
        self.sections = {}
        self.interceptors = []
        self.view = None
        self.dialog_lock = asyncio.Lock()
        self.render_lock = asyncio.Lock()

    @property
    def has_ui(self):
        return True

    @property
    def supports_components(self):
        return True

    @property
    def supports_sidebar(self):
        return True

    @property
    def theme(self):
        return self.screen.app.current_theme

    def notify(self, message, level="info"):
        self.screen.notify(
            message, severity={"error": "error", "warning": "warning"}.get(level, "information")
        )

    def get_prompt_text(self):
        return self.screen.query_one("#editor").text

    async def ask(self, title, choices=None, *, placeholder="", password=False, timeout=None):
        async with self.dialog_lock:
            question = Question(title, choices, placeholder, password)
            prompt = self.screen.query_one("#prompt")
            editor_row = prompt.query_one("#prompt-box")
            self.screen.waiting = True
            self.screen.app.update_sessions()
            editor_row.display = False
            await prompt.mount(question, before=editor_row)
            try:
                return await asyncio.wait_for(asyncio.shield(question.answer), timeout)
            except TimeoutError:
                return None
            finally:
                question.answer.cancel()
                self.screen.waiting = False
                await question.remove()
                editor_row.display = True
                if self.screen.app.screen is self.screen:
                    self.screen.query_one("#editor").focus()
                self.screen.app.update_sessions()

    async def select(self, title, options: Sequence[str], *, timeout=None):
        return await self.ask(title, list(options), timeout=timeout) if options else None

    async def confirm(self, title, message, *, timeout=None):
        return (
            await self.select(f"{title}\n{message}", ["Cancel", "Confirm"], timeout=timeout)
            == "Confirm"
        )

    async def input(self, title, placeholder="", *, timeout=None):
        return await self.ask(title, placeholder=placeholder, timeout=timeout)

    def make_widget(self, content):
        widget = content(self.theme) if callable(content) else Static("\n".join(content))
        if not isinstance(widget, Widget):
            raise TypeError("Component factory must return a Textual widget")
        return widget

    def set_slot_widget(self, key, content, *, placement="above_prompt"):
        if placement not in {"above_prompt", "below_prompt"}:
            raise ValueError("Unknown component placement")
        if content is None:
            self.components.pop(key, None)
        else:
            self.components[key] = placement, self.make_widget(content)
        self.request_render()

    def set_sidebar_section(self, extension_name, key, *, title, content):
        self.sections[extension_name, key] = VerticalGroup(
            Label(Text(title)), self.make_widget(content)
        )
        self.request_render()

    def remove_sidebar_section(self, extension_name, key):
        self.sections.pop((extension_name, key), None)
        self.request_render()

    def open_main_view(self, factory):
        if self.view:
            self.view.close()
        handle = ViewHandle(self)
        self.view = handle
        try:
            handle.widget = factory(handle, self.theme)
            if not isinstance(handle.widget, Widget):
                raise TypeError("View factory must return a Textual widget")
        except Exception:
            handle.close()
            raise
        self.request_render()
        return handle

    def register_key_interceptor(self, handler):
        self.interceptors.append(handler)

        def remove():
            if handler in self.interceptors:
                self.interceptors.remove(handler)

        return remove

    def clear_components(self):
        self.components.clear()
        self.sections.clear()
        self.interceptors.clear()
        if self.view:
            self.view.close()
        self.request_render()

    def request_render(self):
        if self.screen.is_mounted and not self.screen.closed:
            self.screen.call_later(self.render)

    async def render(self):
        async with self.render_lock:
            destinations = {
                "#above-prompt": [w for p, w in self.components.values() if p == "above_prompt"],
                "#below-prompt": [w for p, w in self.components.values() if p == "below_prompt"],
                "#extension-sidebar": list(self.sections.values()),
                "#extension-view": [self.view.widget] if self.view else [],
            }
            for selector, widgets in destinations.items():
                container = self.screen.query_one(selector)
                for child in list(container.children):
                    if child not in widgets:
                        await child.remove()
                for widget in widgets:
                    if not widget.is_mounted:
                        await container.mount(widget)
            self.screen.query_one("#conversation").display = self.view is None
            self.screen.query_one("#extension-view").display = self.view is not None
