"""End-to-end checks that a long run stays inside the context window.

The scripted model never edits its context, so these exercise the runtime's
overflow guard: the last line of defense when a model lets its context grow.
"""

from __future__ import annotations

from conftest import step_response
from rio.agent import render_context
from rio.ai import AgentTool, AgentToolResult, FakeProvider, TextContent
from rio.coding.paths import RioPaths
from rio.coding.resources import RioResourcePaths
from rio.coding.session import CodingSession, CodingSessionConfig
from rio.coding.session_store import InMemorySessionStorage, StepEntry, entry_context
from rio.coding.step_footprint import estimate_text_tokens

STEPS = 100
OBSERVATION_SIZE = 400
CONTEXT_WINDOW = 10_000


async def _inspect(tool_call_id, arguments, signal=None, on_update=None):
    """A tool whose output is a fixed size."""
    target = arguments.get("path", "?")
    body = f"inspect {target}\n" + ("data line\n" * (OBSERVATION_SIZE // 10))
    return AgentToolResult(content=[TextContent(text=body)])


async def _respond(tool_call_id, arguments, signal=None, on_update=None):
    return AgentToolResult(
        content=[TextContent(text=str(arguments.get("message", "")))], terminate=True
    )


INSPECT = AgentTool(
    name="inspect",
    label="Inspect",
    description="Inspect one file in the repository.",
    parameters={"type": "object", "properties": {"path": {"type": "string"}}},
    execute_fn=_inspect,
)
RESPOND = AgentTool(
    name="respond",
    label="Respond",
    description="Answer the user and end the turn.",
    parameters={"type": "object", "properties": {"message": {"type": "string"}}},
    execute_fn=_respond,
)


def long_run_streams(steps: int = STEPS):
    """A survey task: `steps` inspections, then an answer."""
    streams = [
        step_response(
            reasoning="Thinking at length about what to inspect next. " * 20,
            action="inspect",
            args={"path": f"module_{index}.py"},
        )
        for index in range(steps)
    ]
    streams.append(
        step_response(
            reasoning="Survey complete.",
            action="respond",
            args={"message": f"Inspected {steps} modules; all fine."},
        )
    )
    return streams


async def run_survey(tmp_path, steps: int = STEPS):
    """Run the survey and return the session plus the provider's recorded calls."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    home = tmp_path / "home"
    paths = RioPaths(home=home / ".rio", agents_home=home / ".agents")
    provider = FakeProvider(long_run_streams(steps))

    session = await CodingSession.load(
        CodingSessionConfig(
            provider=provider,
            model="test-model",
            cwd=repo,
            paths=paths,
            resource_paths=RioResourcePaths(
                root=home / ".rio", cwd=repo, agents_root=home / ".agents", paths=paths
            ),
            storage=InMemorySessionStorage(),
            tools=(INSPECT, RESPOND),
            max_steps=steps + 5,
            context_window_tokens=CONTEXT_WINDOW,
        )
    )
    async for _event in session.prompt("survey every module in the repository"):
        pass
    return session, provider


def prompt_tokens(call) -> int:
    """Tokens in one recorded provider call's prompt: system plus messages."""
    _model, system, messages, _tools = call
    return estimate_text_tokens(system) + sum(
        estimate_text_tokens(message.text) for message in messages
    )


async def test_the_survey_completes_all_steps(tmp_path) -> None:
    session, provider = await run_survey(tmp_path)
    assert len(provider.calls) == STEPS + 1
    assert session.answer == f"Inspected {STEPS} modules; all fine."


async def test_the_overflow_guard_bounds_prompts_across_a_hundred_steps(tmp_path) -> None:
    """Prompts grow, then the guard keeps them below the context window."""
    _session, provider = await run_survey(tmp_path)
    sizes = [prompt_tokens(call) for call in provider.calls]

    assert max(sizes) < CONTEXT_WINDOW
    assert sizes[-1] > sizes[0]


async def test_the_journaled_context_stays_under_the_limit(tmp_path) -> None:
    session, _provider = await run_survey(tmp_path)
    limit = session.context_usage.limit_tokens
    entries = [e for e in await session.session_entries() if isinstance(e, StepEntry)]

    sizes = [estimate_text_tokens(render_context(entry_context(entry))) for entry in entries]
    assert max(sizes) < limit * 1.2


async def test_reasoning_stays_in_the_context_until_it_is_edited_out(tmp_path) -> None:
    _session, provider = await run_survey(tmp_path, steps=3)

    second_prompt = " ".join(message.text for message in provider.calls[1][2])
    assert "Thinking at length" in second_prompt


async def test_recovery_needs_no_catch_up_steps(tmp_path) -> None:
    """A fresh session resumes with the same context and no model calls."""
    session, _provider = await run_survey(tmp_path)
    storage = session.storage
    original_context = session.context

    repo = tmp_path / "repo"
    home = tmp_path / "home"
    paths = RioPaths(home=home / ".rio", agents_home=home / ".agents")
    resumed = await CodingSession.load(
        CodingSessionConfig(
            provider=FakeProvider([]),
            model="test-model",
            cwd=repo,
            paths=paths,
            resource_paths=RioResourcePaths(
                root=home / ".rio", cwd=repo, agents_root=home / ".agents", paths=paths
            ),
            storage=storage,
            tools=(INSPECT, RESPOND),
        )
    )

    assert resumed.context == original_context
    # Nothing was sent to a model to get there.
    assert resumed.provider.calls == []
