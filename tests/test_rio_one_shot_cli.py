"""One-shot CLI checks."""

import subprocess
import sys

import pytest

from rio.cli import _resolve_task, app


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
