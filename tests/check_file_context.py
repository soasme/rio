"""Run with: uv run python tests/check_file_context.py"""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from conftest import step_response
from rio.agent import apply_state_delta, state_size_chars
from rio.ai import FakeProvider
from rio.coding.coding_skill import CodingSkillOptions, build_coding_skill
from rio.coding.file_context import FileContext
from rio.coding.session_runner import SessionRunner, SessionRunnerConfig
from rio.coding.session_store import InMemorySessionStorage, StepEntry
from rio.coding.tools import create_coding_tools


async def check():
    with TemporaryDirectory() as directory:
        cwd = Path(directory)
        (cwd / "file.py").write_text("one\ntwo", encoding="utf-8")
        tools = create_coding_tools(cwd=cwd)
        read = next(tool for tool in tools if tool.name == "read")
        state = {"files": {}}
        result, delta = await FileContext(cwd, state_size_chars(state)).execute(
            read, "read", {"file": "file.py"}, state
        )
        assert apply_state_delta(state, delta) == state
        assert "Forget a file" in result.text

        write = next(tool for tool in tools if tool.name == "write")
        planned = {"files": {"new.py": {"note": "planned"}}}
        _, delta = await FileContext(cwd).execute(
            write, "write", {"path": "new.py", "content": "new"}, planned
        )
        created = apply_state_delta(planned, delta)["files"]["new.py"]
        assert created["status"] == "created" and created["note"] == "planned"
        assert created["context"]["slices"] == {"1-1": "new"}

        storage = InMemorySessionStorage()
        forget = {"files": {"file.py": {"context": None, "note": "keep this"}}}
        provider = FakeProvider(
            [
                step_response(
                    reasoning="", state_delta={}, action="read", args={"path": "file.py"}
                ),
                step_response(
                    reasoning="",
                    state_delta=forget,
                    action="write",
                    args={"path": "file.py", "content": "updated"},
                ),
                step_response(
                    reasoning="", state_delta={}, action="respond", args={"message": "ok"}
                ),
            ]
        )
        runner = SessionRunner(
            SessionRunnerConfig(
                provider=provider,
                model="test",
                storage=storage,
                skill=build_coding_skill(CodingSkillOptions("instructions", cwd, tuple(tools))),
            )
        )
        async for _ in runner.run("read, forget, then write"):
            pass
        steps = [entry for entry in await storage.read_all() if isinstance(entry, StepEntry)]
        assert steps[0].state_delta == {}  # Runtime content doesn't replace the model's patch.
        assert steps[0].state["files"]["file.py"]["context"]["slices"] == {"1-2": "one\ntwo"}
        assert steps[1].state_delta == forget  # Preserve the null deletion marker.
        entry = steps[1].state["files"]["file.py"]
        assert "context" not in entry and entry["note"] == "keep this"
        assert entry["hash"] != steps[0].state["files"]["file.py"]["hash"]
        assert runner.state == steps[-1].state
        assert (cwd / "file.py").read_text(encoding="utf-8") == "updated"


if __name__ == "__main__":
    asyncio.run(check())
    print("file context checks passed")
