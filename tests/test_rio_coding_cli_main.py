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
        extension_paths: tuple[Path, ...] = (),
        trust_override: object | None = None,
        resume: str | None = None,
        output_mode: object | None = None,
        agents_md: Path | None = None,
        cell_memory_bytes: int | None = None,
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

    async def fake_run(*args: object, **kwargs: object) -> tuple[bool, str]:
        captured["output_mode"] = args[-1]
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


def test_cell_memory_flag_reaches_session_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_run(*args: object, **kwargs: object) -> tuple[bool, str]:
        captured.update(kwargs)
        return True, "session-id"

    monkeypatch.setattr(run_module, "run_persistent_session", fake_run)

    app(["run", "--cell-memory", "512M", "do it"])
    assert captured["cell_memory_bytes"] == 512 * 2**20
    app(["run", "do it"])
    assert captured["cell_memory_bytes"] is None


@pytest.mark.parametrize("value", ["0", "1.5G", "G", "-1M"])
def test_cell_memory_rejects_invalid_sizes(value: str) -> None:
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(["run", "--cell-memory", value, "do it"])
    assert error.value.code == 2


def test_load_agents_md_resolves_cwd_or_explicit_path(tmp_path: Path) -> None:
    from rio.coding import load_agents_md

    assert load_agents_md(tmp_path) is None
    (tmp_path / "AGENTS.md").write_text("cwd rules")
    assert load_agents_md(tmp_path).content == "cwd rules"
    explicit = tmp_path / "rules.md"
    explicit.write_text("explicit rules")
    loaded = load_agents_md(tmp_path, explicit)
    assert (loaded.path, loaded.content) == (str(explicit.resolve()), "explicit rules")
    with pytest.raises(ValueError, match="Cannot read AGENTS.md"):
        load_agents_md(tmp_path, tmp_path / "missing.md")


def test_tools_flag_is_gone() -> None:
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(["run", "--tools=read", "do it"])
    assert error.value.code == 2


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
        agents_md: Path | None = None,
        cell_memory_bytes: int | None = None,
    ) -> tuple[bool, str]:
        raise ValueError("Unknown provider: bonsai2")

    monkeypatch.setattr(run_module, "run_persistent_session", fake_run_persistent_session)

    with pytest.raises(SystemExit) as error:
        app(["run", "--provider", "bonsai2", "do it"])

    assert error.value.code == 2
    stderr = capsys.readouterr().err
    assert "Unknown provider: bonsai2" in stderr
    assert "error: --cwd" not in stderr
