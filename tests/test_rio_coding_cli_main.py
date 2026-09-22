"""Regression tests for top-level CLI command wiring."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from typer.testing import CliRunner

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
    ) -> tuple[bool, str]:
        captured["thinking_level"] = thinking_level
        captured["extension_paths"] = extension_paths
        captured["trust_override"] = trust_override
        return True, "session-id"

    monkeypatch.setattr(run_module, "run_persistent_session", fake_run_persistent_session)

    result = CliRunner().invoke(app, ["run", "--thinking", "high", "--approve", "do it"])

    assert result.exit_code == 0, result.output
    assert captured["thinking_level"] == "high"
    assert captured["extension_paths"] == ()
    assert captured["trust_override"] == "approve"


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
