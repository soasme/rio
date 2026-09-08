"""Tests for the token-accounting slice: step_footprint, session_usage,
session_stats, branch_summary, and diagnostics.

The property that matters most here is the one the SKILL.state paper's
whole cost claim rests on: a step's prompt footprint does not depend on how
many steps came before it, so cumulative cost over a run is linear in the
number of steps, not quadratic. See `test_footprint_does_not_grow_with_step_index`
and `test_projected_cumulative_tokens_is_linear_in_steps` below.
"""

from __future__ import annotations

import json

from rio_ai.messages import AssistantMessage, AssistantMessageDiagnostic, TextContent
from rio_ai.tools import AgentTool, AgentToolResult
from rio_coding.branch_summary import summarize_state_diff
from rio_coding.diagnostics import (
    AgentCallDiagnosticContext,
    AgentCallDiagnosticLogger,
    new_agent_call_run_id,
)
from rio_coding.paths import RioPaths
from rio_coding.session_stats import calculate_session_stats
from rio_coding.session_store import ActionRecord, StepEntry
from rio_coding.session_store.entries import (
    BranchSummaryEntry,
    ModelChangeEntry,
    ReasoningEntry,
    ThinkingLevelChangeEntry,
    TurnEntry,
    ValidationFailureEntry,
)
from rio_coding.session_usage import collect_session_usage, estimated_step_cost
from rio_coding.step_footprint import (
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    StepFootprint,
    context_window_utilization,
    estimate_step_footprint,
    estimate_text_tokens,
    estimate_tool_tokens,
    projected_cumulative_tokens,
)


async def _noop_execute(tool_call_id, arguments, signal=None, on_update=None):
    return AgentToolResult(content=[TextContent(text="ok")])


def _make_tool(name: str = "skill_step") -> AgentTool:
    return AgentTool(
        name=name,
        label="Skill Step",
        description="Advance the skill by one step.",
        parameters={"type": "object", "properties": {}},
        execute_fn=_noop_execute,
    )


class TestStepFootprint:
    def test_text_token_estimate_is_deterministic(self) -> None:
        assert estimate_text_tokens("") == 0
        assert estimate_text_tokens("a") == 1
        assert estimate_text_tokens("abcd") == 1
        assert estimate_text_tokens("abcde") == 2

    def test_tool_token_estimate_grows_with_schema_size(self) -> None:
        small = _make_tool("a")
        big = AgentTool(
            name="a",
            label="Big",
            description="x" * 500,
            parameters={"type": "object", "properties": {"p": {"type": "string"}}},
            execute_fn=_noop_execute,
        )
        assert estimate_tool_tokens(big) > estimate_tool_tokens(small)

    def test_footprint_breaks_down_the_fixed_three_piece_prompt(self) -> None:
        footprint = estimate_step_footprint(
            instructions="You are a coding skill.",
            state={"goal": "fix the bug", "findings": {}},
            observation="tests pass",
            tools=(_make_tool(),),
        )
        assert isinstance(footprint, StepFootprint)
        assert footprint.instructions_tokens > 0
        assert footprint.state_tokens > 0
        assert footprint.observation_tokens > 0
        assert footprint.tools_tokens > 0
        assert footprint.total_tokens == (
            footprint.instructions_tokens
            + footprint.state_tokens
            + footprint.observation_tokens
            + footprint.tools_tokens
        )

    def test_footprint_does_not_grow_with_step_index(self) -> None:
        """The paper's core property, measured at the per-step-prompt layer.

        The execution state is bounded: each step keeps exactly one finding,
        just a different one, mirroring how a real SKILL.state run curates
        its state instead of accumulating an ever-growing transcript. The
        estimated footprint must stay exactly the same no matter how far
        into the run the step is.
        """
        instructions = "You are a coding skill with fixed instructions."
        tools = (_make_tool(),)

        def state_at_step(step: int) -> dict:
            # Fixed-length key and value every time -- only the digit rotates.
            return {"goal": "g", "findings": {f"k{step % 10}": "same length value"}}

        footprints = [
            estimate_step_footprint(
                instructions=instructions,
                state=state_at_step(step),
                observation="fixed-size observation text",
                tools=tools,
            )
            for step in (0, 1, 50, 500, 5_000)
        ]

        totals = {footprint.total_tokens for footprint in footprints}
        assert len(totals) == 1, f"footprint grew across steps: {totals}"

    def test_context_window_utilization_is_constant_across_a_run(self) -> None:
        footprint = estimate_step_footprint(
            instructions="instructions", state={"goal": "g"}, observation="obs"
        )
        early = context_window_utilization(footprint, DEFAULT_CONTEXT_WINDOW_TOKENS)
        late = context_window_utilization(footprint, DEFAULT_CONTEXT_WINDOW_TOKENS)
        assert early == late
        assert 0 < early < 1

    def test_context_window_utilization_rejects_non_positive_window(self) -> None:
        footprint = estimate_step_footprint(instructions="i", state={}, observation="o")
        try:
            context_window_utilization(footprint, 0)
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")

    def test_projected_cumulative_tokens_is_linear_in_steps(self) -> None:
        """Cumulative cost over T steps is O(T), not O(T^2).

        Doubling the step count must exactly double the projection, since a
        SKILL.state step's prompt never includes any earlier step's prompt.
        """
        footprint = estimate_step_footprint(
            instructions="instructions", state={"goal": "g"}, observation="obs"
        )
        ten = projected_cumulative_tokens(footprint, 10)
        twenty = projected_cumulative_tokens(footprint, 20)
        two_hundred = projected_cumulative_tokens(footprint, 200)

        assert twenty == ten * 2
        assert two_hundred == ten * 20
        assert ten == footprint.total_tokens * 10

    def test_projected_cumulative_tokens_rejects_negative_steps(self) -> None:
        footprint = estimate_step_footprint(instructions="i", state={}, observation="o")
        try:
            projected_cumulative_tokens(footprint, -1)
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")


def _make_chain(count: int) -> list[StepEntry]:
    entries: list[StepEntry] = []
    parent = None
    for index in range(count):
        entry = StepEntry(
            parent_id=parent,
            step=index,
            state_delta={"findings": {f"f{index}": "v"}},
            state={"goal": "g", "findings": {f"f{index}": "v"}},
            action=ActionRecord(name="read", arguments={"path": f"file{index}.py"}),
            observation=f"observation {index}",
        )
        entries.append(entry)
        parent = entry.id
    return entries


class TestSessionUsage:
    def test_collect_session_usage_reports_one_step_per_entry(self) -> None:
        entries = _make_chain(3)
        usage = collect_session_usage(entries, instructions="instructions", tools=(_make_tool(),))

        assert [step.number for step in usage.steps] == [1, 2, 3]
        assert usage.action_calls == (("read", 3),)
        assert usage.total_tokens == sum(step.total_tokens for step in usage.steps)
        assert all(step.estimated_cost is None for step in usage.steps)

    def test_journaled_reasoning_does_not_change_the_step_footprint(self) -> None:
        """Journaling is free at prompt time: nothing journaled reaches a prompt.

        A step's footprint is instructions + state + observation + tools. A
        `ReasoningEntry` is none of those, however long it is, so a journal
        with one beside every step measures exactly the same as one without.
        """
        entries = _make_chain(3)
        noisy = [
            item
            for entry in entries
            for item in (
                ReasoningEntry(parent_id=entry.parent_id, step=entry.step, reasoning="w" * 10_000),
                entry,
            )
        ]

        bare_usage = collect_session_usage(entries, instructions="instructions")
        noisy_usage = collect_session_usage(noisy, instructions="instructions")

        assert noisy_usage.steps == bare_usage.steps
        assert noisy_usage.total_tokens == bare_usage.total_tokens

    def test_collect_session_usage_positions_events_against_the_next_step(self) -> None:
        entries = _make_chain(1)
        model_change = ModelChangeEntry(parent_id=entries[0].id, model="gpt-5")
        thinking_change = ThinkingLevelChangeEntry(parent_id=model_change.id, thinking_level="high")
        branch = BranchSummaryEntry(parent_id=thinking_change.id, summary="did stuff")
        next_step = StepEntry(
            parent_id=branch.id,
            step=1,
            state_delta={},
            state={"goal": "g"},
            action=ActionRecord(name="respond", arguments={}),
            terminated=True,
        )

        usage = collect_session_usage(
            [*entries, model_change, thinking_change, branch, next_step],
            instructions="instructions",
        )

        assert len(usage.steps) == 2
        kinds = [event.kind for event in usage.events]
        assert kinds == ["model", "thinking", "branch"]
        assert all(event.step_number == 2 for event in usage.events)

    def test_estimated_step_cost_prices_the_whole_footprint_as_input(self) -> None:
        footprint = estimate_step_footprint(
            instructions="i" * 1000, state={"goal": "g"}, observation="o"
        )
        cost = estimated_step_cost("openai", "gpt-4.1", footprint)
        assert cost is not None
        expected = footprint.total_tokens * 2.0 / 1_000_000
        assert cost == expected

    def test_estimated_step_cost_is_none_for_an_unknown_provider(self) -> None:
        footprint = estimate_step_footprint(instructions="i", state={}, observation="o")
        assert estimated_step_cost("no-such-provider", "no-such-model", footprint) is None

    def test_collect_session_usage_prices_steps_when_provider_and_model_given(self) -> None:
        entries = _make_chain(2)
        usage = collect_session_usage(
            entries,
            instructions="instructions",
            provider="openai",
            model="gpt-4.1",
        )
        assert all(step.estimated_cost is not None for step in usage.steps)
        assert usage.total_cost == sum(step.estimated_cost for step in usage.steps)


class TestSessionStats:
    def test_calculate_session_stats_aggregates_turns_steps_and_failures(self) -> None:
        turn = TurnEntry(observation="start")
        steps = _make_chain(4)
        steps[0].parent_id = turn.id
        failure = ValidationFailureEntry(parent_id=steps[-1].id, step=4, attempt=1, error="bad")

        stats = calculate_session_stats(
            [turn, *steps, failure], instructions="instructions", tools=(_make_tool(),)
        )

        assert stats.turn_count == 1
        assert stats.step_count == 4
        assert stats.validation_error_count == 1
        assert stats.prompt_tokens == (
            stats.instructions_tokens
            + stats.state_tokens
            + stats.observation_tokens
            + stats.tools_tokens
        )
        assert stats.average_step_tokens == stats.prompt_tokens / 4

    def test_calculate_session_stats_of_an_empty_journal(self) -> None:
        stats = calculate_session_stats([], instructions="instructions")
        assert stats.step_count == 0
        assert stats.average_step_tokens is None
        assert stats.estimated_cost is None

    def test_session_stats_step_count_matches_session_usage_action_count(self) -> None:
        """One committed step is exactly one action call under SKILL.state."""
        entries = _make_chain(6)
        stats = calculate_session_stats(entries, instructions="instructions")
        usage = collect_session_usage(entries, instructions="instructions")
        assert stats.step_count == sum(count for _name, count in usage.action_calls)


class TestBranchSummary:
    def test_no_changes_between_identical_states(self) -> None:
        state = {"goal": "g", "findings": {"a": "1"}}
        assert summarize_state_diff(state, dict(state)) == "No changes."

    def test_reports_added_removed_and_changed_fields(self) -> None:
        before = {"goal": "g", "blockers": ["network"], "answer": None}
        after = {"goal": "fixed goal", "scratch": {"note": "x"}}

        summary = summarize_state_diff(before, after)

        assert "Added: scratch=dict[1]" in summary
        assert "Removed: answer, blockers" in summary
        assert "Changed: goal (g -> fixed goal)" in summary

    def test_long_values_are_truncated(self) -> None:
        before = {"notes": "short"}
        after = {"notes": "x" * 500}
        summary = summarize_state_diff(before, after)
        assert "..." in summary
        assert len(summary) < 500


class TestDiagnostics:
    def test_logger_from_paths_uses_the_agent_calls_log_path(self, tmp_path) -> None:
        paths = RioPaths(home=tmp_path / "home")
        logger = AgentCallDiagnosticLogger.from_paths(paths)
        assert logger.path == paths.agent_calls_log_path

    def test_log_exception_writes_a_jsonl_entry(self, tmp_path) -> None:
        logger = AgentCallDiagnosticLogger(tmp_path / "diagnostics.jsonl")
        context = AgentCallDiagnosticContext(
            provider_name="openai",
            model="gpt-4.1",
            cwd=tmp_path,
            session_id="sess-1",
            run_id=new_agent_call_run_id(),
        )
        try:
            raise ValueError("boom")
        except ValueError as exc:
            logger.log_exception(context=context, phase="agent_loop", exc=exc)

        lines = logger.path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["kind"] == "exception"
        assert entry["exception"]["type"] == "ValueError"
        assert entry["exception"]["message"] == "boom"
        assert entry["provider_name"] == "openai"

    def test_log_assistant_error_extracts_safe_provider_details(self, tmp_path) -> None:
        logger = AgentCallDiagnosticLogger(tmp_path / "diagnostics.jsonl")
        context = AgentCallDiagnosticContext(
            provider_name="anthropic",
            model="claude",
            cwd=tmp_path,
            session_id=None,
            run_id=new_agent_call_run_id(),
        )
        message = AssistantMessage(
            content="",
            stop_reason="error",
            error_message="rate limited",
            diagnostics=[
                AssistantMessageDiagnostic(
                    type="provider_error",
                    details={
                        "status_code": 429,
                        "attempts": 3,
                        "secret": "should not leak",
                    },
                )
            ],
        )

        logger.log_assistant_error(context=context, phase="agent_loop", message=message)

        entry = json.loads(logger.path.read_text(encoding="utf-8").splitlines()[0])
        assert entry["error"]["message"] == "rate limited"
        assert entry["error"]["provider"]["status_code"] == 429
        assert entry["error"]["provider"]["attempts"] == 3
        assert "secret" not in json.dumps(entry)

    def test_log_huggingface_route_failover_records_outcome(self, tmp_path) -> None:
        logger = AgentCallDiagnosticLogger(tmp_path / "diagnostics.jsonl")
        context = AgentCallDiagnosticContext(
            provider_name="huggingface",
            model="router",
            cwd=tmp_path,
            session_id=None,
            run_id=new_agent_call_run_id(),
        )
        logger.log_huggingface_route_failover(
            context=context,
            failed_route="route-a",
            replacement_route="route-b",
            success=True,
            error_message=None,
        )
        entry = json.loads(logger.path.read_text(encoding="utf-8").splitlines()[0])
        assert entry["route_failover"] == {
            "from": "route-a",
            "to": "route-b",
            "success": True,
        }
