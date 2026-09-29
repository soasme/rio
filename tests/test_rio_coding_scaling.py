"""End-to-end checks for append-only history and runtime state rebuilds."""

from __future__ import annotations

from conftest import step_response
from rio.ai import AgentTool, AgentToolResult, FakeProvider, TextContent
from rio.coding.paths import RioPaths
from rio.coding.resources import RioResourcePaths
from rio.coding.session import CodingSession, CodingSessionConfig
from rio.coding.session_store import InMemorySessionStorage, StepEntry
from rio.coding.step_footprint import estimate_text_tokens

STEPS = 100
OBSERVATION_SIZE = 400
CONTEXT_WINDOW = 10_000


async def _inspect(tool_call_id, arguments, signal=None, on_update=None):
    """A tool whose output is a fixed size, so growth can only come from history."""
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
    """A survey task: `steps` inspections, each recording one bounded finding.

    The state is updated every step but stays bounded, because each step
    overwrites the same two fields rather than accumulating new ones. That is
    the state-sufficiency assumption the paper relies on: what matters is
    projected into the schema as it is discovered.
    """
    streams = [
        step_response(
            reasoning="Thinking at length about what to inspect next. " * 20,
            state_delta={
                "scratch": {"last_inspected": f"module_{index}.py"},
                "findings": {"latest": f"module_{index}.py looks fine"},
            },
            action="inspect",
            args={"path": f"module_{index}.py"},
        )
        for index in range(steps)
    ]
    streams.append(
        step_response(
            reasoning="Survey complete.",
            state_delta={},
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
        estimate_text_tokens(message.content) for message in messages
    )


async def test_the_survey_completes_all_steps(tmp_path) -> None:
    session, provider = await run_survey(tmp_path)
    assert len(provider.calls) == STEPS + 1
    assert session.answer == f"Inspected {STEPS} modules; all fine."


async def test_context_rebuild_bounds_history_across_a_hundred_steps(tmp_path) -> None:
    """Prompts grow between rebuilds but remain below the context window."""
    _session, provider = await run_survey(tmp_path)
    sizes = [prompt_tokens(call) for call in provider.calls]

    assert max(sizes) < CONTEXT_WINDOW * 0.9
    assert any(later < earlier for earlier, later in zip(sizes, sizes[1:], strict=False))


async def test_rebuild_keeps_cumulative_history_cost_near_linear(tmp_path) -> None:
    _short_session, short_provider = await run_survey(tmp_path / "short", steps=25)
    _long_session, long_provider = await run_survey(tmp_path / "long", steps=50)

    short_total = sum(prompt_tokens(call) for call in short_provider.calls)
    long_total = sum(prompt_tokens(call) for call in long_provider.calls)

    ratio = long_total / short_total
    assert 1.8 < ratio < 2.8, f"doubling the steps changed cost by {ratio:.2f}x"


async def test_rebuild_beats_an_unbounded_history(tmp_path) -> None:
    """The thresholded history costs less than never rebuilding it."""
    session, provider = await run_survey(tmp_path)

    actual_total = sum(prompt_tokens(call) for call in provider.calls)

    entries = [e for e in await session.session_entries() if isinstance(e, StepEntry)]
    instructions_tokens = estimate_text_tokens(session.system_prompt)
    turn_tokens = [
        estimate_text_tokens(entry.observation or "")
        + estimate_text_tokens(entry.action.name)
        + estimate_text_tokens(str(entry.action.arguments))
        for entry in entries
    ]
    transcript_total = 0
    running = 0
    for tokens in turn_tokens:
        transcript_total += instructions_tokens + running
        running += tokens

    assert transcript_total > actual_total


async def test_the_state_stays_bounded_while_the_run_grows(tmp_path) -> None:
    """The prompt can only stay flat if the state itself does not accumulate."""
    session, _provider = await run_survey(tmp_path)
    entries = [e for e in await session.session_entries() if isinstance(e, StepEntry)]

    sizes = [len(str(entry.state)) for entry in entries]
    assert max(sizes) < min(sizes) * 1.5
    # Every step wrote to the state; none of them made it bigger.
    assert all(entry.state_delta for entry in entries[:-1])


async def test_reasoning_is_never_resent_even_at_scale(tmp_path) -> None:
    """Each step reasons at length; the journal keeps it, no prompt resends it."""
    session, provider = await run_survey(tmp_path)

    journal = repr(await session.session_entries())
    assert "Thinking at length" in journal

    prompts = " ".join(
        message.content for _m, _s, messages, _t in provider.calls for message in messages
    )
    assert "Thinking at length" not in prompts


async def test_recovery_needs_no_catch_up_steps(tmp_path) -> None:
    """A fresh session resumes mid-run with zero rebuild steps.

    The paper measures this as state recovery: a history-based agent needs
    several steps to re-derive its context after an interruption, and a
    state-based one needs none, because the checkpoint is the context.
    """
    session, _provider = await run_survey(tmp_path)
    storage = session.storage
    original_state = session.state

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

    assert resumed.state == original_state
    # Nothing was sent to a model to get there.
    assert resumed.provider.calls == []
