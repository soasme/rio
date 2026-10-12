"""Authentication persistence and cross-process refresh regressions."""

import json
import subprocess
import sys

import httpx
import pytest

from rio.coding.auth_commands import login_provider
from rio.coding.credentials import FileCredentialStore, OAuthCredential
from rio.coding.oauth import (
    OAuthError,
    _post_openai_codex_token,
    _required_token_field,
    _token_expiry,
)


@pytest.mark.parametrize("existing", [False, True])
async def test_login_only_changes_default_provider(tmp_path, monkeypatch, existing):
    monkeypatch.setenv("HOME", str(tmp_path))
    home = tmp_path / ".rio"
    home.mkdir()
    catalog = home / "catalog.toml"
    original = """schema_version = 1
[[providers]]
name = "local"
base_url = "http://localhost:8080/v1"
models = ["fresh-model"]
[providers.headers]
X-Catalog = "current"
"""
    catalog.write_text(original)
    settings = home / "providers.json"
    raw = {"default_provider": "local"}
    if existing:
        raw.update(schema_version=2, provider_preferences={"local": {"timeout_seconds": 42}})
        settings.write_text(json.dumps(raw))
    monkeypatch.setattr("rio.coding.auth_commands.getpass", lambda _: "secret-key")
    await login_provider("local", method="api-key")
    assert json.loads(settings.read_text()) == raw
    assert catalog.read_text() == original
    assert FileCredentialStore().get("local") == "secret-key"


async def test_login_does_not_pin_builtin_catalog_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("rio.coding.auth_commands.getpass", lambda _: "secret-key")
    await login_provider("openai", method="api-key")
    home = tmp_path / ".rio"
    assert json.loads((home / "providers.json").read_text()) == {"default_provider": "openai"}
    assert not (home / "catalog.toml").exists()
    assert home.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize(
    "body",
    [
        {
            "error": "invalid_grant",
            "error_description": "rejected rotating-refresh-secret",
            "access_token": "new-access-secret",
            "refresh_token": "new-refresh-secret",
        },
        "raw rotating-refresh-secret access-secret",
    ],
)
async def test_codex_http_errors_redact_response_and_request_tokens(body):
    def handle(request):
        return (
            httpx.Response(400, json=body)
            if isinstance(body, dict)
            else httpx.Response(400, text=body)
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(OAuthError) as error:
            await _post_openai_codex_token(
                {"refresh_token": "rotating-refresh-secret"}, client=client, action="refresh"
            )
    assert "secret" not in str(error.value)
    if isinstance(body, dict):
        assert "invalid_grant" in str(error.value)


@pytest.mark.parametrize("field", ["access_token", "refresh_token"])
def test_missing_token_errors_do_not_include_other_tokens(field):
    raw = {"access_token": "access-secret", "refresh_token": "refresh-secret"}
    raw.pop(field)
    with pytest.raises(OAuthError) as error:
        _required_token_field(raw, field, action="exchange")
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("expiry", [None, "bad"])
def test_expiry_errors_do_not_include_tokens(expiry):
    raw = {"access_token": "access-secret", "refresh_token": "refresh-secret"}
    if expiry is not None:
        raw["expires_in"] = expiry
    with pytest.raises(OAuthError) as error:
        _token_expiry(raw, "access-secret", action="exchange")
    assert "secret" not in str(error.value)


def test_two_processes_spend_rotating_refresh_token_once(tmp_path):
    store = FileCredentialStore(tmp_path / "credentials.json")
    store.set_oauth(
        "openai-codex",
        OAuthCredential(
            access="old-access", refresh="old-refresh", expires=1, account_id="account"
        ),
    )
    script = r"""
import asyncio, os, sys
from pathlib import Path
from rio.coding.credentials import FileCredentialStore, OAuthCredential
from rio.coding.provider_config import provider_config_from_catalog_entry
from rio.coding import oauth, provider_runtime as runtime
root = Path(sys.argv[1])
store = FileCredentialStore(root / "credentials.json")
async def refresh(token):
    with (root / "spent").open("a") as log:
        log.write(token + "\n")
    await asyncio.sleep(0.2)
    return OAuthCredential(access="new-access", refresh="new-refresh", expires=9999999999999,
                           account_id="account")
oauth.refresh_openai_codex_token = refresh
async def main():
    (root / (str(os.getpid()) + ".ready")).touch()
    async with asyncio.timeout(10):
        while len(list(root.glob("*.ready"))) < 2:
            await asyncio.sleep(0.01)
        resolve = runtime.OpenAICodexCredentialResolver(
            provider_config_from_catalog_entry("openai-codex"), credential_store=store)
        credential = await resolve()
        assert credential.access_token == "new-access"
asyncio.run(main())
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(tmp_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    try:
        for process in processes:
            out, err = process.communicate(timeout=15)
            assert process.returncode == 0, out + err
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
    assert (tmp_path / "spent").read_text() == "old-refresh\n"
    assert store.get_oauth("openai-codex").refresh == "new-refresh"


def test_parallel_credential_writes_keep_all_providers(tmp_path):
    path = tmp_path / "credentials.json"
    script = """
import sys, time
from pathlib import Path
from rio.coding.credentials import FileCredentialStore
store = FileCredentialStore(Path(sys.argv[1]))
load = store._load
def slow_load():
    result = load()
    time.sleep(0.1)
    return result
store._load = slow_load
store.set(sys.argv[2], "secret")
"""
    processes = [
        subprocess.Popen([sys.executable, "-c", script, str(path), name]) for name in ("one", "two")
    ]
    try:
        for process in processes:
            assert process.wait(timeout=10) == 0
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
    assert FileCredentialStore(path).names() == ("one", "two")
    assert path.stat().st_mode & 0o777 == 0o600
