"""Conversation application and independent session ownership."""

import asyncio
from dataclasses import replace
from pathlib import Path

from rich.text import Text
from textual import events, work
from textual.app import App
from textual.widgets import Tab, Tabs

from rio_tui.commands import hotkey_bindings, palette_choices
from rio_tui.dialogs import Picker, Preferences
from rio_tui.session import SessionScreen
from rio_tui.settings import Settings


class RioTuiApp(App[None]):
    TITLE = "rio"
    CSS_PATH = "app.tcss"
    # Ctrl+K and the COMMANDS table are the only palette.
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        *hotkey_bindings(),
        # The palette opener shows the table rather than appearing in it.
        ("ctrl+k", "commands", "Commands"),
    ]

    def __init__(self, session, *, initial_prompt=None, settings=None):
        super().__init__()
        self.initial_session = session
        self.initial_prompt = initial_prompt
        paths = getattr(getattr(session, "config", None), "paths", None)
        self.settings_path = (paths.home if paths else Path.home() / ".rio") / "tui.json"
        self.settings = settings or Settings.load(self.settings_path)
        if self.settings.theme not in self.available_themes:
            self.settings = replace(self.settings, theme="textual-dark")
        self.theme = self.settings.theme
        self.sessions = {}
        self.session_number = 0
        self.session_lock = asyncio.Lock()

    @property
    def workspace(self):
        return self.sessions[self.current_mode]

    async def on_mount(self):
        await self.add_session(self.initial_session, self.initial_prompt)

    async def add_session(self, session, initial_prompt=None):
        name = f"session-{self.session_number}"
        self.session_number += 1
        screen = SessionScreen(session, initial_prompt=initial_prompt)
        self.sessions[name] = screen
        self.add_mode(name, lambda: screen)
        await self.switch_mode(name)
        for key, pane in self.sessions.items():
            tabs = pane.query_one(Tabs)
            with tabs.prevent(Tabs.TabActivated):
                await tabs.clear()
                for other in self.sessions:
                    await tabs.add_tab(Tab(other, id=other))
                tabs.active = key
                tabs.display = len(self.sessions) > 1
        self.update_sessions()
        return name

    def update_sessions(self):
        for screen in self.sessions.values():
            if not screen.is_mounted or screen.closed:
                continue
            for key, pane in self.sessions.items():
                tabs = screen.query(f"#{key}")
                if not tabs:
                    continue
                title = getattr(pane.session, "session_title", None) or "New session"
                status = " · question" if pane.waiting else " · working" if pane.busy else ""
                tabs.first().label = Text(title + status)

    def select_session(self, name):
        if name in self.sessions and name != self.current_mode:
            self.switch_mode(name)

    @work
    async def open_session(self, session_id=None):
        async with self.session_lock:
            for key, pane in self.sessions.items():
                if session_id and getattr(pane.session, "session_id", None) == session_id:
                    self.select_session(key)
                    return
            session = None
            try:
                session = await self.workspace.session.open_session(session_id)
                await self.add_session(session)
            except Exception as error:
                if session is not None and all(
                    p.session is not session for p in self.sessions.values()
                ):
                    await session.aclose()
                self.notify(str(error), severity="error")

    @work
    async def fork_session(self, *, title=None):
        async with self.session_lock:
            session = None
            try:
                session = await self.workspace.session.fork_session(title=title)
                await self.add_session(session)
            except Exception as error:
                if session is not None and all(
                    p.session is not session for p in self.sessions.values()
                ):
                    await session.aclose()
                self.notify(str(error), severity="error")

    def action_new_session(self):
        self.open_session()

    def action_resume(self):
        self.workspace.submit("/sessions")

    def action_next_session(self):
        self.navigate(1)

    def action_previous_session(self):
        self.navigate(-1)

    def navigate(self, direction):
        keys = list(self.sessions)
        self.select_session(keys[(keys.index(self.current_mode) + direction) % len(keys)])

    @work
    async def action_close_session(self):
        async with self.session_lock:
            if len(self.sessions) == 1:
                return
            key, pane = self.current_mode, self.workspace
            await pane.shutdown()
            await self.switch_mode(next(name for name in self.sessions if name != key))
            await self.remove_mode(key)
            del self.sessions[key]
            if pane.session is not self.initial_session:
                await pane.session.aclose()
            for screen in self.sessions.values():
                tabs = screen.query_one(Tabs)
                with tabs.prevent(Tabs.TabActivated):
                    await tabs.remove_tab(key)
                    tabs.display = len(self.sessions) > 1

    def action_files(self):
        self.workspace.action_files()

    def action_sidebar(self):
        self.save_preferences(replace(self.settings, sidebar=not self.settings.sidebar))

    def action_settings(self):
        self.push_screen(Preferences(self.settings), self.save_preferences)

    def action_commands(self):
        self.push_screen(
            Picker("Commands", palette_choices()),
            lambda value: self.workspace.submit(value) if value else None,
        )

    def save_preferences(self, settings):
        if settings:
            settings.save(self.settings_path)
            self.settings = settings
            self.theme = settings.theme
            for screen in self.sessions.values():
                screen.apply_preferences()

    async def on_event(self, event):
        if (
            isinstance(event, events.Key)
            and not event.is_forwarded
            and self.current_mode in self.sessions
            and isinstance(self.screen, SessionScreen)
        ):
            bridge = self.workspace.bridge
            for interceptor in tuple(bridge.interceptors):
                try:
                    if interceptor(event, bridge.get_prompt_text()):
                        event.stop()
                        event.prevent_default()
                        return
                except Exception as error:
                    bridge.interceptors.remove(interceptor)
                    self.notify(str(error), severity="error")
        await super().on_event(event)

    def _handle_exception(self, error):
        # Extension widgets are an application boundary; a failed component must
        # resolve its pending view and release the prompt rather than end every session.
        from textual.widget import Widget

        frame = error.__traceback__
        owners = []
        while frame:
            owner = frame.tb_frame.f_locals.get("self")
            if isinstance(owner, Widget):
                owners.extend([owner, *owner.ancestors])
            frame = frame.tb_next
        for pane in self.sessions.values():
            bridge = pane.bridge
            roots = [w for _, w in bridge.components.values()] + list(bridge.sections.values())
            if bridge.view:
                roots.append(bridge.view.widget)
            if any(root in owners for root in roots):
                bridge.clear_components()
                self.notify(f"Extension component failed: {error}", severity="error")
                return
        super()._handle_exception(error)

    async def action_quit(self):
        for screen in self.sessions.values():
            await screen.shutdown()
        self.exit()


async def run_tui_app(session, initial_prompt=None):
    app = RioTuiApp(session, initial_prompt=initial_prompt)
    try:
        await app.run_async()
    finally:
        for screen in app.sessions.values():
            if screen.session is not session:
                await screen.session.aclose()
