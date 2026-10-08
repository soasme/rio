"""OSC 7501 program status reporting."""

from __future__ import annotations

import importlib
import io

import pytest

from rio.cli import app, status

run_module = importlib.import_module("rio.cli.run")


class Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def osc(state: str) -> str:
    return f"\x1b]7501;state={state}:app=rio\x1b\\"


@pytest.mark.parametrize(
    ("override", "stream", "term", "expected"),
    [
        (None, Tty(), "xterm", True),
        (None, io.StringIO(), "xterm", False),
        (None, Tty(), "dumb", False),
        ("0", Tty(), "xterm", False),
        ("1", io.StringIO(), "dumb", True),
        ("yes", io.StringIO(), "xterm", False),
    ],
)
def test_override_beats_detection(monkeypatch, override, stream, term, expected):
    monkeypatch.setenv("TERM", term)
    if override is None:
        monkeypatch.delenv("RIO_PROGRAM_STATUS", raising=False)
    else:
        monkeypatch.setenv("RIO_PROGRAM_STATUS", override)
    assert status.enabled(stream) is expected


def test_report_writes_osc_7501(monkeypatch):
    monkeypatch.setenv("RIO_PROGRAM_STATUS", "1")
    stream = io.StringIO()
    status.report("working", stream)
    assert stream.getvalue() == osc("working")


@pytest.mark.parametrize(
    ("result", "final", "code"),
    [((True, "s"), "done", None), ((False, "s"), "error", 1)],
)
def test_run_reports_working_then_outcome(monkeypatch, capsys, result, final, code):
    async def fake_run(*args, **kwargs):
        return result

    monkeypatch.setenv("RIO_PROGRAM_STATUS", "1")
    monkeypatch.setattr(run_module, "run_persistent_session", fake_run)
    if code is None:
        app(["run", "do it"])
    else:
        with pytest.raises(SystemExit):
            app(["run", "do it"])
    assert capsys.readouterr().err == osc("working") + osc(final)


@pytest.mark.parametrize(("error", "final"), [(KeyboardInterrupt, "idle"), (RuntimeError, "error")])
def test_run_reports_interrupt_and_failure(monkeypatch, capsys, error, final):
    async def fake_run(*args, **kwargs):
        raise error

    monkeypatch.setenv("RIO_PROGRAM_STATUS", "1")
    monkeypatch.setattr(run_module, "run_persistent_session", fake_run)
    with pytest.raises(error):
        app(["run", "do it"])
    assert capsys.readouterr().err == osc("working") + osc(final)


def test_json_stdout_stays_clean(monkeypatch, capsys):
    async def fake_run(*args, **kwargs):
        return True, "s"

    monkeypatch.setenv("RIO_PROGRAM_STATUS", "1")
    monkeypatch.setattr(run_module, "run_persistent_session", fake_run)
    app(["run", "--output", "json", "do it"])
    assert "\x1b]7501" not in capsys.readouterr().out
