"""Regression tests for top-level CLI command wiring."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from rio.cli import app
from rio.coding.paths import RioPaths
from rio.coding.session_manager import SessionManager

run_module = importlib.import_module("rio.cli.run")


def test_thinking_flag_reaches_thinking_level_param(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_run_persistent_session(
        prompt: str,
        cwd: Path,
        provider_name: str | None = None,
        model: str | None = None,
        thinking_level: object | None = None,
        extension_paths: tuple[Path, ...] = (),
        trust_override: object | None = None,
        resume: str | None = None,
        output_mode: object | None = None,
    ) -> tuple[bool, str]:
        captured["thinking_level"] = thinking_level
        captured["extension_paths"] = extension_paths
        captured["trust_override"] = trust_override
        captured["output_mode"] = output_mode
        return True, "session-id"

    monkeypatch.setattr(run_module, "run_persistent_session", fake_run_persistent_session)

    app(["run", "--thinking", "high", "--approve", "do it"])
    assert captured["thinking_level"] == "high"
    assert captured["extension_paths"] == ()
    assert captured["trust_override"] == "approve"
    assert captured["output_mode"] == "human"


def test_json_output_reaches_session_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_run(*args: object) -> tuple[bool, str]:
        captured["output_mode"] = args[-1]
        return True, "session-id"

    monkeypatch.setattr(run_module, "run_persistent_session", fake_run)

    app(["run", "--output", "json", "do it"])

    assert captured["output_mode"] == "json"


def test_help_lists_commands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        app(["--help"])

    assert error.value.code == 0
    assert "commands:" in capsys.readouterr().out


def test_version_prints_installed_version(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli_module = importlib.import_module("rio.cli")
    monkeypatch.setattr(cli_module, "current_version", lambda: "1.2.3")

    app(["version"])

    assert capsys.readouterr().out == "1.2.3\n"


def test_coding_version_module_is_deprecated() -> None:
    import sys

    sys.modules.pop("rio.coding.version", None)
    with pytest.deprecated_call(match="rio.coding.version is deprecated"):
        importlib.import_module("rio.coding.version")


def test_unrelated_value_error_does_not_blame_cwd(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fake_run_persistent_session(
        prompt: str,
        cwd: Path,
        provider_name: str | None = None,
        model: str | None = None,
        thinking_level: object | None = None,
        extension_paths: tuple[Path, ...] = (),
        trust_override: object | None = None,
        resume: str | None = None,
        output_mode: object | None = None,
    ) -> tuple[bool, str]:
        raise ValueError("Unknown provider: bonsai2")

    monkeypatch.setattr(run_module, "run_persistent_session", fake_run_persistent_session)

    with pytest.raises(SystemExit) as error:
        app(["run", "--provider", "bonsai2", "do it"])

    assert error.value.code == 2
    stderr = capsys.readouterr().err
    assert "Unknown provider: bonsai2" in stderr
    assert "error: --cwd" not in stderr


@pytest.mark.anyio
async def test_resume_reuses_the_durable_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sessions: list[object] = []

    async def fake_run(
        *args: object, storage: object = None, **kwargs: object
    ) -> tuple[bool, str, str]:
        sessions.append(storage)
        return True, "provider", "model"

    monkeypatch.setattr(run_module, "_run_configured_session", fake_run)
    manager = SessionManager(RioPaths(home=tmp_path / ".rio", agents_home=tmp_path / ".agents"))
    project = tmp_path / "project"
    project.mkdir()

    _, session_id = await run_module.run_persistent_session(
        "first task", project, session_manager=manager
    )
    _, resumed_id = await run_module.run_persistent_session(
        "next task", project, resume=session_id, session_manager=manager
    )

    assert resumed_id == session_id
    assert sessions[0].path == sessions[1].path
