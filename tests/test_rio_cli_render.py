"""Human transcript regressions for the durable event stream."""

from __future__ import annotations

import json
from typing import Any

import pytest

from rio.ai import ProviderToolCallDeltaEvent
from rio.cli.render import PlainEventRenderer, StepDraft


def emit(renderer: PlainEventRenderer, kind: str, **changes: dict[str, Any]) -> None:
    renderer.render({"type": kind, "changes": changes})


def execution(stdout: str = "", stderr: str = "", **extra: Any) -> dict[str, Any]:
    return {"cell_id": 2, "output": {"stdout": stdout, "stderr": stderr}, **extra}


def test_committed_cells_show_literal_source_notes_and_successors(
    capsys: pytest.CaptureFixture[str],
) -> None:
    renderer = PlainEventRenderer()
    cells = {
        "10": {"id": 10, "previous_id": 2, "kind": "code", "source": "print('[red]')\n"},
        "9": {"id": 9, "previous_id": None, "kind": "note", "text": "Inspect files.\nThen test."},
    }
    emit(renderer, "model_response", cells=cells)
    emit(renderer, "turn_prepared", meta={"state": {"cells": list(cells.values())}})
    assert capsys.readouterr().out == ""
    emit(renderer, "patch_accepted", cells=cells)
    emit(renderer, "patch_accepted", cells={})
    assert capsys.readouterr().out == ("• Inspect files.\n  Then test.\n\n• print('[red]')\n")


def test_streamed_snapshots_preserve_partial_lines_and_do_not_repeat(
    capsys: pytest.CaptureFixture[str],
) -> None:
    renderer = PlainEventRenderer()
    emit(renderer, "execution_started", executions={"2": execution()})
    emit(renderer, "execution_output", executions={"2": execution("hel")})
    assert capsys.readouterr().out == "  └ hel"
    emit(renderer, "execution_output", executions={"2": execution("hello\n")})
    emit(renderer, "execution_output", executions={"2": execution("hello\nworld")})
    emit(renderer, "execution_output", executions={"2": execution("hello\nworld")})
    emit(
        renderer,
        "execution_finished",
        executions={"2": execution("hello\nworld", result={"type": "success", "exit_code": 0})},
    )
    assert capsys.readouterr().out == "lo\n    world\n  └ success (exit 0)\n"


def test_interleaved_notes_and_stderr_keep_separate_entries(
    capsys: pytest.CaptureFixture[str],
) -> None:
    renderer = PlainEventRenderer()
    emit(renderer, "execution_started", executions={"2": execution()})
    emit(renderer, "execution_output", executions={"2": execution("one")})
    emit(
        renderer,
        "patch_accepted",
        cells={
            "3": {"id": 3, "previous_id": None, "kind": "note", "text": "Waiting [literal]"},
        },
    )
    emit(renderer, "execution_output", executions={"2": execution("onetwo", "warning\n")})
    emit(renderer, "run_ended", meta={"terminal": {"result": "failure", "reason": "Stopped"}})
    assert capsys.readouterr().out == (
        "  └ one\n• Waiting [literal]\n  └ two\n  └ stderr: warning\n\n• Failure: Stopped\n"
    )


@pytest.mark.parametrize(
    "result, expected",
    [
        ({"type": "success", "exit_code": 0}, "success (exit 0)"),
        ({"type": "error", "exit_code": 1}, "error (exit 1)"),
        ({"type": "error", "reason": "uv unavailable"}, "error: uv unavailable"),
        ({"type": "cancelled"}, "cancelled"),
        ({"type": "UnknownExecution", "reason": "worker lost"}, "UnknownExecution: worker lost"),
    ],
)
def test_silent_executions_still_show_outcome(
    result: str, expected: str, capsys: pytest.CaptureFixture[str]
) -> None:
    emit(PlainEventRenderer(), "execution_finished", executions={"2": execution(result=result)})
    assert capsys.readouterr().out == f"  └ {expected}\n"


@pytest.mark.parametrize("durable_truncation", [False, True])
def test_truncation_is_explicit_and_only_reported_once(
    durable_truncation: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    renderer = PlainEventRenderer()
    record = execution("x" * (20 if durable_truncation else 8_001), truncated=durable_truncation)
    emit(renderer, "execution_output", executions={"2": record})
    emit(renderer, "output_truncated", executions={"2": record})
    emit(
        renderer, "execution_finished", executions={"2": {**record, "result": {"type": "success"}}}
    )
    output = capsys.readouterr().out
    assert output.count("x") == (20 if durable_truncation else 8_000)
    assert output.count("[output truncated]") == 1
    assert output.endswith("  └ success\n")


def test_conclusion_request_does_not_claim_success_and_retries_are_visible(
    capsys: pytest.CaptureFixture[str],
) -> None:
    renderer = PlainEventRenderer()
    emit(
        renderer,
        "patch_accepted",
        cells={"3": {"id": 3, "previous_id": None, "kind": "note", "text": "Checked."}},
        controls={
            "10": {"control": {"type": "cancel", "target_cell_id": 2, "reason": "Stale."}},
            "9": {"control": {"type": "conclusion", "result": "success", "reason": "Done?"}},
        },
    )
    emit(renderer, "patch_rejected", turns={"t1": {"error": "Invalid patch"}})
    emit(renderer, "provider_failed", turns={"t1": {"provider_error": "HTTP 529"}})
    assert capsys.readouterr().out == (
        "• Checked.\n\n• Done?\n\n• Stale.\n\n• Retry: Invalid patch\n\n"
        "• Provider error: HTTP 529\n"
    )


def test_command_arguments_are_quoted_without_cell_metadata(
    capsys: pytest.CaptureFixture[str],
) -> None:
    renderer = PlainEventRenderer()
    emit(
        renderer,
        "patch_accepted",
        cells={
            "4": {
                "id": 4,
                "previous_id": None,
                "kind": "cmd",
                "argv": ["printf", "%s", "a b", "$HOME", "", "[red]"],
            },
        },
    )
    assert capsys.readouterr().out == "• printf %s 'a b' '$HOME' '' '[red]'\n"


def test_output_chunks_then_final_snapshot_do_not_repeat(
    capsys: pytest.CaptureFixture[str],
) -> None:
    renderer = PlainEventRenderer()
    emit(renderer, "execution_output", output_chunks={"2": execution("hel")})
    emit(renderer, "execution_output", output_chunks={"2": execution("lo\n")})
    emit(
        renderer,
        "execution_finished",
        executions={"2": execution("hello\n", result={"type": "success", "exit_code": 0})},
    )
    assert capsys.readouterr().out == "  └ hello\n  └ success (exit 0)\n"


def stream(renderer: PlainEventRenderer, ops: list[dict[str, Any]], size: int = 3) -> None:
    raw = json.dumps({"ops": ops})
    for start in range(0, len(raw), size):
        renderer.preview(ProviderToolCallDeltaEvent(delta=raw[start : start + size]))


def test_step_draft_reads_bodies_from_any_fragmentation() -> None:
    ops = [
        {"op": "add", "cell": {"kind": "note", "text": 'say "hi" é 😀\n'}},
        {"op": "replace", "id": 3, "cell": {"source": "print(1)\n", "kind": "code"}},
        {"op": "add", "cell": {"kind": "cmd", "argv": ["pytest", "a b"]}},
        {
            "op": "control",
            "control": {"type": "resolution", "evidence_refs": ["e:1"], "reason": "ok"},
        },
    ]
    for raw in (json.dumps({"ops": ops}), json.dumps({"ops": ops}, indent=1)):
        bodies: dict[int, str] = {}
        draft = StepDraft()
        for char in raw:
            for index, text in draft.feed(char):
                bodies[index] = bodies.get(index, "") + text
        assert bodies == {0: 'say "hi" é 😀\n', 1: "print(1)\n", 2: "pytest 'a b'"}


def test_streamed_cells_show_as_written_and_are_not_repeated(
    capsys: pytest.CaptureFixture[str],
) -> None:
    renderer = PlainEventRenderer()
    source = "# /// script\n# dependencies = []\n# ///\nprint(2)\n"
    renderer.preview(ProviderToolCallDeltaEvent(delta='{"ops": [{"op": "add", "cell": {"sou'))
    assert capsys.readouterr().out == ""
    renderer = PlainEventRenderer()
    stream(renderer, [{"op": "add", "cell": {"kind": "note", "text": "Check."}}])
    assert capsys.readouterr().out == "• Check."
    renderer = PlainEventRenderer()
    stream(renderer, [{"op": "add", "cell": {"kind": "code", "source": source}}])
    emit(renderer, "patch_accepted", cells={"5": {"id": 5, "kind": "code", "source": source}})
    emit(renderer, "execution_output", output_chunks={"5": execution("2\n")})
    assert capsys.readouterr().out == (
        "• # /// script\n  # dependencies = []\n  # ///\n  print(2)\n  └ 2\n"
    )


def test_accepted_cells_that_differ_from_the_draft_are_shown(
    capsys: pytest.CaptureFixture[str],
) -> None:
    renderer = PlainEventRenderer()
    stream(renderer, [{"op": "add", "cell": {"kind": "note", "text": "draft"}}])
    emit(
        renderer,
        "patch_accepted",
        cells={
            "1": {"id": 1, "kind": "note", "text": "final"},
            "2": {"id": 2, "kind": "cmd", "argv": ["true"]},
        },
    )
    assert capsys.readouterr().out == "• draft\n\n• final\n\n• true\n"


def test_rejected_draft_is_followed_by_the_retry_reason(
    capsys: pytest.CaptureFixture[str],
) -> None:
    renderer = PlainEventRenderer()
    stream(renderer, [{"op": "add", "cell": {"kind": "code", "source": "print(1)"}}])
    emit(renderer, "patch_rejected", turns={"1": {"error": "Code requires PEP 723"}})
    stream(renderer, [{"op": "add", "cell": {"kind": "code", "source": "print(1)"}}])
    emit(renderer, "patch_accepted", cells={"2": {"id": 2, "kind": "code", "source": "print(1)"}})
    assert capsys.readouterr().out == (
        "• print(1)\n\n• Retry: Code requires PEP 723\n\n• print(1)\n"
    )


def test_output_interrupting_a_draft_resumes_it_on_a_new_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    renderer = PlainEventRenderer()
    renderer.preview(ProviderToolCallDeltaEvent(delta='{"ops":[{"cell":{"text":"one'))
    emit(renderer, "execution_output", output_chunks={"2": execution("out\n")})
    renderer.preview(ProviderToolCallDeltaEvent(delta='\\ntwo"}}]}'))
    assert capsys.readouterr().out == "• one\n  └ out\n  \n  two"
