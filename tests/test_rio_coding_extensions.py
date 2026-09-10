"""Extension actions and hooks participate in the state-only coding loop."""

from pathlib import Path

import pytest

from conftest import step_response
from rio.ai import FakeProvider
from rio.coding.extensions import ExtensionError
from rio.coding.resources import RioResourcePaths
from rio.coding.session import CodingSession, CodingSessionConfig
from rio.coding.session_store import CustomEntry, InMemorySessionStorage


async def test_extension_hooks_state_actions_and_reload(tmp_path: Path):
    directory = tmp_path / "extensions"
    directory.mkdir()
    extension = directory / "example.py"
    extension.write_text("""
from rio.ai import AgentTool, AgentToolResult, TextContent
from rio.coding.extensions import InputHookResult, ToolCallHookResult, ToolResultHookResult
API = None
SEEN = []
def setup(api):
    global API
    API = api
    api.add_prompt_guideline("Always record the extension result.")
    api.on("input", lambda event, ctx: InputHookResult(action="transform", text="rewritten"))
    api.on("tool_call", lambda event, ctx: ToolCallHookResult(arguments={"value": "hooked"}))
    api.on("tool_result", lambda event, ctx: ToolResultHookResult(content="result hook"))
    def step(event, ctx):
        SEEN.append((event.step, ctx.state))
        ctx.state["environment"]["injected"] = "must not persist"
        event.state["environment"]["injected"] = "must not persist"
    api.on("step_start", step)
    async def execute(call_id, arguments, signal=None, on_update=None):
        await api.append_entry("example", dict(arguments))
        return AgentToolResult(content=[TextContent(text="done")], terminate=True)
    api.register_tool(AgentTool(name="extension_action", label="Extension", description="Finish",
        parameters={"type":"object","properties":{}}, execute_fn=execute))
""")
    provider = FakeProvider(
        [
            step_response(
                reasoning="discard",
                state_delta={"goal": "rewritten"},
                action="extension_action",
                args={},
            )
        ]
    )
    session = await CodingSession.load(
        CodingSessionConfig(
            provider=provider,
            model="fake",
            cwd=tmp_path,
            storage=InMemorySessionStorage(),
            resource_paths=RioResourcePaths(root=tmp_path, cwd=tmp_path),
        )
    )
    assert "Always record the extension result." in session.system_prompt
    assert "extension_action" in {tool.name for tool in session.tools}
    old_api = session.extensions._extensions[-1].api
    events = [event async for event in session.prompt("original")]
    assert any(type(event).__name__ == "StepStartEvent" for event in events)
    entries = await session.session_entries()
    assert any(
        isinstance(entry, CustomEntry) and entry.data == {"value": "hooked"} for entry in entries
    )
    assert "injected" not in session.state["environment"]
    assert not session.extensions.diagnostics
    extension.unlink()
    await session.reload()
    assert "extension_action" not in {tool.name for tool in session.tools}
    with pytest.raises(ExtensionError):
        old_api.notify("stale")
    await session.aclose()
