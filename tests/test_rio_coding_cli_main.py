"""Regression tests for top-level CLI command wiring."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from rio.cli import app, build_parser

run_module = importlib.import_module("rio.cli.run")


def test_thinking_flag_reaches_thinking_level_param(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_run_persistent_session(
        prompt: str,
        cwd: Path,
        provider_name: str | None = None,
        model: str | None = None,
        thinking_level: object | None = None,
        resume: str | None = None,
        output_mode: object | None = None,
        agents_md: Path | None = None,
    ) -> tuple[bool, str]:
        captured["thinking_level"] = thinking_level
        captured["output_mode"] = output_mode
        return True, "session-id"

    monkeypatch.setattr(run_module, "run_persistent_session", fake_run_persistent_session)

    app(["run", "--thinking", "high", "do it"])
    assert captured["thinking_level"] == "high"
    assert captured["output_mode"] == "human"


def test_json_output_reaches_session_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_run(*args: object, **kwargs: object) -> tuple[bool, str]:
        captured["output_mode"] = kwargs["output_mode"]
        captured["agents_md"] = kwargs["agents_md"]
        return True, "session-id"

    monkeypatch.setattr(run_module, "run_persistent_session", fake_run)

    app(["run", "--output", "json", "do it"])

    assert captured["output_mode"] == "json"
    assert captured["agents_md"] is None


def test_agents_md_flag_reaches_session_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_run(*args: object, **kwargs: object) -> tuple[bool, str]:
        captured.update(kwargs)
        return True, "session-id"

    monkeypatch.setattr(run_module, "run_persistent_session", fake_run)

    app(["run", "--agents-md", "rules.md", "do it"])

    assert captured["agents_md"] == Path("rules.md")


def test_project_context_resolves_cwd_or_explicit_path(tmp_path: Path) -> None:
    from rio.coding.context import project_context

    assert project_context(tmp_path) == ""
    (tmp_path / "AGENTS.md").write_text("cwd rules")
    assert "cwd rules" in project_context(tmp_path)
    explicit = tmp_path / "rules.md"
    explicit.write_text("explicit rules")
    loaded = project_context(tmp_path, explicit)
    assert f'path="{explicit.resolve()}"' in loaded and "explicit rules" in loaded
    with pytest.raises(ValueError, match="Cannot read AGENTS.md"):
        project_context(tmp_path, tmp_path / "missing.md")


def test_tools_flag_is_gone() -> None:
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(["run", "--tools=read", "do it"])
    assert error.value.code == 2


def test_help_lists_commands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        app(["--help"])

    assert error.value.code == 0
    help_text = capsys.readouterr().out
    assert "commands:" in help_text
    for command in ("run", "login", "version"):
        assert command in help_text
    assert "mcp" not in help_text.lower()


def test_bare_rio_requires_a_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        app([])

    assert error.value.code == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "usage: rio" in output.err
    assert "required: COMMAND" in output.err


def test_no_color_flag_is_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        app(["run", "--no-color", "do it"])

    assert error.value.code == 2
    assert "unrecognized arguments: --no-color" in capsys.readouterr().err


@pytest.mark.parametrize("method", [None, "browser", "device_code", "api-key"])
def test_login_dispatches_provider_and_method(
    method: str | None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fake_login(name: str, *, method: str | None = None) -> str:
        assert name == "anthropic"
        assert method == expected_method
        return "Logged in to anthropic."

    expected_method = method
    monkeypatch.setattr("rio.cli.login_provider", fake_login)

    app(["login", "anthropic", *(["--method", method] if method else [])])

    assert capsys.readouterr().out == "Logged in to anthropic.\n"


def test_login_error_prints_usage(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fake_login(name: str, *, method: str | None = None) -> str:
        raise ValueError("Unknown provider: missing")

    monkeypatch.setattr("rio.cli.login_provider", fake_login)

    with pytest.raises(SystemExit) as error:
        app(["login", "missing"])

    assert error.value.code == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "usage: rio login" in output.err
    assert "Unknown provider: missing" in output.err


@pytest.mark.parametrize("command", ["add", "remove", "list", "login", "logout"])
def test_mcp_commands_are_rejected(command: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        app(["mcp", command])

    assert error.value.code == 2
    assert "invalid choice: 'mcp'" in capsys.readouterr().err


def test_version_prints_installed_version(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli_module = importlib.import_module("rio.cli")
    monkeypatch.setattr(cli_module, "current_version", lambda: "1.2.3")

    app(["version"])

    assert capsys.readouterr().out == "1.2.3\n"


def test_unrelated_value_error_does_not_blame_cwd(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fake_run_persistent_session(
        prompt: str,
        cwd: Path,
        provider_name: str | None = None,
        model: str | None = None,
        thinking_level: object | None = None,
        resume: str | None = None,
        output_mode: object | None = None,
        agents_md: Path | None = None,
    ) -> tuple[bool, str]:
        raise ValueError("Unknown provider: bonsai2")

    monkeypatch.setattr(run_module, "run_persistent_session", fake_run_persistent_session)

    with pytest.raises(SystemExit) as error:
        app(["run", "--provider", "bonsai2", "do it"])

    assert error.value.code == 1
    stderr = capsys.readouterr().err
    assert "Unknown provider: bonsai2" in stderr
    assert "error: --cwd" not in stderr
