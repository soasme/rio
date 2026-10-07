"""Human transcript regressions for the durable event stream."""

import pytest

from rio.coding.rendering.steps import PlainEventRenderer


def emit(renderer, kind, **changes):
    renderer.render({"type": kind, "changes": changes})


def execution(output="", stderr="", **extra):
    return {"cell_id": 2, "output": output, "stderr": stderr, **extra}


def test_committed_cells_show_literal_source_notes_and_successors(capsys):
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


def test_streamed_snapshots_preserve_partial_lines_and_do_not_repeat(capsys):
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


def test_interleaved_notes_and_stderr_keep_separate_entries(capsys):
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
def test_silent_executions_still_show_outcome(result, expected, capsys):
    emit(PlainEventRenderer(), "execution_finished", executions={"2": execution(result=result)})
    assert capsys.readouterr().out == f"  └ {expected}\n"


@pytest.mark.parametrize("durable_truncation", [False, True])
def test_truncation_is_explicit_and_only_reported_once(durable_truncation, capsys):
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


def test_conclusion_request_does_not_claim_success_and_retries_are_visible(capsys):
    renderer = PlainEventRenderer()
    emit(
        renderer,
        "patch_accepted",
        cells={
            "3": {
                "id": 3,
                "previous_id": None,
                "kind": "note",
                "text": "Done?",
                "role": "conclusion",
                "result": "success",
            },
        },
    )
    emit(renderer, "patch_rejected", turns={"t1": {"error": "Invalid patch"}})
    assert capsys.readouterr().out == ("• Done?\n\n• Retry: Invalid patch\n")


def test_command_arguments_are_quoted_without_cell_metadata(capsys):
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
