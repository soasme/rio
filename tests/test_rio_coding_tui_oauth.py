"""Exercise browser OAuth through the terminal frontend and a real loopback callback."""

import asyncio
import base64
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from textual.widgets import Input

from rio_ai import FakeProvider
from rio_coding import oauth
from rio_coding.credentials import FileCredentialStore
from rio_coding.frontend_session import ConfiguredSession
from rio_coding.provider_config import load_provider_settings
from rio_coding.session import CodingSession, CodingSessionConfig
from rio_coding.session_store import InMemorySessionStorage
from rio_tui import RioTuiApp, Settings
from rio_tui.bridge import Question
from rio_tui.widgets.conversation import Notice


@pytest.mark.parametrize(
    "completion", ["callback", "paste", "no_server", "cancel", "bad_state", "exchange_error"]
)
async def test_tui_browser_login_completes_or_cancels(monkeypatch, tmp_path, completion):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setenv("RIO_OAUTH_CALLBACK_HOST", "127.0.0.1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    servers = []
    urls = []
    exchanged = []
    original_start = oauth._start_local_oauth_server

    async def start(state):
        if completion == "no_server":
            return None
        server = await original_start(state, callback_port=0)
        assert server is not None
        servers.append(server)
        return server

    async def exchange(code, verifier, **kwargs):
        if completion == "exchange_error":
            raise oauth.OAuthError("Token exchange failed")
        assert code == "test-authorization-code"
        assert verifier
        exchanged.append(code)
        payload = {oauth.OPENAI_CODEX_ACCOUNT_CLAIM: {"chatgpt_account_id": "test-account"}}
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        return oauth.TokenResponse(
            access=f"header.{encoded}.signature", refresh="test-refresh", expires=9999999999999
        )

    monkeypatch.setattr(oauth, "_start_local_oauth_server", start)
    monkeypatch.setattr(oauth, "exchange_openai_codex_authorization_code", exchange)
    monkeypatch.setattr(oauth.webbrowser, "open", lambda url: urls.append(url))
    settings = load_provider_settings()
    provider = settings.get_provider("openai")
    session = ConfiguredSession(
        await CodingSession.load(
            CodingSessionConfig(
                provider=FakeProvider([]),
                provider_name="openai",
                model=provider.default_model,
                cwd=tmp_path,
                storage=InMemorySessionStorage(),
                load_extensions=False,
                project_resources_trusted=False,
            )
        )
    )
    app = RioTuiApp(session, settings=Settings())
    try:
        async with app.run_test() as pilot:
            app.workspace.submit("/login openai-codex")
            await pilot.pause()
            assert app.screen.query(Question)
            login_screen = app.screen
            assert len(urls) == 1
            state = parse_qs(urlparse(urls[0]).query)["state"][0]
            if completion in {"callback", "exchange_error"}:
                host, port = servers[0]._server.server_address
                async with httpx.AsyncClient(trust_env=False) as client:
                    response = await client.get(
                        f"http://{host}:{port}/auth/callback",
                        params={"state": state, "code": "test-authorization-code"},
                    )
                assert response.status_code == 200
            elif completion == "cancel":
                await pilot.press("escape")
            else:
                if completion == "bad_state":
                    state = "wrong-state"
                login_screen.query_one(
                    Input
                ).value = f"http://localhost:1455/auth/callback?code=test-authorization-code&state={state}"
                await pilot.press("enter")
            await asyncio.wait_for(
                asyncio.gather(*(w.wait() for w in app.workers if w.node is app.workspace)),
                timeout=5,
            )
            await pilot.pause()
            assert not app.screen.query(Question)
            assert not app.workspace.busy
            assert all(not server._thread.is_alive() for server in servers)
            output = " ".join(item.text for item in app.screen.query(Notice))
            credential = FileCredentialStore().get_oauth("openai-codex")
            if completion == "cancel":
                assert credential is None
                assert exchanged == []
                assert "Sign-in cancelled" in output
            elif completion in {"bad_state", "exchange_error"}:
                assert credential is None
                assert session.provider_name == "openai"
                expected = (
                    "OAuth state mismatch" if completion == "bad_state" else "Token exchange failed"
                )
                assert expected in output
            else:
                assert credential.account_id == "test-account"
                assert session.provider_name == "openai-codex"
                assert load_provider_settings().default_provider == "openai-codex"
                assert "Logged in to openai-codex" in output
    finally:
        await session.aclose()
