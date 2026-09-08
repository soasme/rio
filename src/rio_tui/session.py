"""One conversation screen wired to one coding session."""

import asyncio
import json
import re
from pathlib import Path
from time import monotonic

from textual import work
from textual.containers import Horizontal, Vertical, VerticalGroup
from textual.screen import Screen
from textual.widgets import Tabs

from rio_agent.events import (
    ActionEndEvent,
    ActionStartEvent,
    ReasoningDiscardedEvent,
    StateUpdateEvent,
    StepStartEvent,
    ValidationErrorEvent,
)
from rio_coding.events import AutoRetryStartEvent, QueueUpdateEvent, SessionRunEndEvent
from rio_coding.session_store import StepEntry, TurnEntry, latest_leaf_id, path_to_entry
from rio_tui.bridge import SessionBridge
from rio_tui.commands import ACTIONS, BUILTIN, help_message
from rio_tui.dialogs import Changes, FilePicker, Picker, Preferences
from rio_tui.widget_conversation import (
    Answer,
    Conversation,
    Notice,
    Thought,
    ToolBlock,
    UserMessage,
)
from rio_tui.widget_prompt import Editor, Prompt
from rio_tui.widget_sidebar import Plan, Sidebar
from rio_tui.widget_terminal import ShellTerminal

#: What a leading `/` has to look like before an unmatched name is reported as
#: a typo. A path (`/usr/bin/env ...`) or an escaped `//` line is just a message.
COMMAND_NAME = re.compile(r"^/([A-Za-z][A-Za-z0-9_:.-]*)(?:\s|$)")


class SessionScreen(Screen):
    def __init__(self, session, *, initial_prompt=None):
        super().__init__()
        self.session = session
        self.initial_prompt = initial_prompt
        self.bridge = SessionBridge(self)
        self.busy = False
        self.waiting = False
        self.started = None
        self.active_tool = None
        self.shell = None
        self.worker = None
        self.closed = False

    @property
    def prompt(self):
        return self.query_one(Prompt)

    @property
    def conversation(self):
        return self.query_one(Conversation)

    def compose(self):
        with Horizontal(id="layout"):
            yield Sidebar(getattr(self.session, "cwd", Path.cwd()))
            with Vertical(id="column"):
                yield Tabs(id="sessions")
                yield Conversation(id="conversation")
                yield Vertical(id="extension-view")
                yield VerticalGroup(id="above-prompt")
                yield Prompt(getattr(self.session, "cwd", Path.cwd()))
                yield VerticalGroup(id="below-prompt")

    async def on_mount(self):
        if hasattr(self.session, "extensions"):
            self.session.extensions.set_ui_bridge(self.bridge)
        self.apply_preferences()
        self.refresh_commands()
        await self.restore_display()
        self.query_one(Editor).focus()
        self.update_status()
        self.set_interval(1, self.update_status)
        if self.initial_prompt:
            self.submit(self.initial_prompt)

    def refresh_commands(self):
        """Offer loaded skills and prompt templates in the `/` popover.

        Templates go in first: a bare `/<name>` resolves to a template before a
        skill, so a shared name should describe the one that actually wins.
        """
        commands = {}
        for template in getattr(self.session, "prompt_templates", ()) or ():
            commands.setdefault(template.name, template.description or "Prompt template")
        for skill in getattr(self.session, "skills", ()) or ():
            commands.setdefault(skill.name, skill.description or "Skill")
        self.prompt.set_resource_commands(commands)

    def unknown_command(self, text):
        """Return the `/name` nothing claims, or None when something will.

        Only text shaped like a command counts: a leading path or an escaped
        `//` line is a message, and the backend still owns `/skill:<name>`.
        """
        match = COMMAND_NAME.match(text.strip())
        if match is None:
            return None
        name = match.group(1)
        if name.startswith("skill:"):
            return None
        loaded = {
            resource.name.lower()
            for attribute in ("prompt_templates", "skills")
            for resource in getattr(self.session, attribute, ()) or ()
        }
        return None if name.lower() in loaded else name

    def apply_preferences(self):
        self.query_one(Sidebar).display = self.app.settings.sidebar
        self.query_one("#column").set_class(self.app.settings.column, "column")
        for thought in self.query(Thought):
            thought.display = self.app.settings.thoughts

    async def restore_display(self):
        self.query_one(Editor).sent[False].clear()
        if hasattr(self.session, "session_entries"):
            entries = await self.session.session_entries()
            leaf = latest_leaf_id(entries)
            for entry in (path_to_entry(entries, leaf) if leaf else [])[-500:]:
                if isinstance(entry, TurnEntry):
                    await self.conversation.add(UserMessage(entry.observation))
                    self.query_one(Editor).sent[False].append(entry.observation)
                elif isinstance(entry, StepEntry):
                    if entry.action.name != "respond":
                        block = await self.conversation.add(
                            ToolBlock(entry.action.name, entry.action.arguments)
                        )
                        await block.complete(entry.observation or "")
                    if entry.terminated:
                        answer = entry.action.arguments.get("message")
                        if answer:
                            await self.conversation.add(Answer(str(answer)))
        self.query_one(Editor).history_index = len(self.query_one(Editor).sent[False])
        if not self.conversation.query_one("#blocks").children:
            await self.conversation.add(
                Notice("rio\nAsk a question or describe a task. / for commands · ! for shell")
            )

    def on_unmount(self):
        self.closed = True
        self.session.cancel()

    def update_status(self):
        if self.closed or not self.query(Prompt):
            return
        elapsed = int(monotonic() - self.started) if self.started else 0
        status = "Waiting for you" if self.waiting else f"Working · {elapsed}s" if self.busy else ""
        self.prompt.show_status(self.session, status)
        self.query_one(Plan).update_state(self.session.state)
        self.app.update_sessions()

    def on_editor_submit(self, event: Editor.Submit):
        event.stop()
        if event.shell:
            self.run_shell(event.text)
        else:
            self.submit(event.text)

    def submit(self, text):
        if text.startswith("/"):
            self.command(text)
        elif text.startswith("!"):
            self.run_shell(text[1:].strip())
        elif self.busy:
            self.session.queue_steering_message(text)
            self.run_worker(self.conversation.add(Notice("Queued: " + text)))
        else:
            self.busy = True
            self.started = monotonic()
            self.worker = self.run_turn(text)

    @work
    async def run_turn(self, text):
        await self.conversation.add(UserMessage(text))
        self.update_status()
        try:
            async for event in self.session.prompt(text):
                await self.consume(event)
        except Exception as error:
            await self.conversation.add(Notice(str(error), error=True))
        finally:
            self.busy = False
            self.started = None
            self.active_tool = None
            if not self.closed:
                self.update_status()
                if self.app.settings.notifications:
                    self.app.bell()

    async def consume(self, event):
        if isinstance(event, ActionStartEvent) and event.name != "respond":
            self.active_tool = await self.conversation.add(ToolBlock(event.name, event.arguments))
        elif isinstance(event, ActionEndEvent) and self.active_tool:
            diff = None
            arguments = self.active_tool.arguments
            if not event.is_error and event.name == "edit":
                edits = arguments.get("edits") or [arguments]
                if isinstance(edits, list) and all(isinstance(e, dict) for e in edits):
                    diff = (
                        "\n".join(str(e.get("oldText", "")) for e in edits),
                        "\n".join(str(e.get("newText", "")) for e in edits),
                    )
            await self.active_tool.complete(event.result.text, error=event.is_error, diff=diff)
            self.active_tool = None
        elif isinstance(event, ReasoningDiscardedEvent):
            block = Thought(event.reasoning)
            block.display = self.app.settings.thoughts
            await self.conversation.add(block)
        elif isinstance(event, SessionRunEndEvent) and event.answer:
            await self.conversation.add(Answer(event.answer))
        elif isinstance(event, (ValidationErrorEvent, AutoRetryStartEvent)):
            await self.conversation.add(
                Notice(getattr(event, "error", None) or event.error_message)
            )
        elif isinstance(event, QueueUpdateEvent):
            self.prompt.show_status(
                self.session, f"Queued · {len(event.steering) + len(event.follow_up)}"
            )
        if isinstance(event, (StateUpdateEvent, StepStartEvent)):
            self.update_status()

    def on_tabs_tab_activated(self, event):
        if event.tab.id:
            self.app.select_session(event.tab.id)

    def on_shell_terminal_finished(self, event):
        self.prompt.cwd = Path(event.terminal.cwd)
        from textual.widgets import Label

        self.prompt.query_one("#cwd-info", Label).update(str(self.prompt.cwd))

    def on_click(self, event):
        identifier = getattr(event.widget, "id", None)
        if identifier == "model-info":
            self.submit("/model")
        elif identifier == "mode-info":
            self.submit("/thinking")
        elif identifier == "cwd-info":
            self.action_files()

    def on_editor_find_file(self):
        self.action_files()

    def action_files(self):
        self.app.push_screen(FilePicker(self.prompt.cwd), self.insert_file)

    def insert_file(self, path):
        if path:
            self.prompt.insert_path(path)

    def on_directory_tree_file_selected(self, event):
        event.stop()
        self.insert_file(event.path.relative_to(self.prompt.cwd))

    def action_interrupt(self):
        if self.shell and not self.shell.finished:
            self.shell.action_interrupt()
        self.session.cancel()
        if self.worker and self.waiting:
            self.worker.cancel()

    @work
    async def run_shell(self, command):
        if not command:
            return
        if self.shell and not self.shell.finished:
            self.shell.send(command + "\r")
            return
        terminal = ShellTerminal(command, self.prompt.cwd)
        self.shell = terminal
        await self.conversation.add(terminal)
        terminal.focus()
        try:
            await terminal.run()
        except Exception as error:
            await self.conversation.add(Notice(str(error), error=True))
        finally:
            if not self.closed and self.app.screen is self:
                self.query_one(Editor).focus()

    def picker(self, title, choices):
        self.app.push_screen(
            Picker(title, choices), lambda value: self.submit(value) if value else None
        )

    @work
    async def command(self, text):
        name, _, value = text[1:].partition(" ")
        try:
            if name in {"quit", "exit"}:
                self.app.call_later(self.app.action_quit)
            elif name == "new":
                self.app.open_session()
            elif name in {"sessions", "resume"}:
                if value:
                    self.app.open_session(value)
                else:
                    manager = getattr(self.session, "session_manager", None)
                    records = manager.list_sessions(self.session.cwd) if manager else []
                    self.picker(
                        "Resume session",
                        [(f"{r.title or r.id} · {r.model}", f"/resume {r.id}") for r in records],
                    )
            elif name in {"sidebar", "next", "prev", "close"}:
                # Pure app actions; the palette reaches them through the same table.
                getattr(self.app, "action_" + ACTIONS[name])()
            elif name == "settings":
                self.app.push_screen(Preferences(self.app.settings), self.app.save_preferences)
            elif name == "files":
                self.action_files()
            elif name == "diff":
                self.app.push_screen(Changes(self.prompt.cwd))
            elif name == "shell":
                self.prompt.set_shell(True)
            elif name == "cancel":
                self.action_interrupt()
            elif name == "clear":
                await self.conversation.clear()
            elif name == "state":
                await self.conversation.add(
                    Answer("```json\n" + json.dumps(self.session.state, indent=2) + "\n```")
                )
            elif name in {"model", "provider", "thinking"}:
                if not value:
                    attribute = {
                        "model": "available_models",
                        "provider": "available_providers",
                        "thinking": "available_thinking_levels",
                    }[name]
                    self.picker(
                        name.capitalize(),
                        [(str(v), f"/{name} {v}") for v in getattr(self.session, attribute, ())],
                    )
                else:
                    if self.busy:
                        raise ValueError("Interrupt this session before changing its model")
                    setter = {
                        "model": "set_model",
                        "provider": "set_provider_name",
                        "thinking": "set_thinking_level",
                    }[name]
                    await getattr(self.session, setter)(value)
                    self.update_status()
            elif name in {"skills", "prompts"}:
                if name == "skills":
                    # A skill is `/<name>` unless a command or a template already
                    # answers to that name, where only `/skill:` still reaches it.
                    taken = set(BUILTIN) | {
                        t.name for t in getattr(self.session, "prompt_templates", ()) or ()
                    }
                    resources = [
                        (r, f"/skill:{r.name}" if r.name in taken else f"/{r.name}")
                        for r in getattr(self.session, "skills", ())
                    ]
                else:
                    resources = [
                        (r, f"/{r.name}") for r in getattr(self.session, "prompt_templates", ())
                    ]
                self.picker(
                    name.capitalize(),
                    [(r.name + " · " + (r.description or ""), command) for r, command in resources],
                )
            elif name == "name":
                await self.session.set_session_name(value)
                self.app.update_sessions()
            elif name == "reload":
                if self.busy:
                    raise ValueError("Interrupt this session before reloading resources")
                await self.session.reload()
                self.session.extensions.set_ui_bridge(self.bridge)
                self.refresh_commands()
            elif name in {"checkpoints", "restore"}:
                if self.busy:
                    raise ValueError("Interrupt this session before restoring a checkpoint")
                if value:
                    await self.session.restore(value)
                    await self.conversation.clear()
                    await self.restore_display()
                    self.update_status()
                else:
                    entries = await self.session.checkpoints()
                    self.picker("Restore checkpoint", [(e.id, f"/restore {e.id}") for e in entries])
            elif name == "login":
                if value:
                    self.worker = self.login(value)
                else:
                    self.picker(
                        "Sign in", [(p, f"/login {p}") for p in self.session.available_providers]
                    )
            elif name == "logout":
                from rio_coding.auth_commands import logout_provider

                await self.conversation.add(Notice(logout_provider(value)))
            elif name == "help":
                await self.conversation.add(Answer(help_message()))
            else:
                registry = getattr(self.session, "command_registry", None)
                result = registry.execute(self.session, text) if registry else None
                if result and result.handled:
                    if result.message:
                        await self.conversation.add(Notice(str(result.message)))
                    routes = {
                        "resume_picker_requested": "/sessions",
                        "prompts_picker_requested": "/prompts",
                        "tree_picker_requested": "/checkpoints",
                        "login_picker_requested": "/login",
                        "model_picker_requested": "/model",
                        "skills_picker_requested": "/skills",
                        "theme_picker_requested": "/settings",
                        "local_requested": "/provider",
                    }
                    for flag, command in routes.items():
                        if getattr(result, flag, False):
                            self.submit(command)
                    if result.exit_requested:
                        self.app.call_later(self.app.action_quit)
                    if result.new_session_requested:
                        self.app.open_session()
                    if result.resume_session_id:
                        self.app.open_session(result.resume_session_id)
                    if result.clear_requested:
                        await self.conversation.clear()
                    if result.reload_requested:
                        self.submit("/reload")
                    if result.session_name:
                        self.submit("/name " + result.session_name)
                    if result.login_provider:
                        self.submit("/login " + result.login_provider)
                    if result.logout_provider:
                        self.submit("/logout " + result.logout_provider)
                    if result.thinking_level:
                        self.submit("/thinking " + result.thinking_level)
                    if result.theme:
                        from dataclasses import replace

                        if result.theme not in self.app.available_themes:
                            raise ValueError(f"Unknown theme: {result.theme}")
                        self.app.save_preferences(replace(self.app.settings, theme=result.theme))
                    if result.export_requested:
                        from rio_coding.session_export import export_session_artifact

                        path = (
                            result.export_destination or Path(self.session.cwd) / "rio-session.html"
                        )
                        artifact = export_session_artifact(
                            await self.session.session_entries(),
                            path,
                            format=result.export_format,
                            instructions=self.session.system_prompt,
                            tools=self.session.tools,
                            model=self.session.model,
                            provider=self.session.provider_name,
                        )
                        await self.conversation.add(Notice(f"Exported {artifact}"))
                elif (unknown := self.unknown_command(text)) is not None:
                    raise ValueError(f"Unknown command: /{unknown}")
                else:
                    # Skill invocations and prompt templates are interpreted by the backend.
                    if self.busy:
                        self.session.queue_steering_message(text)
                    else:
                        self.busy = True
                        self.started = monotonic()
                        self.worker = self.run_turn(text)
        except Exception as error:
            await self.conversation.add(Notice(str(error), error=True))

    @work
    async def login(self, provider):
        from rio_coding.auth_commands import login_provider
        from rio_coding.oauth_registry import get_oauth_provider
        from rio_coding.oauth_types import OAuthLoginCallbacks

        try:
            if get_oauth_provider(provider):

                def auth(info):
                    self.run_worker(
                        self.conversation.add(
                            Answer(f"[Open sign-in page]({info.url})\n\n{info.instructions or ''}")
                        )
                    )

                async def prompt(info):
                    result = await self.bridge.input(info.message, info.placeholder or "")
                    if result is None:
                        raise asyncio.CancelledError
                    return result

                async def manual():
                    result = await self.bridge.input(
                        "Paste the redirect URL, or finish in your browser"
                    )
                    if result is None:
                        raise asyncio.CancelledError
                    return result

                async def select(info):
                    labels = [o.label for o in info.options]
                    chosen = await self.bridge.select(info.message, labels)
                    return next((o.id for o in info.options if o.label == chosen), None)

                def device(info):
                    self.run_worker(
                        self.conversation.add(
                            Answer(f"[Sign in]({info.verification_uri}) · Code: `{info.user_code}`")
                        )
                    )

                callbacks = OAuthLoginCallbacks(
                    on_auth=auth,
                    on_device_code=device,
                    on_prompt=prompt,
                    on_select=select,
                    on_manual_code_input=manual,
                )
                result = await login_provider(provider, callbacks=callbacks)
            else:
                key = await self.bridge.ask(f"API key for {provider}", password=True)
                if key is None:
                    return
                result = await login_provider(provider, api_key=key)
            await self.session.set_provider_name(provider)
            await self.conversation.add(Notice(result))
            self.update_status()
        except asyncio.CancelledError:
            if not self.closed and self.query(Conversation):
                await self.conversation.add(Notice("Sign-in cancelled"))
        except Exception as error:
            await self.conversation.add(Notice(str(error), error=True))

    async def shutdown(self):
        self.closed = True
        self.session.cancel()
        workers = self.workers.cancel_node(self)
        await asyncio.gather(*(w.wait() for w in workers), return_exceptions=True)
        self.bridge.clear_components()
