from types import SimpleNamespace

import pytest
from textual.widgets import Input

from rio_agent.events import StateUpdateEvent, ValidationErrorEvent
from rio_coding.events import SessionRunEndEvent
from rio_coding.tui import RioTuiApp, TuiEventAdapter, TuiSettings
from rio_coding.tui.widgets import StateSidebar


def test_adapter_keeps_committed_state_on_validation_failure():
    adapter = TuiEventAdapter()
    state = {"plan": ["read file"]}
    adapter.consume(StateUpdateEvent(step=1, delta=state, state=state))
    state["plan"].append("mutated outside UI")
    items = adapter.consume(ValidationErrorEvent(step=2, attempt=1, error="bad patch"))
    assert adapter.state.state == {"plan": ["read file"]}
    assert items[0].role == "validation_retry"


class FakeSession:
    state = {"goal": ""}
    model = "fake"
    step_footprint = SimpleNamespace(total_tokens=200)
    queued_message_count = 0
    prompt_templates = ()
    available_models = ("fake", "other")
    available_thinking_levels = ("off", "high")

    async def set_model(self, model):
        self.model = model

    async def set_thinking_level(self, level):
        self.thinking_level = level

    def __init__(self):
        self.prompts = []
        self.cancelled = False

    async def prompt(self, text):
        self.prompts.append(text)
        self.state = {"goal": text, "answer": "Done"}
        yield StateUpdateEvent(step=1, delta=self.state, state=self.state)
        yield SessionRunEndEvent(steps=1, state=self.state, answer="Done")

    def cancel(self):
        self.cancelled = True


@pytest.mark.asyncio
async def test_interactive_prompt_and_state_sidebar():
    session = FakeSession()
    app = RioTuiApp(session, settings=TuiSettings())
    async with app.run_test() as pilot:
        await pilot.click("#prompt")
        await pilot.press("h", "i", "enter")
        await pilot.pause()
        assert session.prompts == ["hi"]
        assert app.adapter.state.state["answer"] == "Done"
        assert not app._busy
        assert app.query_one(Input).value == ""
        assert "200" in str(app.query_one(StateSidebar).render())
        await pilot.press("escape")
        assert session.cancelled


@pytest.mark.asyncio
async def test_initial_prompt_and_help_are_local():
    session = FakeSession()
    app = RioTuiApp(session, initial_prompt="start", settings=TuiSettings())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.submit_prompt("/help")
        app.submit_prompt("/state")
        assert session.prompts == ["start"]


@pytest.mark.asyncio
async def test_model_picker_selects_and_switches():
    session = FakeSession()
    app = RioTuiApp(session, settings=TuiSettings())
    async with app.run_test() as pilot:
        app.submit_prompt("/model")
        await pilot.pause()
        await pilot.press("down", "enter")
        await pilot.pause()
        assert session.model == "other"
        assert not app._busy
        assert not session.prompts


@pytest.mark.asyncio
async def test_thinking_picker_and_dismissal():
    session = FakeSession()
    app = RioTuiApp(session, settings=TuiSettings())
    async with app.run_test() as pilot:
        app.submit_prompt("/thinking")
        await pilot.pause()
        await pilot.press("down", "enter")
        await pilot.pause()
        assert session.thinking_level == "high"
        app.submit_prompt("/model")
        await pilot.pause()
        await pilot.press("escape")
        assert session.model == "fake"


@pytest.mark.asyncio
async def test_extension_dialogs_select_confirm_input_and_timeout():
    import asyncio

    from rio_coding.tui.bridge import TuiBridge

    app = RioTuiApp(FakeSession(), settings=TuiSettings())
    async with app.run_test() as pilot:
        bridge = TuiBridge(app)
        choice = asyncio.create_task(bridge.select("Choose", ["one", "two"]))
        await pilot.pause()
        await pilot.press("down", "enter")
        assert await choice == "two"
        await pilot.pause()
        confirmation = asyncio.create_task(bridge.confirm("Proceed?", "Make change"))
        await pilot.pause()
        await pilot.press("enter")
        assert await confirmation is False
        await pilot.pause()
        prompt = asyncio.create_task(bridge.input("Name"))
        await pilot.pause()
        await pilot.press("h", "i", "enter")
        assert await prompt == "hi"
        await pilot.pause()
        assert await bridge.select("Timeout", ["one"], timeout=0.01) is None
        await pilot.pause()
        bridge.notify("An extension notification")
        await pilot.pause()


@pytest.mark.asyncio
async def test_skill_picker_uses_skill_command():
    session = FakeSession()
    session.skills = (SimpleNamespace(name="review", description="Review code"),)
    app = RioTuiApp(session, settings=TuiSettings())
    async with app.run_test() as pilot:
        app.submit_prompt("/skills")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert session.prompts == ["/skill:review"]


@pytest.mark.asyncio
async def test_registered_extension_command_is_dispatched():
    from rio_coding.commands import CommandRegistry, CommandResult, SlashCommand

    called = []
    registry = CommandRegistry()
    registry.register(
        SlashCommand(
            name="custom",
            description="Custom",
            usage="/custom",
            handler=lambda context: (
                called.append(context.args) or CommandResult(handled=True, message="ok")
            ),
        )
    )
    session = FakeSession()
    session.command_registry = registry
    app = RioTuiApp(session, settings=TuiSettings())
    async with app.run_test():
        app.submit_prompt("/custom value")
        assert called == ["value"]
        assert session.prompts == []


@pytest.mark.asyncio
async def test_extension_widgets_sidebar_main_view_and_key_interception():
    from textual.widgets import Static

    app = RioTuiApp(FakeSession(), settings=TuiSettings())
    async with app.run_test() as pilot:
        bridge = app.ui_bridge
        bridge.set_slot_widget("status", ["[bold]Extension status[/bold]"])
        bridge.set_sidebar_section("test", "details", title="Details", content=["Visible"])
        await pilot.pause()
        assert len(app.query_one("#above-prompt").children) == 1
        assert len(app.query_one("#extension-sidebar").children) == 1
        handle = bridge.open_main_view(
            lambda handle, theme: Static("Custom main", id="custom-main")
        )
        await pilot.pause()
        assert handle.is_open
        assert not app.query_one("#stream").display
        assert app.query_one("#custom-main").is_mounted
        unsubscribe = bridge.register_key_interceptor(lambda event, text: event.key == "x")
        await pilot.click("#prompt")
        await pilot.press("x", "y")
        assert app.query_one(Input).value == "y"
        unsubscribe()
        handle.close("accepted")
        handle.close("ignored")
        assert await handle.wait() == "accepted"
        await pilot.pause()
        assert app.query_one("#stream").display
        pending = bridge.open_main_view(lambda handle, theme: Static("Pending"))
        bridge.clear_components()
        assert await pending.wait() is None
        await pilot.pause()
        assert not app.query_one("#above-prompt").children
        assert not app.query_one("#extension-sidebar").children


@pytest.mark.asyncio
async def test_extension_widget_crash_closes_pending_main_view():
    from textual.widgets import Static

    class BrokenWidget(Static):
        def on_mount(self):
            raise RuntimeError("broken extension")

    app = RioTuiApp(FakeSession(), settings=TuiSettings())
    async with app.run_test() as pilot:
        handle = app.ui_bridge.open_main_view(lambda handle, theme: BrokenWidget())
        await pilot.pause()
        assert await handle.wait() is None
        assert not handle.is_open
        assert app.query_one("#stream").display


def test_adapter_turns_and_tool_results():
    from rio_agent.events import ActionEndEvent, ActionStartEvent, StepStartEvent
    from rio_ai.tools import AgentToolResult

    adapter = TuiEventAdapter()
    assert adapter.consume(StepStartEvent(step=2, state={}, observation="")) == []
    assert adapter.state.step == 2
    items = adapter.consume(
        ActionStartEvent(step=2, name="bash", arguments={"command": "git status"})
    )
    assert items[0].text == "Running git status"
    assert adapter.state.active_action == "git status"
    items = adapter.consume(
        ActionEndEvent(
            step=2,
            name="bash",
            result=AgentToolResult(content="failed"),
            is_error=True,
        )
    )
    assert items[0].continuation
    assert items[0].text == "bash: failed"
    assert items[0].role == "error"
    assert adapter.state.active_action is None


@pytest.mark.asyncio
async def test_transcript_wraps_and_reflows_and_working_row():
    from textual.widgets import RichLog, Static

    from rio_coding.tui.state import StepStreamItem
    from rio_coding.tui.widgets import StepStream

    app = RioTuiApp(FakeSession(), settings=TuiSettings(sidebar_position="off"))
    async with app.run_test(size=(100, 30)) as pilot:
        stream = app.query_one(StepStream)
        app.write_item(StepStreamItem("custom", "[literal] " + "x" * 160))
        app.write_item(StepStreamItem("step", "output", continuation=True))
        await pilot.pause()
        before = len(stream.lines)
        assert stream.lines[0].text.startswith("• [literal]")
        assert any(line.text.startswith("  └ output") for line in stream.lines)
        await pilot.resize_terminal(45, 30)
        await pilot.pause()
        assert len(stream.lines) > before
        log = stream.query_one(RichLog)
        assert log.max_scroll_x == 0
        stream.show_working(132, "escape", "gh run watch 123")
        await pilot.pause()
        status = stream.query_one(Static)
        assert (
            str(status.render()) == "• Working (2m 12s • escape to interrupt)\n  └ gh run watch 123"
        )
        assert len(stream.entries) == 2
        stream.show_working(None, "escape", None)
        assert not status.display


@pytest.mark.asyncio
async def test_working_timer_and_cleanup():
    import asyncio
    from time import monotonic

    from textual.widgets import Static

    from rio_coding.tui.widgets import StepStream

    class SlowSession(FakeSession):
        async def prompt(self, text):
            await gate.wait()
            raise RuntimeError("tool failed")
            yield

    gate = asyncio.Event()
    app = RioTuiApp(SlowSession(), settings=TuiSettings())
    async with app.run_test() as pilot:
        app.submit_prompt("run")
        await pilot.pause()
        assert app._working_since is not None
        app._working_since = monotonic() - 141
        app.refresh_working()
        status = app.query_one(StepStream).query_one(Static)
        assert app.working_elapsed == 141
        assert "2m 21s" in str(status.render())
        await pilot.press("escape")
        assert app.session.cancelled
        gate.set()
        await pilot.pause()
        assert app._working_since is None
        assert status.display
        assert str(status.render()).startswith("- Worked for 2m 21s --")
        assert not app.adapter.state.running


@pytest.mark.parametrize(
    ("elapsed", "expected"),
    [
        (0, "0s"),
        (59, "59s"),
        (60, "1m 0s"),
        (132, "2m 12s"),
        (4503, "1h 15m 3s"),
        (90061, "1d 1h 1m 1s"),
    ],
)
def test_human_elapsed(elapsed, expected):
    from rio_coding.tui.formatting import human_elapsed

    assert human_elapsed(elapsed) == expected


@pytest.mark.asyncio
async def test_wide_transcript_and_working_indentation():
    from textual.widgets import RichLog, Static

    from rio_coding.tui.widgets import StepStream

    app = RioTuiApp(FakeSession(), settings=TuiSettings(sidebar_position="off"))
    async with app.run_test(size=(140, 30)) as pilot:
        stream = app.query_one(StepStream)
        from rich.text import Text

        stream.write(Text("x" * 400))
        await pilot.pause()
        lines = [line.text for line in stream.lines]
        assert len(lines[0]) > 80
        assert lines[0].startswith("• ")
        assert all(line.startswith("  ") for line in lines[1:])
        assert len(lines[0]) == stream.query_one(RichLog).scrollable_content_region.width
        stream.show_working(90061, "escape", "x" * 400)
        await pilot.pause()
        status = str(stream.query_one(Static).render()).splitlines()
        assert status[1].startswith("  └ ")
        assert all(line.startswith("    ") for line in status[2:])
        await pilot.resize_terminal(45, 30)
        await pilot.pause()
        status = str(stream.query_one(Static).render()).splitlines()
        assert all(len(line) <= 45 for line in status)
        action_start = next(i for i, line in enumerate(status) if line.startswith("  └ "))
        assert all(line.startswith("    ") for line in status[action_start + 1 :])


@pytest.mark.asyncio
async def test_append_only_and_whole_entry_retention(monkeypatch):
    from rich.text import Text

    from rio_coding.tui.widgets import StepStream

    app = RioTuiApp(FakeSession(), settings=TuiSettings(sidebar_position="off"))
    async with app.run_test(size=(100, 30)) as pilot:
        stream = app.query_one(StepStream)
        await pilot.pause()
        calls = []
        original = stream._entry_lines

        def counted(*args):
            calls.append(1)
            return original(*args)

        monkeypatch.setattr(stream, "_entry_lines", counted)
        for index in range(1001):
            stream.write(Text(f"entry-{index:04d} " + "x" * 1000))
        assert len(calls) == 1001  # Appends never rewrap existing entries, even at the cap.
        assert len(stream.entries) == 1000
        assert len(stream.lines) > 10000
        output = "\n".join(line.text for line in stream.lines)
        assert "entry-0000" not in output
        assert "• entry-0001" in output
        assert "• entry-1000" in output
        assert next(line.text for line in stream.lines if line.text).startswith("• entry-0001")


@pytest.mark.asyncio
async def test_initial_prompt_defers_rendering_until_layout():
    from rio_coding.tui.widgets import StepStream

    class StartupApp(RioTuiApp):
        def submit_prompt(self, text):
            super().submit_prompt(text)
            self.startup_lines = len(self.query_one(StepStream).lines)

    app = StartupApp(FakeSession(), initial_prompt="x" * 240, settings=TuiSettings())
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.startup_lines == 0
        assert app.query_one(StepStream).lines[0].text.startswith("• xxxxx")


def test_tool_label_fallback_and_result_truncation():
    from rio_agent.events import ActionEndEvent, ActionStartEvent
    from rio_ai.tools import AgentToolResult

    adapter = TuiEventAdapter()
    item = adapter.consume(ActionStartEvent(step=1, name="bash", arguments={"command": ""}))[0]
    assert item.text == 'Running bash({"command": ""})'
    item = adapter.consume(
        ActionEndEvent(
            step=1,
            name="bash",
            result=AgentToolResult(content="x" * 8001),
            is_error=False,
        )
    )[0]
    assert item.text == "bash: " + "x" * 8000 + "\n… output truncated"


@pytest.mark.asyncio
async def test_completed_status_freezes_and_next_run_restarts(monkeypatch):
    import asyncio

    from textual.widgets import Static

    from rio_coding.tui.widgets import StepStream

    gate = asyncio.Event()
    now = 100.0
    monkeypatch.setattr("rio_coding.tui.app.monotonic", lambda: now)

    class SlowSession(FakeSession):
        async def prompt(self, text):
            await gate.wait()
            yield SessionRunEndEvent(steps=1, state={}, answer="Done")

    app = RioTuiApp(SlowSession(), settings=TuiSettings())
    async with app.run_test() as pilot:
        status = app.query_one(StepStream).query_one(Static)
        assert not status.display
        app.submit_prompt("first")
        await pilot.pause()
        now = 232.0
        gate.set()
        await pilot.pause()
        assert str(status.render()).startswith("- Worked for 2m 12s --")
        assert len(str(status.render())) == app.query_one(StepStream).content_region.width
        now = 400.0
        app.refresh_state()
        app.refresh_working()
        await pilot.resize_terminal(100, 30)
        await pilot.pause()
        assert str(status.render()).startswith("- Worked for 2m 12s --")
        assert len(str(status.render())) == app.query_one(StepStream).content_region.width
        gate.clear()
        app.submit_prompt("second")
        await pilot.pause()
        assert "Working (0s" in str(status.render())
        gate.set()
        await pilot.pause()
        assert str(status.render()).startswith("- Worked for 0s --")
