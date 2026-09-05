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
