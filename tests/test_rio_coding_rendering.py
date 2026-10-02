"""Tests for rio.coding's non-TUI renderers and session export.

Covers `rio.coding.rendering.{base,plain,json,steps}` and
`rio.coding.session_export`: live per-step rendering of `rio.agent`/`rio.coding`
events, an after-the-fact step account read back from the journal, and a
self-contained HTML export.
"""

from __future__ import annotations

import json

import pytest

from conftest import add_code
from rio.agent import (
    ExecutionEvent,
    PatchEvent,
    ReasoningEvent,
    StepStartEvent,
    ValidationErrorEvent,
    apply_patch,
    new_notebook,
)
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


def _ran(source: str, text: str) -> ExecutionEvent:
    notebook = apply_patch(new_notebook(), [add_code(source)])
    notebook["cells"][0]["outputs"] = [{"output_type": "stream", "name": "stdout", "text": text}]
    return ExecutionEvent(step=1, cells=[0], notebook=notebook)


def test_plain_renderer_shows_each_cell_run_and_its_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    renderer = PlainEventRenderer()

    renderer.render(StepStartEvent(step=1, notebook=new_notebook()))
    renderer.render(PatchEvent(step=1, patch=[add_code("!ls")], cells=[0]))
    renderer.render(_ran("!ls", "file1\nfile2\n"))

    out = capsys.readouterr().out
    assert "[0] !ls" in out
    assert "  └ file1\n    file2" in out
    assert "step 1" not in out
    assert renderer.finish() is True


def test_plain_renderer_reports_patches_that_run_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    renderer = PlainEventRenderer()

    renderer.render(PatchEvent(step=1, patch=[{"op": "remove", "path": "/cells/0"}], cells=[]))

    assert "Notebook edited (1 operations)" in capsys.readouterr().out


def test_plain_renderer_shows_errors_by_name(capsys: pytest.CaptureFixture[str]) -> None:
    renderer = PlainEventRenderer()
    event = _ran("1/0", "")
    event.notebook["cells"][0]["outputs"] = [
        {"output_type": "error", "ename": "ZeroDivisionError", "evalue": "division by zero"}
    ]

    renderer.render(event)

    assert "ZeroDivisionError: division by zero" in capsys.readouterr().out


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

    renderer.render(StepStartEvent(step=1, notebook=new_notebook()))
    renderer.render(_ran("print('ok')", "ok\n"))
    renderer.render(SessionRunEndEvent(steps=1, answer="done"))

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines[0] == {"type": "step_start", "step": 1, "notebook": new_notebook()}
    assert lines[1]["type"] == "execution"
    assert lines[1]["cells"] == [0]
    assert lines[1]["notebook"]["cells"][0]["outputs"][0]["text"] == "ok\n"
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
    renderer.render(_ran("!ls", "a\n"))

    out = capsys.readouterr().out
    assert f"{GREEN}●{RESET}" in out
    assert "[0] !ls" in out


def test_plain_renderer_uses_plain_dots_when_no_color(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")

    renderer = PlainEventRenderer()
    renderer.render(_ran("!ls", "a\n"))

    out = capsys.readouterr().out
    assert "· [0] !ls" in out
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
    cell = {"cell_type": "code", "id": "c1", "metadata": {}, "source": "!ls"}
    cell |= {"execution_count": 1, "outputs": []}
    return [
        TurnEntry(id="t1", observation="Fix the bug in foo.py"),
        StepEntry(
            id="s1",
            parent_id="t1",
            step=1,
            patch=[{"op": "add", "path": "/cells/-", "value": cell}],
            cells=[0],
        ),
        ValidationFailureEntry(id="v1", parent_id="s1", step=2, attempt=1, error="bad patch"),
        StepEntry(id="s2", parent_id="v1", step=2, reply="answer: done"),
    ]


def test_render_run_steps_produces_step_account() -> None:
    text = render_run_steps(_sample_entries())

    assert "step 1: 1 patch operation(s), ran cells [0]" in text
    assert "step 2: 0 patch operation(s)" in text
    assert "retry 1: bad patch" in text
    assert "reply: answer: done" in text


def test_render_completed_run_includes_final_notebook() -> None:
    text = render_completed_run(_sample_entries())

    assert "Final notebook:" in text
    assert '"source": "!ls"' in text


# -- session_export ---------------------------------------------------------


def test_render_session_html_escapes_malicious_output() -> None:
    entries = [StepEntry(id="s1", step=1, reply="<script>alert(1)</script>")]

    html_out = render_session_html(entries, title="Escape test")

    assert "<script>alert(1)</script>" not in html_out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_out


def test_render_session_html_includes_steps_table_and_footprint() -> None:
    html_out = render_session_html(
        _sample_entries(), title="Steps test", instructions="You are a coding skill."
    )

    assert '<table class="steps">' in html_out
    assert "!ls" in html_out
    assert '<table class="usage">' in html_out
    assert "<title>Steps test</title>" in html_out


def test_render_session_html_attaches_reasoning_to_its_step_collapsed() -> None:
    entries = [
        ReasoningEntry(id="r1", step=1, reasoning="I will list the files first.", truncated=True),
        StepEntry(id="s1", parent_id="r1", step=1),
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

    assert "retry 1: bad patch" in html_out
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
