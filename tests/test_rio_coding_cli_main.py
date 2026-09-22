"""Regression test for `rio` CLI argument wiring into run_configured_session."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rio.coding import cli as cli_module


def test_main_call_args_bind_to_expected_params() -> None:
    """`main` passes args to `run_configured_session` positionally: guard the order."""
    args = ("prompt text", Path.cwd(), "provider", "model", "high", (), "approve")
    bound = inspect.signature(cli_module.run_configured_session).bind(*args)
    assert bound.arguments["thinking_level"] == "high"
    assert bound.arguments["extension_paths"] == ()
    assert bound.arguments["trust_override"] == "approve"


def test_thinking_flag_reaches_thinking_level_param(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_run_configured_session(
        prompt: str,
        cwd: Path,
        provider_name: str | None = None,
        model: str | None = None,
        thinking_level: object | None = None,
        extension_paths: tuple[Path, ...] = (),
        trust_override: object | None = None,
    ) -> bool:
        captured["thinking_level"] = thinking_level
        captured["extension_paths"] = extension_paths
        captured["trust_override"] = trust_override
        return True

    monkeypatch.setattr(cli_module, "run_configured_session", fake_run_configured_session)

    result = CliRunner().invoke(cli_module.app, ["--thinking", "high", "--approve", "do it"])

    assert result.exit_code == 0, result.output
    assert captured["thinking_level"] == "high"
    assert captured["extension_paths"] == ()
    assert captured["trust_override"] == "approve"
