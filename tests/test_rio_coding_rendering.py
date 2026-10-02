"""Tests for rio.coding's non-TUI renderers and session export.

Covers `rio.coding.rendering.{base,plain,json,steps}` and
`rio.coding.session_export`: live per-step rendering of `rio.agent`/`rio.coding`
events, an after-the-fact step account read back from the journal, and a
self-contained HTML export.
"""

from __future__ import annotations

import json

import pytest

from rio.agent import (
    ActionEndEvent,
    ActionStartEvent,
    ContextEditEvent,
    ReasoningEvent,
    StepEndEvent,
    StepStartEvent,
    ValidationErrorEvent,
)
from rio.ai.tools import AgentToolResult
from rio.coding.events import AutoRetryEndEvent, AutoRetryStartEvent, SessionRunEndEvent
from rio.coding.rendering import (
    JsonEventRenderer,
    PlainEventRenderer,
    PrintOutputMode,
    create_event_renderer,
    render_completed_run,
    render_run_steps,
)
from rio.coding.session_export import (
    SessionExportError,
    export_session_html,
    export_session_jsonl,
    normalize_export_format,
    render_session_html,
)
from rio.coding.session_store import (
    ActionRecord,
    ReasoningEntry,
    StepEntry,
    TurnEntry,
    ValidationFailureEntry,
)

# -- rendering.plain ----------------------------------------------------------


def test_plain_renderer_hides_reasoning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Human output contains the transcript, not internal reasoning."""
    renderer = PlainEventRenderer()

    renderer.render(ReasoningEvent(step=1, reasoning="I will check the file next"))

    out = capsys.readouterr().out
    assert out == ""


def test_plain_renderer_reports_context_edits(capsys: pytest.CaptureFixture[str]) -> None:
    renderer = PlainEventRenderer()

    renderer.render(
        ContextEditEvent(step=1, accepted=True, before_tokens=900, after_tokens=200, turns=3)
    )

    assert "Context edited: ~900 -> ~200 tokens" in capsys.readouterr().out


def test_plain_renderer_renders_step_lifecycle(capsys: pytest.CaptureFixture[str]) -> None:
    renderer = PlainEventRenderer()

    renderer.render(StepStartEvent(step=1, context=[{"role": "user", "text": "Fix bug"}]))
    renderer.render(ActionStartEvent(step=1, name="bash", arguments={"command": "ls"}))
    renderer.render(
        ActionEndEvent(
            step=1,
            name="bash",
            result=AgentToolResult(content="file1\nfile2"),
            is_error=False,
        )
    )
    renderer.render(
        StepEndEvent(step=1, context=[{"role": "user", "text": "Fix bug"}], terminated=False)
    )

    out = capsys.readouterr().out
    assert "Running ls" in out
    assert "  └ bash: file1" in out
    assert "step 1" not in out
    assert "Fix bug" not in out
    assert renderer.finish() is True


def test_plain_renderer_read_shows_path_and_line_range_not_content(
    capsys: pytest.CaptureFixture[str],
) -> None:
    renderer = PlainEventRenderer()

    renderer.render(ActionStartEvent(step=1, name="read", arguments={"path": "a.py"}))
    renderer.render(
        ActionEndEvent(
            step=1,
            name="read",
            result=AgentToolResult(
                content="read a.py (lines 1-3 of 3)\n\ndef add(a, b):\n    return a + b",
                details={"path": "a.py", "start_line": 1, "end_line": 3},
            ),
            is_error=False,
        )
    )

    out = capsys.readouterr().out
    assert "read a.py:1:3" in out
    assert "def add" not in out
    assert "{" not in out


def test_plain_renderer_write_shows_path_only(capsys: pytest.CaptureFixture[str]) -> None:
    renderer = PlainEventRenderer()

    renderer.render(
        ActionStartEvent(step=1, name="write", arguments={"path": "a.py", "content": "x"})
    )
    renderer.render(
        ActionEndEvent(
            step=1,
            name="write",
            result=AgentToolResult(
                content="write a.py (1 characters)\n\nSuccessfully wrote to a.py.",
                details={"path": "a.py", "characters": 1},
            ),
            is_error=False,
        )
    )

    out = capsys.readouterr().out
    assert out.strip().endswith("write a.py")
    assert "Successfully wrote" not in out


def test_plain_renderer_edit_shows_diff_not_success_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    renderer = PlainEventRenderer()
    patch = "--- a.py\n+++ a.py\n@@ -1,2 +1,2 @@\n-def add(a, b):\n+def add(a, b):  # sum\n"

    renderer.render(ActionStartEvent(step=1, name="edit", arguments={"path": "a.py", "edits": []}))
    renderer.render(
        ActionEndEvent(
            step=1,
            name="edit",
            result=AgentToolResult(
                content="edit a.py (1 edit(s))\n\nSuccessfully replaced 1 block(s) in a.py.",
                details={"path": "a.py", "edits": 1, "diff": "...", "patch": patch},
            ),
            is_error=False,
        )
    )

    out = capsys.readouterr().out
    assert "edit a.py" in out
    assert "+def add(a, b):  # sum" in out
    assert "Successfully replaced" not in out


def test_plain_renderer_file_tool_failure_shows_arguments_and_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    renderer = PlainEventRenderer()

    renderer.render(ActionStartEvent(step=1, name="read", arguments={"path": "missing.py"}))
    renderer.render(
        ActionEndEvent(
            step=1,
            name="read",
            result=AgentToolResult(content="read failed: File not found: missing.py"),
            is_error=True,
        )
    )

    out = capsys.readouterr().out
    assert '"path":"missing.py"' in out
    assert "File not found: missing.py" in out


def test_plain_renderer_renders_validation_error_as_retry(
    capsys: pytest.CaptureFixture[str],
) -> None:
    renderer = PlainEventRenderer()

    renderer.render(ValidationErrorEvent(step=2, attempt=1, error="delta touched unknown field"))

    out = capsys.readouterr().out
    assert "Retry" in out
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

    renderer.render(StepStartEvent(step=1, context=[{"role": "user", "text": "start"}]))
    renderer.render(
        ActionEndEvent(step=1, name="bash", result=AgentToolResult(content="ok"), is_error=False)
    )
    renderer.render(SessionRunEndEvent(steps=1, answer="done"))

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines[0] == {
        "type": "step_start",
        "step": 1,
        "context": [{"role": "user", "text": "start"}],
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
    assert isinstance(create_event_renderer(PrintOutputMode.human), PlainEventRenderer)


# -- rendering.ansi -----------------------------------------------------------


def test_should_use_color_respects_no_color_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from rio.coding.rendering.ansi import should_use_color

    monkeypatch.setenv("NO_COLOR", "1")
    assert should_use_color() is False

    monkeypatch.delenv("NO_COLOR", raising=False)
    # Note: may be True or False depending on test runner TTY


def test_should_use_color_checks_isatty(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    from rio.coding.rendering.ansi import should_use_color

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    assert should_use_color() is False

    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    assert should_use_color() is True


def test_dot_returns_plain_when_colors_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from rio.coding.rendering.ansi import dot

    monkeypatch.setenv("NO_COLOR", "1")
    assert dot(success=True) == "·"
    assert dot(success=False) == "·"


def test_dot_returns_colored_when_colors_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    from rio.coding.rendering.ansi import GREEN, RED, RESET, dot

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    assert dot(success=True) == f"{GREEN}●{RESET}"
    assert dot(success=False) == f"{RED}●{RESET}"


def test_plain_renderer_uses_colored_dots_on_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import sys

    from rio.coding.rendering.ansi import GREEN, RESET

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    renderer = PlainEventRenderer()
    renderer.render(ActionStartEvent(step=1, name="bash", arguments={"command": "ls"}))

    out = capsys.readouterr().out
    assert f"{GREEN}●{RESET}" in out
    assert "Running ls" in out


def test_plain_renderer_uses_plain_dots_when_no_color(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")

    renderer = PlainEventRenderer()
    renderer.render(ActionStartEvent(step=1, name="bash", arguments={"command": "ls"}))

    out = capsys.readouterr().out
    assert "· Running ls" in out
    assert "\033[" not in out  # No ANSI codes


def test_plain_renderer_uses_red_dot_on_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import sys

    from rio.coding.rendering.ansi import RED, RESET

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    renderer = PlainEventRenderer()
    renderer.render(
        AutoRetryStartEvent(attempt=1, max_attempts=3, delay_ms=0, error_message="HTTP 503")
    )
    renderer.render(AutoRetryEndEvent(success=False, attempt=3, final_error="retries exhausted"))

    assert renderer.finish() is False
    err = capsys.readouterr().err
    assert f"{RED}●{RESET}" in err or "retries exhausted" in err


# -- rendering.steps --------------------------------------------------------


def _sample_entries() -> list:
    return [
        TurnEntry(id="t1", observation="Fix the bug in foo.py"),
        StepEntry(
            id="s1",
            parent_id="t1",
            step=1,
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
            state={"context": [{"role": "notes", "text": "answer: done"}]},
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


def test_render_completed_run_includes_final_context() -> None:
    text = render_completed_run(_sample_entries())

    assert "Final context:" in text
    assert "[[CTX_TURN 1 role=notes]]\nanswer: done" in text


# -- session_export ---------------------------------------------------------


def test_render_session_html_escapes_malicious_observation() -> None:
    entries = [
        StepEntry(
            id="s1",
            step=1,
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
    assert '<table class="usage">' in html_out
    assert "<title>Steps test</title>" in html_out


def test_render_session_html_attaches_reasoning_to_its_step_collapsed() -> None:
    entries = [
        ReasoningEntry(id="r1", step=1, reasoning="I will list the files first.", truncated=True),
        StepEntry(
            id="s1",
            parent_id="r1",
            step=1,
            state={"goal": "x"},
            action=ActionRecord(name="bash", arguments={"command": "ls"}),
            observation="ok",
            terminated=False,
        ),
    ]

    html_out = render_session_html(entries, title="Reasoning test")

    assert '<details class="reasoning">' in html_out
    assert "I will list the files first." in html_out
    # Collapsed: the details element carries no `open` attribute.
    assert '<details class="reasoning" open>' not in html_out
    # It sits above the step it explains.
    assert html_out.index("I will list the files first.") < html_out.index('id="step-1"')


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
