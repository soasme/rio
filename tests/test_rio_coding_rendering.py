"""Tests for rio_coding's non-TUI renderers and session export.

Covers `rio_coding.rendering.{base,plain,json,steps}` and
`rio_coding.session_export`. There is no conversation transcript to render
under SKILL.state, so these tests exercise the redesigned surface: live
per-step rendering of `rio_agent`/`rio_coding` events, an after-the-fact
step account read back from the journal, and a self-contained HTML export.
"""

from __future__ import annotations

import json

import pytest

from rio_agent import (
    ActionEndEvent,
    ActionStartEvent,
    ReasoningDiscardedEvent,
    StateUpdateEvent,
    StepEndEvent,
    StepStartEvent,
    ValidationErrorEvent,
)
from rio_ai.tools import AgentToolResult
from rio_coding.events import AutoRetryEndEvent, AutoRetryStartEvent, SessionRunEndEvent
from rio_coding.rendering import (
    JsonEventRenderer,
    PlainEventRenderer,
    PrintOutputMode,
    create_event_renderer,
    render_completed_run,
    render_run_steps,
)
from rio_coding.session_export import (
    SessionExportError,
    export_session_html,
    export_session_jsonl,
    normalize_export_format,
    render_session_html,
)
from rio_coding.session_store import ActionRecord, StepEntry, TurnEntry, ValidationFailureEntry

# -- rendering.plain ----------------------------------------------------------


def test_plain_renderer_marks_discarded_reasoning_as_discarded(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A reader must never mistake shown-once reasoning for session content."""
    renderer = PlainEventRenderer()

    renderer.render(ReasoningDiscardedEvent(step=1, reasoning="I will check the file next"))

    out = capsys.readouterr().out
    assert "discarded" in out
    assert "I will check the file next" in out


def test_plain_renderer_renders_step_lifecycle(capsys: pytest.CaptureFixture[str]) -> None:
    renderer = PlainEventRenderer()

    renderer.render(StepStartEvent(step=1, state={"goal": ""}, observation="start"))
    renderer.render(StateUpdateEvent(step=1, delta={"goal": "Fix bug"}, state={"goal": "Fix bug"}))
    renderer.render(ActionStartEvent(step=1, name="bash", arguments={"command": "ls"}))
    renderer.render(
        ActionEndEvent(
            step=1,
            name="bash",
            result=AgentToolResult(content="file1\nfile2"),
            is_error=False,
        )
    )
    renderer.render(StepEndEvent(step=1, state={"goal": "Fix bug"}, terminated=False))

    out = capsys.readouterr().out
    assert "step 1" in out
    assert "bash" in out
    assert "Fix bug" in out
    assert "file1" in out
    assert renderer.finish() is True


def test_plain_renderer_renders_validation_error_as_retry(
    capsys: pytest.CaptureFixture[str],
) -> None:
    renderer = PlainEventRenderer()

    renderer.render(ValidationErrorEvent(step=2, attempt=1, error="delta touched unknown field"))

    out = capsys.readouterr().out
    assert "retry" in out
    assert "delta touched unknown field" in out


def test_plain_renderer_fails_on_unrecovered_auto_retry(capsys: pytest.CaptureFixture[str]) -> None:
    renderer = PlainEventRenderer()

    renderer.render(
        AutoRetryStartEvent(attempt=1, max_attempts=3, delay_ms=0, error_message="HTTP 503")
    )
    renderer.render(AutoRetryEndEvent(success=False, attempt=3, final_error="retries exhausted"))

    assert renderer.finish() is False
    assert "retries exhausted" in capsys.readouterr().err


def test_plain_renderer_recovers_after_successful_retry() -> None:
    renderer = PlainEventRenderer()

    renderer.render(
        AutoRetryStartEvent(attempt=1, max_attempts=3, delay_ms=0, error_message="HTTP 503")
    )
    renderer.render(AutoRetryEndEvent(success=True, attempt=2))

    assert renderer.finish() is True


# -- rendering.json -------------------------------------------------------


def test_json_renderer_emits_one_json_object_per_event(capsys: pytest.CaptureFixture[str]) -> None:
    renderer = JsonEventRenderer()

    renderer.render(StepStartEvent(step=1, state={"goal": ""}, observation="start"))
    renderer.render(
        ActionEndEvent(step=1, name="bash", result=AgentToolResult(content="ok"), is_error=False)
    )
    renderer.render(SessionRunEndEvent(steps=1, state={"goal": ""}, answer="done"))

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines[0] == {
        "type": "step_start",
        "step": 1,
        "state": {"goal": ""},
        "observation": "start",
    }
    assert lines[1]["type"] == "action_end"
    assert lines[1]["is_error"] is False
    assert lines[1]["result"]["content"] == [{"type": "text", "text": "ok"}]
    assert lines[2]["type"] == "run_end"
    assert lines[2]["answer"] == "done"
    assert renderer.finish() is True


def test_json_renderer_fails_on_unrecovered_auto_retry(capsys: pytest.CaptureFixture[str]) -> None:
    renderer = JsonEventRenderer()

    renderer.render(AutoRetryEndEvent(success=False, attempt=3, final_error="exhausted"))

    capsys.readouterr()
    assert renderer.finish() is False


# -- rendering (dispatch) --------------------------------------------------


def test_create_event_renderer_dispatches_by_mode() -> None:
    assert isinstance(create_event_renderer(PrintOutputMode.json), JsonEventRenderer)
    assert isinstance(create_event_renderer(PrintOutputMode.text), PlainEventRenderer)


# -- rendering.steps --------------------------------------------------------


def _sample_entries() -> list:
    return [
        TurnEntry(id="t1", observation="Fix the bug in foo.py"),
        StepEntry(
            id="s1",
            parent_id="t1",
            step=1,
            state_delta={"goal": "Fix bug"},
            state={"goal": "Fix bug"},
            action=ActionRecord(name="bash", arguments={"command": "ls"}),
            observation="file1\nfile2",
            terminated=False,
        ),
        ValidationFailureEntry(id="v1", parent_id="s1", step=2, attempt=1, error="bad delta"),
        StepEntry(
            id="s2",
            parent_id="v1",
            step=2,
            state_delta={"answer": "done"},
            state={"goal": "Fix bug", "answer": "done"},
            action=ActionRecord(name="respond", arguments={}),
            observation="",
            terminated=True,
        ),
    ]


def test_render_run_steps_produces_step_account() -> None:
    text = render_run_steps(_sample_entries())

    assert "step 1: bash" in text
    assert "step 2: respond" in text
    assert "retry 1: bad delta" in text
    assert "(terminated)" in text


def test_render_completed_run_includes_final_state() -> None:
    text = render_completed_run(_sample_entries())

    assert "Final execution state:" in text
    assert '"answer": "done"' in text


# -- session_export ---------------------------------------------------------


def test_render_session_html_escapes_malicious_observation() -> None:
    entries = [
        StepEntry(
            id="s1",
            step=1,
            state_delta={},
            state={},
            action=ActionRecord(name="bash", arguments={"command": "echo <script>"}),
            observation="<script>alert(1)</script>",
            terminated=False,
        )
    ]

    html_out = render_session_html(entries, title="Escape test")

    assert "<script>alert(1)</script>" not in html_out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_out


def test_render_session_html_includes_steps_table_and_footprint() -> None:
    entries = [
        StepEntry(
            id="s1",
            step=1,
            state_delta={"goal": "x"},
            state={"goal": "x"},
            action=ActionRecord(name="bash", arguments={"command": "ls"}),
            observation="ok",
            terminated=False,
        )
    ]

    html_out = render_session_html(
        entries, title="Steps test", instructions="You are a coding skill."
    )

    assert '<table class="steps">' in html_out
    assert "bash" in html_out
    assert "Projected cumulative tokens" in html_out
    assert "Per-step footprint is fixed at" in html_out
    assert "<title>Steps test</title>" in html_out


def test_render_session_html_renders_retry_and_final_step() -> None:
    html_out = render_session_html(_sample_entries(), title="Retry test")

    assert "retry 1: bad delta" in html_out
    assert '<span class="badge">final</span>' in html_out


def test_export_session_html_writes_file(tmp_path) -> None:
    entries = [TurnEntry(id="t1", observation="hi")]
    output_path = tmp_path / "session.html"

    result = export_session_html(entries, output_path, title="Session")

    assert result == output_path
    assert output_path.read_text(encoding="utf-8").startswith("<!doctype html>")


def test_export_session_jsonl_writes_file(tmp_path) -> None:
    entries = [TurnEntry(id="t1", observation="hi")]
    output_path = tmp_path / "session.jsonl"

    result = export_session_jsonl(entries, output_path)

    assert result == output_path
    lines = output_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["type"] == "turn"


def test_normalize_export_format_rejects_unknown() -> None:
    with pytest.raises(SessionExportError):
        normalize_export_format("exe")
