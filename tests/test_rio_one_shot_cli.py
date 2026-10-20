"""One-shot CLI checks."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest

from rio.cli import _resolve_task, app
from rio.coding.paths import RioPaths


def test_cli_requires_a_task() -> None:
    with pytest.raises(SystemExit) as error:
        app(["run"])
    assert error.value.code == 2


def test_long_task_is_not_treated_as_a_file() -> None:
    task = "Fix nthroot_mod in README.md " + "x" * 300 + " README.md"
    assert _resolve_task([task]) == task


def test_markdown_task_file_is_expanded(tmp_path: Path) -> None:
    task_file = tmp_path / "task.md"
    task_file.write_text("fix the parser", encoding="utf-8")
    assert _resolve_task([str(task_file)]) == "fix the parser"


@pytest.mark.parametrize("option", ["--extension=provider.py", "--approve", "--no-approve"])
def test_run_rejects_extension_loading_options(option: str) -> None:
    with pytest.raises(SystemExit) as error:
        app(["run", option, "task"])
    assert error.value.code == 2


def test_cli_does_not_import_extension_or_project_trust_runtime() -> None:
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
import rio.cli
for name in sys.modules:
    assert not name.startswith((
        "rio.coding.extensions", "rio.coding.project_trust",
        "rio.coding.local_backends", "rio.coding.llama_cpp",
    )), name
""",
        ],
        check=True,
    )


@pytest.mark.parametrize(
    ("provider", "model", "message"),
    [
        ("missing", "model", "Unknown provider: missing"),
        ("configured", "missing", "Model is not configured for provider configured: missing"),
    ],
)
async def test_provider_errors_do_not_load_extensions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str, model: str, message: str
) -> None:
    from rio.cli import run as cli
    from rio.coding.provider_config import (
        ProviderConfig,
        ProviderConfigError,
        ProviderSettings,
    )

    monkeypatch.setenv("HOME", str(tmp_path))
    extension = tmp_path / ".rio" / "extensions" / "probe.py"
    extension.parent.mkdir(parents=True)
    marker = tmp_path / "extension-loaded"
    extension.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    settings = ProviderSettings(
        default_provider="configured",
        providers=(
            ProviderConfig(
                name="configured",
                base_url="http://localhost:8000/v1",
                models=("model",),
                default_model="model",
            ),
        ),
    )
    monkeypatch.setattr(cli, "load_provider_settings", lambda: settings)
    with pytest.raises(ProviderConfigError, match=message):
        await cli.run_persistent_session("task", cwd=tmp_path, provider_name=provider, model=model)
    assert not marker.exists()


@pytest.mark.anyio
async def test_run_uses_catalog_for_local_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from rio.cli.run import run_persistent_session

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    paths = RioPaths(home=tmp_path / ".rio")
    paths.home.mkdir(parents=True, exist_ok=True)
    (paths.home / "catalog.toml").write_text(
        "schema_version = 1\n"
        "[[providers]]\n"
        'name = "local-llama"\n'
        'base_url = "http://127.0.0.1:8080/v1"\n'
        'models = ["qwen-local"]\n'
    )
    project = tmp_path / "project"
    project.mkdir()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            raise httpx.ConnectError("server is down", request=request)
        return httpx.Response(
            200,
            text=(
                "data: "
                + json.dumps(
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "step-1",
                                            "type": "function",
                                            "function": {
                                                "name": "step",
                                                "arguments": json.dumps(
                                                    {
                                                        "ops": [
                                                            {
                                                                "op": "control",
                                                                "control": {
                                                                    "type": "conclusion",
                                                                    "result": "success",
                                                                    "reason": "hello",
                                                                },
                                                            }
                                                        ]
                                                    }
                                                ),
                                            },
                                        }
                                    ]
                                },
                                "finish_reason": "tool_calls",
                            }
                        ]
                    }
                )
                + "\n"
                + "\ndata: [DONE]\n\n"
            ),
            headers={"content-type": "text/event-stream"},
        )

    def fake_client(*, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)

    import rio.ai.http as http

    monkeypatch.setattr(http, "create_async_client", fake_client)
    ok, _ = await run_persistent_session(
        prompt="say hello",
        model="qwen-local",
        cwd=project,
        provider_name="local-llama",
    )
    assert ok is True
    assert "hello" in capsys.readouterr().out
    assert [request.url.path for request in requests] == ["/v1/chat/completions"]


@pytest.mark.parametrize(
    "arguments",
    [
        ["--cwd", "/rio-does-not-exist"],
        ["--provider", "rio-missing-provider"],
    ],
)
def test_invalid_new_run_does_not_create_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("RIO_PROGRAM_STATUS", "0")
    with pytest.raises(SystemExit) as error:
        app(["run", *arguments, "task"])
    assert error.value.code == 1
    stderr = capsys.readouterr().err
    assert stderr.startswith("rio: error: ")
    assert "usage:" not in stderr
    assert not list((tmp_path / ".rio" / "sessions").glob("*"))


def test_session_owner_error_is_reported_without_usage(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from rio.agent.store import StorageFailure
    from rio.cli import run as cli

    async def owned(*args: Any, **kwargs: Any) -> NoReturn:
        raise StorageFailure("Session already has an owner")

    monkeypatch.setenv("RIO_PROGRAM_STATUS", "0")
    monkeypatch.setattr(cli, "run_persistent_session", owned)
    with pytest.raises(SystemExit) as error:
        app(["run", "task"])
    assert error.value.code == 1
    assert capsys.readouterr().err == "rio: error: Session already has an owner\n"
