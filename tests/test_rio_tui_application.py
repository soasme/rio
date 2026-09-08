"""Behavioral tests for the conversation UI and its coding-session boundary."""

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.widgets import OptionList, Tabs
from textual_diff_view import DiffView

import rio_tui
from rio_agent.events import ActionEndEvent, ActionStartEvent, StateUpdateEvent
from rio_ai.tools import AgentToolResult
from rio_coding.events import SessionRunEndEvent
from rio_coding.paths import RioPaths
from rio_tui import RioTuiApp, Settings
from rio_tui.bridge import Question
from rio_tui.commands import COMMANDS, help_message, palette_choices
from rio_tui.dialogs import FilePicker, Picker
from rio_tui.widget_conversation import Answer, Conversation, Notice, ToolBlock, UserMessage
from rio_tui.widget_prompt import Editor
from rio_tui.widget_sidebar import ProjectTree, Sidebar
from rio_tui.widget_terminal import ShellTerminal

EMOJI_RANGES = ((0x1F000, 0x1FAFF), (0x2600, 0x26FF), (0xFE0F, 0xFE0F))


def emoji(text):
    """Emoji in text. Typographic marks the widgets do use (`·`, `❯`, `✓`, `›`) are not emoji."""
    return [c for c in text if any(low <= ord(c) <= high for low, high in EMOJI_RANGES)]


def resource(name, description):
    """A stand-in for a loaded skill or prompt template."""
    return SimpleNamespace(name=name, description=description)


class Session:
    model = "test"
    state = {}
    session_title = None
    thinking_level = "off"
    available_models = ("test", "other")
    skills = ()
    prompt_templates = ()

    def __init__(self, cwd, session_id="first"):
        self.cwd = Path(cwd)
        self.config = SimpleNamespace(paths=RioPaths(home=self.cwd / "home"))
        self.session_id = session_id
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False
        self.prompts = []
        self.steering = []

    async def open_session(self, session_id=None):
        return Session(self.cwd, session_id or "second")

    async def prompt(self, text):
        self.prompts.append(text)
        self.started.set()
        await self.release.wait()
        self.state = {"goal": text}
        yield StateUpdateEvent(step=1, delta=self.state, state=self.state)
        yield SessionRunEndEvent(steps=1, state=self.state, answer=text)

    def queue_steering_message(self, text):
        self.steering.append(text)

    def cancel(self):
        self.release.set()

    async def aclose(self):
        self.closed = True

    async def set_model(self, model):
        self.model = model


def make_app(tmp_path):
    return RioTuiApp(Session(tmp_path), settings=Settings())


async def loaded(app, node):
    await asyncio.gather(*(w.wait() for w in app.workers if w.node is node))


@pytest.mark.asyncio
async def test_layout_is_conversation_first(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        screen = app.workspace
        assert not screen.query_one(Sidebar).display
        assert not screen.query_one(Tabs).display
        assert screen.query_one(Editor).has_focus
        assert screen.query_one("#column").outer_size.width == 100
        assert screen.query_one("#info-bar").region.y > screen.query_one(Editor).region.y
        assert screen.query_one(Conversation).region.bottom <= screen.query_one("#prompt").region.y
        assert not screen.query("RichLog")


@pytest.mark.asyncio
async def test_multiline_and_history(tmp_path):
    app = make_app(tmp_path)
    app.initial_session.release.set()
    async with app.run_test() as pilot:
        await pilot.press("a", "shift+enter", "b", "enter")
        await pilot.pause()
        assert app.initial_session.prompts == ["a\nb"]
        assert app.workspace.query_one(UserMessage).text == "a\nb"
        assert app.workspace.query_one(Answer).text == "a\nb"
        await pilot.press("up")
        assert app.workspace.query_one(Editor).text == "a\nb"
        app.workspace.query_one(Editor).move_cursor(app.workspace.query_one(Editor).document.end)
        await pilot.press("down")
        assert app.workspace.query_one(Editor).text == ""


@pytest.mark.asyncio
async def test_slash_completion_and_model_picker(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("/", "m", "o", "tab")
        assert app.workspace.query_one(Editor).text == "/model "
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, Picker)
        await pilot.press("o", "t", "h", "enter")
        await pilot.pause()
        assert app.workspace.session.model == "other"


@pytest.mark.asyncio
async def test_tools_expand_individually_and_show_real_diff_widget(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        pane = app.workspace
        await pane.consume(
            ActionStartEvent(
                step=1,
                name="edit",
                arguments={
                    "path": "sample.py",
                    "edits": [{"oldText": "a = 1", "newText": "a = 2"}],
                },
            )
        )
        await pane.consume(
            ActionEndEvent(
                step=1, name="edit", result=AgentToolResult(content="Updated"), is_error=False
            )
        )
        await pane.consume(ActionStartEvent(step=2, name="read", arguments={"path": "other.py"}))
        await pane.consume(
            ActionEndEvent(
                step=2, name="read", result=AgentToolResult(content="hello"), is_error=False
            )
        )
        await pilot.pause()
        first, second = pane.query(ToolBlock)
        assert first.collapsed and second.collapsed
        first.collapsed = False
        await pilot.pause()
        assert first.query(DiffView)
        assert second.collapsed
        assert first.title.endswith("done")


@pytest.mark.asyncio
async def test_tool_blocks_name_their_target_whatever_the_model_spells(tmp_path):
    calls = [
        ("read", {"filePath": str(tmp_path / "a.py")}),
        ("read", {"files": [str(tmp_path / "b.py"), str(tmp_path / "c.py")]}),
        ("bash", {"cmd": "ls -l"}),
        ("lookup", {"query": "todo"}),
        (
            "edit",
            {
                "filePath": str(tmp_path / "d.py"),
                "edits": [{"oldText": "a = 1", "newText": "a = 2"}],
            },
        ),
    ]
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        pane = app.workspace
        for step, (name, arguments) in enumerate(calls, start=1):
            await pane.consume(ActionStartEvent(step=step, name=name, arguments=arguments))
            await pane.consume(
                ActionEndEvent(
                    step=step, name=name, result=AgentToolResult(content="ok"), is_error=False
                )
            )
        await pilot.pause()
        blocks = list(pane.query(ToolBlock))
        assert [block.label for block in blocks] == [
            "read a.py",
            "read b.py, c.py",
            "bash ls -l",
            "lookup todo",
            "edit d.py",
        ]
        assert blocks[-1].query_one(DiffView).path_original == "d.py"


@pytest.mark.asyncio
async def test_concurrent_sessions_keep_drafts_and_events_separate(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        first = app.workspace
        first.submit("first answer")
        await first.session.started.wait()
        first.query_one(Editor).text = "draft"
        await app.open_session().wait()
        second = app.workspace
        assert second is not first
        second.submit("second answer")
        await second.session.started.wait()
        first.session.release.set()
        await pilot.pause()
        assert not first.busy and second.busy
        assert first.query_one(Answer).text == "first answer"
        assert not second.query(Answer)
        second.session.release.set()
        await pilot.pause()
        app.action_previous_session()
        await pilot.pause()
        assert app.workspace is first
        assert first.query_one(Editor).text == "draft"
        app.action_next_session()
        await pilot.pause()
        await app.action_close_session().wait()
        assert len(app.sessions) == 1
        assert second.session.closed


@pytest.mark.asyncio
async def test_resume_existing_session_selects_tab(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await app.open_session("saved").wait()
        saved = app.workspace
        app.action_previous_session()
        await pilot.pause()
        await app.open_session("saved").wait()
        assert app.workspace is saved
        assert len(app.sessions) == 2


@pytest.mark.asyncio
async def test_steering_does_not_start_another_run(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        pane = app.workspace
        pane.submit("start")
        await pane.session.started.wait()
        pane.submit("also test")
        await pilot.pause()
        assert pane.session.steering == ["also test"]
        assert pane.session.prompts == ["start"]
        pane.session.release.set()
        await pilot.pause()


@pytest.mark.asyncio
async def test_file_search_inserts_quoted_path(tmp_path):
    (tmp_path / "file name.py").write_text("print('hello')")
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        app.action_files()
        await pilot.pause()
        assert isinstance(app.screen, FilePicker)
        await pilot.press("f", "n", "p", "enter")
        await pilot.pause()
        assert app.workspace.query_one(Editor).text == "@'file name.py' "


@pytest.mark.asyncio
async def test_shell_mode_is_inline_and_output_stays(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("!")
        assert app.workspace.prompt.shell
        editor = app.workspace.query_one(Editor)
        editor.text = "read value; printf 'reply:%s' \"$value\""
        await pilot.press("enter")
        await pilot.pause()
        terminal = app.workspace.query_one(ShellTerminal)
        assert terminal.ancestors.count(app.workspace.conversation) == 1
        await pilot.press("o", "k", "enter")
        await pilot.pause()
        assert terminal.finished
        assert "reply:ok" in terminal.output
        assert app.workspace.query_one(Editor).has_focus
        await pilot.press("escape")
        assert not app.workspace.prompt.shell
        assert terminal.is_mounted


@pytest.mark.asyncio
async def test_extension_question_replaces_prompt_and_restores_focus(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        task = asyncio.create_task(app.workspace.bridge.confirm("Run tool?", "Modify file"))
        await pilot.pause()
        assert app.workspace.query(Question)
        assert not app.workspace.query_one("#prompt-box").display
        await pilot.press("down", "enter")
        assert await task is True
        await pilot.pause()
        assert not app.workspace.query(Question)
        assert app.workspace.query_one(Editor).has_focus
        assert await app.workspace.bridge.input("Timeout", timeout=0.01) is None


@pytest.mark.asyncio
async def test_settings_and_sidebar_persist(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        app.action_sidebar()
        await pilot.pause()
        assert app.workspace.query_one(Sidebar).display
        app.save_preferences(
            replace(app.settings, column=False, theme="textual-light", diff_layout="split")
        )
        assert app.theme == "textual-light"
        assert not app.workspace.query_one("#column").has_class("column")
        assert Settings.load(app.settings_path) == app.settings


@pytest.mark.asyncio
async def test_resume_uses_active_branch_only(tmp_path):
    from rio_coding.session_store import ActionRecord, LeafEntry, StepEntry, TurnEntry

    app = make_app(tmp_path)
    turn = TurnEntry(observation="initial task")
    abandoned = StepEntry(
        parent_id=turn.id,
        step=1,
        action=ActionRecord(name="respond", arguments={"message": "abandoned"}),
        terminated=True,
    )
    active = StepEntry(
        parent_id=turn.id,
        step=1,
        action=ActionRecord(name="respond", arguments={"message": "restored"}),
        terminated=True,
    )

    async def entries():
        return [turn, abandoned, active, LeafEntry(entry_id=active.id)]

    app.initial_session.session_entries = entries
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.workspace.query_one(UserMessage).text == "initial task"
        assert [answer.text for answer in app.workspace.query(Answer)] == ["restored"]


@pytest.mark.parametrize("raw", ["[]", '{"sidebar": "wrong"}', "bad json"])
def test_invalid_preferences_fall_back(tmp_path, raw):
    path = tmp_path / "tui.json"
    path.write_text(raw)
    assert Settings.load(path) == Settings()


def test_headless_bridge_does_not_load_ui():
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from rio_coding.extensions.api import NullUiBridge; "
            "import sys; assert NullUiBridge().theme is None; assert 'rio_tui' not in sys.modules",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_shell_changes_its_working_directory(tmp_path):
    (tmp_path / "nested").mkdir()
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await app.workspace.run_shell("cd nested").wait()
        await pilot.pause()
        assert app.workspace.prompt.cwd == tmp_path / "nested"
        await app.workspace.run_shell("pwd").wait()
        terminal = list(app.workspace.query(ShellTerminal))[-1]
        assert terminal.buffer.columns == terminal.content_size.width
        assert str(tmp_path / "nested") in "".join(
            line.strip() for line in terminal.output.splitlines()
        )


@pytest.mark.asyncio
async def test_close_tab_terminates_shell(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await app.open_session().wait()
        app.workspace.run_shell("sleep 30")
        await pilot.pause()
        terminal = app.workspace.query_one(ShellTerminal)
        await app.action_close_session().wait()
        assert terminal.process.returncode is not None


@pytest.mark.asyncio
async def test_extension_components_and_view_cleanup(tmp_path):
    from textual.widgets import Static

    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        bridge = app.workspace.bridge
        bridge.set_slot_widget("status", ["extension status"])
        bridge.set_sidebar_section("test", "info", title="Extension", content=["details"])
        view = bridge.open_main_view(lambda handle, theme: Static("custom view"))
        await pilot.pause()
        assert not app.workspace.conversation.display
        assert app.workspace.query_one("#above-prompt").children
        view.close("done")
        assert await view.wait() == "done"
        bridge.clear_components()
        await pilot.pause()
        assert app.workspace.conversation.display
        assert not app.workspace.query_one("#above-prompt").children


@pytest.mark.asyncio
async def test_extension_crash_does_not_end_application(tmp_path):
    from textual.widgets import Static

    class Broken(Static):
        def on_mount(self):
            raise RuntimeError("broken component")

    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        handle = app.workspace.bridge.open_main_view(lambda handle, theme: Broken())
        await pilot.pause()
        assert await handle.wait() is None
        assert app.workspace.conversation.display


@pytest.mark.asyncio
async def test_slash_completion_offers_skills_and_templates(tmp_path):
    app = make_app(tmp_path)
    app.initial_session.skills = (resource("audit", "Audit the dependency tree"),)
    app.initial_session.prompt_templates = (resource("zoom", "Zoom in on a file"),)
    async with app.run_test() as pilot:
        await pilot.press("/", "a", "u", "tab")
        assert app.workspace.query_one(Editor).text == "/audit "
        app.workspace.query_one(Editor).clear()
        await pilot.press("/", "z")
        assert app.workspace.prompt.choices == ["/zoom"]


@pytest.mark.asyncio
async def test_a_bare_skill_name_is_sent_for_expansion(tmp_path):
    app = make_app(tmp_path)
    app.initial_session.skills = (resource("audit", "Audit the dependency tree"),)
    app.initial_session.release.set()
    async with app.run_test() as pilot:
        app.workspace.submit("/audit the lockfile")
        await pilot.pause()
        await loaded(app, app.workspace)
    assert app.initial_session.prompts == ["/audit the lockfile"]


@pytest.mark.asyncio
async def test_unknown_slash_command_is_reported_not_sent(tmp_path):
    app = make_app(tmp_path)
    app.initial_session.release.set()
    async with app.run_test() as pilot:
        app.workspace.submit("/nope go")
        await pilot.pause()
        await loaded(app, app.workspace)
        notices = [notice.text for notice in app.workspace.query(Notice)]
    assert any("Unknown command: /nope" in text for text in notices)
    assert app.initial_session.prompts == []


@pytest.mark.asyncio
async def test_a_leading_path_is_still_a_message(tmp_path):
    app = make_app(tmp_path)
    app.initial_session.release.set()
    async with app.run_test() as pilot:
        app.workspace.submit("/usr/bin/env python is missing")
        await pilot.pause()
        await loaded(app, app.workspace)
    assert app.initial_session.prompts == ["/usr/bin/env python is missing"]


@pytest.mark.asyncio
async def test_skills_picker_prefers_bare_names(tmp_path):
    app = make_app(tmp_path)
    app.initial_session.skills = (
        resource("audit", "Audit the dependency tree"),
        resource("model", "Pick a model"),
        resource("zoom", "Zoom in on a file"),
    )
    app.initial_session.prompt_templates = (resource("zoom", "Zoom template"),)
    async with app.run_test() as pilot:
        app.workspace.submit("/skills")
        await pilot.pause()
        assert isinstance(app.screen, Picker)
        assert [command for _label, command in app.screen.choices] == [
            "/audit",
            "/skill:model",
            "/skill:zoom",
        ]


@pytest.mark.asyncio
async def test_quit_slash_command_cancels_active_work(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        app.workspace.submit("start")
        await app.initial_session.started.wait()
        app.workspace.submit("/quit")
        await pilot.pause()
    assert app.initial_session.release.is_set()


@pytest.mark.asyncio
async def test_diff_review_staged_and_working_tree(tmp_path):
    import subprocess

    from rio_tui.dialogs import Changes

    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init")
    (tmp_path / "sample.py").write_text("a = 1\n")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "initial")
    (tmp_path / "sample.py").write_text("a = 2\n")
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        review = Changes(tmp_path)
        app.push_screen(review)
        await pilot.pause()
        await loaded(app, review)
        assert review.query(DiffView)
        git("add", ".")
        review.action_staged()
        await loaded(app, review)
        assert review.staged
        assert review.query(DiffView)


@pytest.mark.asyncio
async def test_preferences_save_is_accessible_in_small_terminal(tmp_path):
    from textual.widgets import Checkbox

    from rio_tui.dialogs import Preferences

    app = make_app(tmp_path)
    async with app.run_test(size=(70, 22)) as pilot:
        app.action_settings()
        await pilot.pause()
        assert isinstance(app.screen, Preferences)
        app.screen.query_one("#column", Checkbox).value = False
        assert await pilot.click("#save")
        await pilot.pause()
        assert app.screen is app.workspace
        assert not app.settings.column
        assert Settings.load(app.settings_path).column is False


@pytest.mark.asyncio
async def test_message_menu_can_edit_previous_prompt(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test(size=(90, 30)) as pilot:
        block = await app.workspace.conversation.add(UserMessage("Rewrite this paragraph"))
        await pilot.pause()
        await pilot.click(block, button=3, offset=(3, 1))
        await pilot.pause()
        assert isinstance(app.screen, Picker)
        await pilot.press("e", "d", "i", "t", "enter")
        await pilot.pause()
        assert app.workspace.query_one(Editor).text == "Rewrite this paragraph"


def test_hotkeys_and_palette_come_from_one_table():
    keys = {key for key, _action, _description in RioTuiApp.BINDINGS}
    actions = {action for _key, action, _description in RioTuiApp.BINDINGS}
    assert keys == {command.key for command in COMMANDS if command.key} | {"ctrl+k"}
    assert actions - {"commands"} == {command.action for command in COMMANDS if command.key}
    labels = {command: label for label, command in palette_choices()}
    assert set(labels) == {"/" + command.name for command in COMMANDS}
    for command in COMMANDS:
        assert labels["/" + command.name].endswith(command.hotkey or command.description)
    message = help_message()
    assert "Ctrl+K" in message
    assert all(command.hotkey in message for command in COMMANDS if command.key)


@pytest.mark.asyncio
async def test_palette_runs_a_command_that_only_had_a_hotkey(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+k")
        await pilot.pause()
        assert isinstance(app.screen, Picker)
        await pilot.press(*"sidebar", "enter")
        await pilot.pause()
        await loaded(app, app.workspace)
        await pilot.pause()
        assert app.workspace.query_one(Sidebar).display


@pytest.mark.asyncio
async def test_session_commands_mirror_their_hotkeys(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        first = app.workspace
        await app.open_session().wait()
        second = app.workspace
        second.submit("/prev")
        await loaded(app, second)
        await pilot.pause()
        assert app.workspace is first
        first.submit("/next")
        await loaded(app, first)
        await pilot.pause()
        assert app.workspace is second
        second.submit("/close")
        await loaded(app, second)
        await pilot.pause()
        assert len(app.sessions) == 1
        assert app.workspace is first
        await app.open_session().wait()
        await pilot.press("ctrl+w")
        await pilot.pause()
        await pilot.pause()
        assert len(app.sessions) == 1


@pytest.mark.asyncio
async def test_completion_popover_shows_hotkeys(tmp_path):
    app = make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("/", "n", "e")
        await pilot.pause()
        options = app.workspace.query_one("#completion", OptionList)
        labels = [
            str(options.get_option_at_index(index).prompt) for index in range(options.option_count)
        ]
        assert [label.split()[0] for label in labels] == ["/new", "/next"]
        assert [label.split()[-1] for label in labels] == ["Ctrl+N", "Ctrl+]"]


def test_widgets_carry_no_emoji():
    # Both sources are inherited, so scanning the source text alone would miss them.
    assert RioTuiApp.ENABLE_COMMAND_PALETTE is False
    icons = (ProjectTree.ICON_NODE, ProjectTree.ICON_NODE_EXPANDED, ProjectTree.ICON_FILE)
    assert not [char for icon in icons for char in emoji(icon)]
    assert len({len(icon) for icon in icons}) == 1
    for path in sorted(Path(rio_tui.__file__).parent.rglob("*")):
        if path.is_file():
            assert not emoji(path.read_text(encoding="utf-8", errors="ignore")), path
