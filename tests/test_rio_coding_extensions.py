"""Extension hooks participate in the coding loop."""

from pathlib import Path

import pytest

from conftest import step_response
from rio.ai import FakeProvider
from rio.coding.extensions import ExtensionError
from rio.coding.resources import RioResourcePaths
from rio.coding.session import CodingSession, CodingSessionConfig
from rio.coding.session_store import CustomEntry, InMemorySessionStorage


async def test_extension_hooks_and_reload(tmp_path: Path):
    directory = tmp_path / "extensions"
    directory.mkdir()
    extension = directory / "example.py"
    extension.write_text("""
from rio.coding.extensions import InputHookResult
def setup(api):
    api.add_prompt_guideline("Always record the extension result.")
    api.on("input", lambda event, ctx: InputHookResult(action="transform", text="rewritten"))
    async def step(event, ctx):
        ctx.notebook["cells"].append({"cell_type": "markdown", "source": "injected"})
        await api.append_entry("example", {"step": event.step})
    api.on("step_start", step)
""")
    provider = FakeProvider([step_response(reasoning="discard", reply="done")])
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
    old_api = session.extensions._extensions[-1].api
    events = [event async for event in session.prompt("original")]
    assert any(type(event).__name__ == "StepStartEvent" for event in events)
    entries = await session.session_entries()
    assert any(isinstance(entry, CustomEntry) and entry.data == {"step": 0} for entry in entries)
    assert "injected" not in str(session.notebook)
    assert session.notebook["cells"][0]["source"] == "rewritten"
    assert not session.extensions.diagnostics
    extension.unlink()
    await session.reload()
    assert "Always record the extension result." not in session.system_prompt
    with pytest.raises(ExtensionError):
        old_api.notify("stale")
    await session.aclose()
