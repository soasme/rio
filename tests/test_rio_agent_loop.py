"""Tests for `run_context_loop`, the CLM loop whose context is a notebook.

Uses `rio.ai.FakeProvider` to script model replies and an in-process executor,
so these assert the runtime's own guarantees: patches become the next context,
changed cells run, invalid patches are retried, and a reply ends the run.
"""

from __future__ import annotations

import json

import pytest

from conftest import FakeExecutor, add_code, add_markdown, make_skill, step_response
from rio.agent import (
    ExecutionEvent,
    PatchEvent,
    ProviderResponseError,
    ReasoningEvent,
    RetriesExhaustedError,
    RunEndEvent,
    StepEndEvent,
    ValidationErrorEvent,
    run_context_loop,
)
from rio.ai import (
    AssistantDoneEvent,
    AssistantErrorEvent,
    AssistantMessage,
    FakeProvider,
    TextContent,
    ToolCall,
    malformed_tool_arguments,
)


async def _run(provider, *, skill=None, **kwargs) -> list:
    return [
        event
        async for event in run_context_loop(
            provider=provider, model="m", skill=skill or make_skill(), **kwargs
        )
    ]


def _request_notebook(provider, index: int) -> dict:
    text = provider.calls[index][2][0].text
    return json.loads(text.split("```json\n", 1)[1].split("\n```", 1)[0])


@pytest.mark.asyncio
async def test_the_task_is_a_user_cell_and_the_request_is_the_whole_notebook():
    provider = FakeProvider([step_response(reply="hi")])

    await _run(provider, observation="do the task")

    request = provider.calls[0][2]
    assert len(request) == 1
    cells = _request_notebook(provider, 0)["cells"]
    assert cells[0]["source"] == "do the task"
    assert cells[0]["metadata"] == {"rio": {"role": "user"}}
    assert "JSON Patch" in provider.calls[0][1]


@pytest.mark.asyncio
async def test_changed_cells_run_and_their_outputs_reach_the_next_request():
    provider = FakeProvider(
        [
            step_response(reasoning="compute", patch=[add_code("x = 41\nprint(x + 1)")]),
            step_response(reply="42"),
        ]
    )

    events = await _run(provider, observation="task")

    patch = next(e for e in events if isinstance(e, PatchEvent))
    assert patch.cells == [1]
    assert any(isinstance(e, ReasoningEvent) and e.reasoning == "compute" for e in events)
    cell = _request_notebook(provider, 1)["cells"][1]
    assert cell["outputs"][0]["text"] == "42\n"


@pytest.mark.asyncio
async def test_unchanged_cells_keep_their_outputs_and_later_cells_see_their_variables():
    provider = FakeProvider(
        [
            step_response(patch=[add_code("x = 1\nprint('first')")]),
            step_response(
                patch=[
                    {"op": "replace", "path": "/cells/1/outputs", "value": []},
                    add_code("print(x + 1)"),
                ]
            ),
            step_response(reply="done"),
        ]
    )

    events = await _run(provider, observation="task")

    second = [e for e in events if isinstance(e, ExecutionEvent)][1]
    assert second.cells == [2]
    cells = second.notebook["cells"]
    assert cells[1]["outputs"] == []
    assert cells[2]["outputs"][0]["text"] == "2\n"


@pytest.mark.asyncio
async def test_markdown_only_patches_run_nothing():
    provider = FakeProvider(
        [step_response(patch=[add_markdown("a note")]), step_response(reply="ok")]
    )

    events = await _run(provider, observation="task")

    assert not any(isinstance(e, ExecutionEvent) for e in events)
    assert _request_notebook(provider, 1)["cells"][1]["source"] == "a note"


@pytest.mark.asyncio
async def test_a_blank_reply_does_not_end_the_run():
    provider = FakeProvider(
        [step_response(patch=[add_markdown("a note")], reply=" "), step_response(reply="ok")]
    )

    events = await _run(provider, observation="task")

    run_end = events[-1]
    assert (run_end.steps, run_end.reply) == (2, "ok")
    assert [c["source"] for c in run_end.notebook["cells"][1:]] == ["a note", "ok"]


@pytest.mark.asyncio
async def test_a_reply_ends_the_run_and_is_kept_as_a_cell():
    provider = FakeProvider([step_response(reply="all done")])

    events = await _run(provider, observation="start", max_steps=100)

    step_end = next(e for e in events if isinstance(e, StepEndEvent))
    assert step_end.terminated
    run_end = events[-1]
    assert isinstance(run_end, RunEndEvent)
    assert (run_end.steps, run_end.reply) == (1, "all done")
    last = run_end.notebook["cells"][-1]
    assert (last["source"], last["metadata"]) == ("all done", {"rio": {"role": "assistant"}})


@pytest.mark.asyncio
async def test_an_invalid_notebook_is_retried_with_a_transient_correction():
    bad = {"op": "add", "path": "/cells/-", "value": {"cell_type": "nope", "source": ""}}
    provider = FakeProvider([step_response(patch=[bad]), step_response(reply="ok")])

    events = await _run(provider, observation="start")

    error = next(e for e in events if isinstance(e, ValidationErrorEvent)).error
    assert "invalid notebook" in error
    assert "Rejected reply" in provider.calls[1][2][0].text
    assert len(events[-1].notebook["cells"]) == 2


@pytest.mark.asyncio
async def test_arguments_of_the_wrong_type_are_retried():
    call = ToolCall(id="c", name="skill_step", arguments={"patch": "nope"})
    message = AssistantMessage(content=[call], stop_reason="toolUse")
    provider = FakeProvider(
        [[AssistantDoneEvent(reason="toolUse", message=message)], step_response(reply="ok")]
    )

    events = await _run(provider, observation="start")

    error = next(e for e in events if isinstance(e, ValidationErrorEvent)).error
    assert error.startswith("invalid `skill_step` arguments: `patch`")


@pytest.mark.asyncio
async def test_a_patch_that_fails_to_apply_is_retried():
    missing = {"op": "remove", "path": "/cells/9"}
    provider = FakeProvider([step_response(patch=[missing]), step_response(reply="ok")])

    events = await _run(provider, observation="start")

    error = next(e for e in events if isinstance(e, ValidationErrorEvent)).error
    assert error.startswith("patch failed")


@pytest.mark.asyncio
async def test_a_patch_that_grows_the_notebook_over_the_limit_is_rejected():
    provider = FakeProvider(
        [step_response(patch=[add_markdown("z" * 4000)]), step_response(reply="ok")]
    )

    events = await _run(provider, observation="start", context_window_tokens=1000)

    error = next(e for e in events if isinstance(e, ValidationErrorEvent)).error
    assert "over the ~800-token limit" in error


@pytest.mark.asyncio
async def test_a_patch_that_shrinks_an_oversized_notebook_is_accepted():
    shrink = {"op": "replace", "path": "/cells/0/source", "value": "short"}
    provider = FakeProvider([step_response(patch=[shrink], reply="ok")])

    events = await _run(provider, observation="z" * 4000, context_window_tokens=1000)

    assert not any(isinstance(e, ValidationErrorEvent) for e in events)
    assert events[-1].notebook["cells"][0]["source"] == "short"


@pytest.mark.asyncio
async def test_every_request_asks_to_review_each_cell_but_messages():
    provider = FakeProvider([step_response(patch=[add_code("x = 1")]), step_response(reply="ok")])

    await _run(provider, observation="task")

    assert "Manage every cell, every step" in provider.calls[0][1]
    assert "review cells" not in provider.calls[0][2][0].text
    assert "[review cells [1]: keep, summarize, or remove each]" in provider.calls[1][2][0].text


@pytest.mark.asyncio
async def test_every_request_lists_the_cell_paths_a_patch_can_address():
    provider = FakeProvider([step_response(patch=[add_code("x = 1")]), step_response(reply="ok")])

    events = await _run(provider, observation="task")

    ids = [cell["id"] for cell in events[-1].notebook["cells"]]
    second = provider.calls[1][2][0].text
    assert f"/cells/0 = {ids[0]}, /cells/1 = {ids[1]}; add cells at /cells/-" in second


@pytest.mark.asyncio
async def test_a_reply_without_the_step_tool_is_retried():
    no_call = AssistantMessage(content=[TextContent(text="I am done")], stop_reason="stop")
    provider = FakeProvider(
        [[AssistantDoneEvent(reason="stop", message=no_call)], step_response(reply="ok")]
    )

    events = await _run(provider, observation="start")

    error = next(e for e in events if isinstance(e, ValidationErrorEvent)).error
    assert "call the `skill_step` tool" in error


@pytest.mark.asyncio
async def test_retries_exhausted_raises():
    bad = step_response(patch=[{"op": "remove", "path": "/nope"}])
    provider = FakeProvider([bad, bad])

    with pytest.raises(RetriesExhaustedError):
        await _run(provider, observation="start", max_retries=1)


@pytest.mark.asyncio
async def test_a_provider_error_is_raised_instead_of_retried():
    error = AssistantMessage(content=[], stop_reason="error", error_message="429 rate limited")
    provider = FakeProvider([[AssistantErrorEvent(reason="error", error=error)]])

    with pytest.raises(ProviderResponseError, match="429 rate limited"):
        await _run(provider, observation="start")


@pytest.mark.asyncio
async def test_a_failing_executor_becomes_an_output_instead_of_crashing():
    class Broken(FakeExecutor):
        async def __call__(self, notebook, changed):
            raise RuntimeError("no kernel")

    provider = FakeProvider([step_response(patch=[add_code("1")]), step_response(reply="ok")])

    await _run(provider, skill=make_skill(executor=Broken()), observation="start")

    output = _request_notebook(provider, 1)["cells"][1]["outputs"][0]
    assert (output["ename"], output["evalue"]) == ("RuntimeError", "no kernel")


@pytest.mark.asyncio
async def test_a_truncated_call_is_reported_as_truncation():
    partial = '{"patch": [{"op'
    message = AssistantMessage(
        content=[ToolCall(id="c", name="skill_step", arguments=malformed_tool_arguments(partial))],
        stop_reason="length",
    )
    provider = FakeProvider(
        [[AssistantDoneEvent(reason="length", message=message)], step_response(reply="ok")]
    )

    events = await _run(provider, observation="start", max_retries=1)

    error = next(e for e in events if isinstance(e, ValidationErrorEvent)).error
    assert "output token limit" in error
    assert str(len(partial)) in error


@pytest.mark.asyncio
async def test_max_steps_stops_a_run_without_a_reply():
    provider = FakeProvider([step_response(patch=[add_markdown("a")])])

    events = await _run(provider, observation="start", max_steps=1)

    assert (events[-1].steps, events[-1].reply) == (1, None)


@pytest.mark.asyncio
async def test_the_notebook_records_the_kernel_and_stamps_cells_that_ran():
    provider = FakeProvider([step_response(patch=[add_code("x = 1")]), step_response(reply="ok")])

    await _run(provider, observation="task")

    first, second = _request_notebook(provider, 0), _request_notebook(provider, 1)
    assert first["metadata"]["rio"]["kernel"] == {"id": "k1", "running": False}
    assert second["metadata"]["rio"]["kernel"] == {"id": "k1", "running": True}
    assert second["cells"][1]["metadata"] == {"rio": {"kernel": "k1", "defines": ["x"]}}


@pytest.mark.asyncio
async def test_cells_from_an_older_kernel_are_listed_and_rerun_by_removing_the_stamp():
    executor = FakeExecutor()
    skill = make_skill(executor=executor)
    provider = FakeProvider([step_response(patch=[add_code("x = 41")], reply="set")])
    events = await _run(provider, skill=skill, observation="task")
    executor.restart("k2")

    unstamp = {"op": "remove", "path": "/cells/1/metadata/rio/kernel"}
    provider = FakeProvider(
        [
            step_response(patch=[unstamp]),
            step_response(patch=[add_code("print(x + 1)")], reply="ok"),
        ]
    )
    events = await _run(provider, skill=skill, notebook=events[-1].notebook, observation="more")

    assert "[stale cells [1]" in provider.calls[0][2][0].text
    assert next(e for e in events if isinstance(e, PatchEvent)).cells == [1]
    assert "stale cells" not in provider.calls[1][2][0].text
    assert events[-1].notebook["cells"][4]["outputs"][0]["text"] == "42\n"


@pytest.mark.asyncio
async def test_a_patch_that_reads_a_variable_from_a_stale_cell_is_rejected():
    executor = FakeExecutor()
    skill = make_skill(executor=executor)
    provider = FakeProvider([step_response(patch=[add_code("x = 41")], reply="set")])
    events = await _run(provider, skill=skill, observation="task")
    executor.restart("k2")

    unstamp = {"op": "remove", "path": "/cells/1/metadata/rio/kernel"}
    provider = FakeProvider(
        [
            step_response(patch=[add_code("print(x + 1)")]),
            step_response(patch=[unstamp, add_code("print(x + 1)")], reply="ok"),
        ]
    )
    events = await _run(provider, skill=skill, notebook=events[-1].notebook, observation="more")

    error = next(e for e in events if isinstance(e, ValidationErrorEvent)).error
    assert error.startswith("cell 4 reads `x`, which only stale cell 1 defined")
    assert events[-1].notebook["cells"][4]["outputs"][0]["text"] == "42\n"
