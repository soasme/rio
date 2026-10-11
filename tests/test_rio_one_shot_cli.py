"""One-shot CLI checks."""

import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from rio.cli import _resolve_task, app
from rio.coding.paths import RioPaths


def test_cli_requires_a_task():
    with pytest.raises(SystemExit) as error:
        app(["run"])
    assert error.value.code == 2


def test_long_task_is_not_treated_as_a_file():
    task = "Fix nthroot_mod in README.md " + "x" * 300 + " README.md"
    assert _resolve_task([task]) == task


def test_markdown_task_file_is_expanded(tmp_path):
    task_file = tmp_path / "task.md"
    task_file.write_text("fix the parser", encoding="utf-8")
    assert _resolve_task([str(task_file)]) == "fix the parser"


@pytest.mark.parametrize("option", ["--extension=provider.py", "--approve", "--no-approve"])
def test_run_rejects_extension_loading_options(option):
    with pytest.raises(SystemExit) as error:
        app(["run", option, "task"])
    assert error.value.code == 2


def test_cli_does_not_import_extension_or_project_trust_runtime():
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
    tmp_path, monkeypatch, provider, model, message
):
    from rio.cli import run as cli
    from rio.coding.provider_config import (
        OpenAICompatibleProviderConfig,
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
            OpenAICompatibleProviderConfig(
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

    def handler(request: httpx.Request) -> httpx.Response | Exception:
        requests.append(request)
        if request.method == "GET":
            return httpx.ConnectError("server is down", request=request)
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
                                                                "op": "add",
                                                                "cell": {
                                                                    "kind": "note",
                                                                    "text": "hello",
                                                                    "role": "conclusion",
                                                                    "result": "success",
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

    import rio.ai.openai_compatible as compatible

    monkeypatch.setattr(compatible, "create_async_client", fake_client)
    ok, _ = await run_persistent_session(
        prompt="say hello",
        model="qwen-local",
        cwd=project,
        provider_name="local-llama",
    )
    assert ok is True
    assert "hello" in capsys.readouterr().out
    assert [request.url.path for request in requests] == ["/v1/chat/completions"]
