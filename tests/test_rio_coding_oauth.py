"""Tests for rio_coding's credential store and OAuth provider stack.

Ported from tau's test_credentials.py, test_oauth.py, and
test_oauth_providers.py. All network calls use httpx.MockTransport fakes;
nothing hits a real endpoint.

Tests in tau's test_oauth_providers.py that exercise
`OAuthRuntimeCredentialResolver` / `provider_config_from_catalog_entry`
(from tau_coding.provider_runtime / tau_coding.provider_config) are not
ported here: those modules are a different slice of the rio_coding port and
do not exist yet.
"""

from __future__ import annotations

import asyncio
import base64
from json import dumps
from stat import S_IMODE
from time import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from rio_coding.credentials import CredentialStoreError, FileCredentialStore, OAuthCredential
from rio_coding.oauth import (
    OPENAI_CODEX_ACCOUNT_CLAIM,
    OPENAI_CODEX_CLIENT_ID,
    OAuthError,
    account_id_from_access_token,
    create_openai_codex_authorization_flow,
    parse_authorization_input,
    refresh_openai_codex_token,
)
from rio_coding.oauth_anthropic import (
    ANTHROPIC_CLIENT_ID,
    ANTHROPIC_TOKEN_URL,
    refresh_anthropic_token,
)
from rio_coding.oauth_device import DevicePollResult, poll_oauth_device_code
from rio_coding.oauth_github_copilot import (
    GITHUB_COPILOT_CLIENT_ID,
    github_copilot_base_url,
    login_github_copilot,
    normalize_github_domain,
    refresh_github_copilot_token,
)
from rio_coding.oauth_registry import get_oauth_provider, oauth_provider_ids
from rio_coding.oauth_types import (
    OAuthDeviceCodeInfo,
    OAuthLoginCallbacks,
    OAuthPrompt,
    OAuthSelectPrompt,
)


def _callbacks(
    *,
    prompt: str = "",
    device_codes: list[OAuthDeviceCodeInfo] | None = None,
) -> OAuthLoginCallbacks:
    async def on_prompt(_prompt: OAuthPrompt) -> str:
        return prompt

    async def on_select(_prompt: OAuthSelectPrompt) -> str | None:
        return None

    return OAuthLoginCallbacks(
        on_auth=lambda _info: None,
        on_device_code=lambda info: device_codes.append(info) if device_codes is not None else None,
        on_prompt=on_prompt,
        on_select=on_select,
    )


def _jwt(account_id: str, *, expires: int | None = None) -> str:
    payload = {OPENAI_CODEX_ACCOUNT_CLAIM: {"chatgpt_account_id": account_id}}
    if expires is not None:
        payload["exp"] = expires
    return ".".join(
        [
            _base64url(dumps({"alg": "none"}).encode()),
            _base64url(dumps(payload).encode()),
            "signature",
        ]
    )


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


# -- credentials.py -----------------------------------------------------


def test_file_credential_store_round_trips_and_sets_private_permissions(tmp_path) -> None:
    path = tmp_path / "credentials.json"
    store = FileCredentialStore(path)

    store.set("openai", "test-key")

    assert store.get("openai") == "test-key"
    assert S_IMODE(path.stat().st_mode) == 0o600


def test_file_credential_store_deletes_key(tmp_path) -> None:
    store = FileCredentialStore(tmp_path / "credentials.json")
    store.set("openai", "test-key")

    store.delete("openai")

    assert store.get("openai") is None


def test_file_credential_store_rejects_empty_values(tmp_path) -> None:
    store = FileCredentialStore(tmp_path / "credentials.json")

    with pytest.raises(CredentialStoreError, match="must not be empty"):
        store.set("openai", "")


def test_file_credential_store_round_trips_oauth_credentials(tmp_path) -> None:
    path = tmp_path / "credentials.json"
    store = FileCredentialStore(path)
    credential = OAuthCredential(
        access="access-token",
        refresh="refresh-token",
        expires=123456,
        account_id="account-1",
    )

    store.set_oauth("openai-codex", credential)

    assert store.get("openai-codex") is None
    assert store.get_oauth("openai-codex") == credential
    assert '"type": "oauth"' in path.read_text(encoding="utf-8")


def test_file_credential_store_round_trips_extensible_oauth_metadata(tmp_path) -> None:
    path = tmp_path / "credentials.json"
    store = FileCredentialStore(path)
    credential = OAuthCredential(
        access="copilot-access",
        refresh="github-token",
        expires=123456,
        metadata={
            "enterprise_domain": "ghe.example.com",
            "available_model_ids": ["gpt-5.4", "claude-sonnet-4.6"],
        },
    )

    store.set_oauth("github-copilot", credential)

    assert store.get_oauth("github-copilot") == credential
    assert '"account_id"' not in path.read_text(encoding="utf-8")
    assert not list(tmp_path.glob(".credentials.json.*"))


def test_file_credential_store_loads_legacy_codex_oauth_shape(tmp_path) -> None:
    path = tmp_path / "credentials.json"
    path.write_text(
        '{"openai-codex":{"type":"oauth","access":"a","refresh":"r",'
        '"expires":123,"account_id":"account"}}',
        encoding="utf-8",
    )

    credential = FileCredentialStore(path).get_oauth("openai-codex")

    assert credential == OAuthCredential(
        access="a",
        refresh="r",
        expires=123,
        account_id="account",
    )


# -- oauth.py (OpenAI Codex) ---------------------------------------------


def test_create_openai_codex_authorization_flow_includes_pkce_and_codex_params() -> None:
    flow = create_openai_codex_authorization_flow(originator="rio-test")

    url = urlparse(flow.url)
    params = parse_qs(url.query)

    assert url.geturl().startswith("https://auth.openai.com/oauth/authorize?")
    assert params["response_type"] == ["code"]
    assert params["client_id"] == [OPENAI_CODEX_CLIENT_ID]
    assert params["redirect_uri"] == ["http://localhost:1455/auth/callback"]
    assert params["scope"] == ["openid profile email offline_access"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["codex_cli_simplified_flow"] == ["true"]
    assert params["originator"] == ["rio-test"]
    assert params["state"] == [flow.state]
    assert params["code_challenge"][0]
    assert flow.verifier


def test_parse_authorization_input_accepts_redirect_url_query_and_raw_code() -> None:
    assert (
        parse_authorization_input("http://localhost:1455/auth/callback?code=abc&state=state-1").code
        == "abc"
    )
    assert parse_authorization_input("code=abc&state=state-1").state == "state-1"
    assert parse_authorization_input("abc#state-1").state == "state-1"
    assert parse_authorization_input("abc").code == "abc"


def test_account_id_from_access_token_reads_openai_auth_claim() -> None:
    assert account_id_from_access_token(_jwt("account-1")) == "account-1"
    assert account_id_from_access_token("not-a-jwt") is None


async def test_refresh_openai_codex_token_returns_oauth_credential() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        assert "grant_type=refresh_token" in body
        assert "client_id=" in body
        return httpx.Response(
            200,
            json={
                "access_token": _jwt("account-2"),
                "refresh_token": "new-refresh",
                "expires_in": 3600,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        credential = await refresh_openai_codex_token("old-refresh", client=client)

    assert credential.access == _jwt("account-2")
    assert credential.refresh == "new-refresh"
    assert credential.account_id == "account-2"
    assert credential.expires > 0


async def test_refresh_openai_codex_token_preserves_refresh_and_reads_jwt_expiry() -> None:
    expires = int(time()) + 3600
    access_token = _jwt("account-3", expires=expires)

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        assert "grant_type=refresh_token" in body
        return httpx.Response(200, json={"access_token": access_token})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        credential = await refresh_openai_codex_token("old-refresh", client=client)

    assert credential.access == access_token
    assert credential.refresh == "old-refresh"
    assert credential.account_id == "account-3"
    assert credential.expires == expires * 1000


# -- oauth_anthropic.py ----------------------------------------------------


async def test_refresh_anthropic_token_uses_json_and_redacts_failed_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == ANTHROPIC_TOKEN_URL
        assert request.headers["content-type"] == "application/json"
        assert request.content
        assert ANTHROPIC_CLIENT_ID.encode() in request.content
        return httpx.Response(401, text="secret-token-body")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OAuthError) as error:
            await refresh_anthropic_token("refresh-secret", client=client)

    assert "401" in str(error.value)
    assert "secret-token-body" not in str(error.value)
    assert "refresh-secret" not in str(error.value)


async def test_refresh_anthropic_token_reports_structured_oauth_error() -> None:
    """A dead refresh token should say so, not just report a status code."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": "invalid_grant",
                "error_description": "Refresh token not found or invalid",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OAuthError) as error:
            await refresh_anthropic_token("refresh-secret", client=client)

    assert "invalid_grant: Refresh token not found or invalid" in str(error.value)
    assert "refresh-secret" not in str(error.value)


async def test_refresh_anthropic_token_reports_nested_error_without_echoing_token() -> None:
    """Anthropic's nested envelope still yields detail, minus anything we sent."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": "refresh_token refresh-secret is malformed. " + "x" * 400,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OAuthError) as error:
            await refresh_anthropic_token("refresh-secret", client=client)

    message = str(error.value)
    assert "invalid_request_error: refresh_token <redacted> is malformed." in message
    assert "refresh-secret" not in message
    assert len(message) < 300


async def test_refresh_anthropic_token_scrubs_a_token_before_truncating() -> None:
    """Scrub then truncate: the other order leaks the surviving prefix."""
    secret = "refresh-" + "s" * 40

    def handler(_request: httpx.Request) -> httpx.Response:
        # Place the token so it straddles the 200-character truncation point.
        return httpx.Response(
            400,
            json={"error": "invalid_grant", "error_description": "y" * 175 + secret},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OAuthError) as error:
            await refresh_anthropic_token(secret, client=client)

    message = str(error.value)
    # Truncating first would have left the token's leading characters here.
    assert message.endswith("<redacted>")
    assert "refresh-ss" not in message


async def test_refresh_anthropic_token_returns_provider_neutral_credential() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": "anthropic-access",
                "refresh_token": "anthropic-refresh",
                "expires_in": 3600,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        credential = await refresh_anthropic_token("old-refresh", client=client)

    assert credential.access == "anthropic-access"
    assert credential.refresh == "anthropic-refresh"
    assert credential.account_id is None
    assert credential.expires > 0


# -- oauth_github_copilot.py -----------------------------------------------


async def test_github_copilot_device_login_and_token_exchange() -> None:
    device_codes: list[OAuthDeviceCodeInfo] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login/device/code":
            assert f"client_id={GITHUB_COPILOT_CLIENT_ID}" in request.content.decode()
            return httpx.Response(
                200,
                json={
                    "device_code": "device-secret",
                    "user_code": "ABCD-1234",
                    "verification_uri": "https://github.com/login/device",
                    "interval": 0,
                    "expires_in": 60,
                },
            )
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": "github-token"})
        if request.url.path == "/copilot_internal/v2/token":
            assert request.headers["authorization"] == "Bearer github-token"
            return httpx.Response(
                200,
                json={
                    "token": "tid=1;exp=9999999999;proxy-ep=proxy.business.githubcopilot.com",
                    "expires_at": 9999999999,
                },
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        credential = await login_github_copilot(
            _callbacks(device_codes=device_codes),
            client=client,
        )

    assert device_codes == [
        OAuthDeviceCodeInfo(
            user_code="ABCD-1234",
            verification_uri="https://github.com/login/device",
            interval_seconds=0,
            expires_in_seconds=60,
        )
    ]
    assert credential.refresh == "github-token"
    assert credential.access.startswith("tid=1")
    assert github_copilot_base_url(credential.access) == ("https://api.business.githubcopilot.com")


async def test_github_copilot_rejects_untrusted_device_verification_uri() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "device_code": "device",
                "user_code": "code",
                "verification_uri": "file:///tmp/not-safe",
                "interval": 5,
                "expires_in": 60,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OAuthError, match="Untrusted verification_uri"):
            await login_github_copilot(_callbacks(), client=client)


async def test_refresh_github_copilot_preserves_enterprise_metadata() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.ghe.example.com"
        return httpx.Response(200, json={"token": "copilot", "expires_at": 9999999999})

    original = OAuthCredential(
        access="old",
        refresh="github-token",
        expires=1,
        metadata={"enterprise_domain": "ghe.example.com"},
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        refreshed = await refresh_github_copilot_token(original, client=client)

    assert refreshed.metadata == original.metadata
    assert normalize_github_domain("https://ghe.example.com/path") == "ghe.example.com"
    assert github_copilot_base_url(None, "ghe.example.com") == (
        "https://copilot-api.ghe.example.com"
    )


# -- oauth_device.py ---------------------------------------------------


async def test_device_poll_slow_down_and_cancel() -> None:
    sleeps: list[float] = []
    results = iter(
        [
            DevicePollResult[str](status="slow_down"),
            DevicePollResult(status="complete", value="done"),
        ]
    )

    async def fake_poll() -> DevicePollResult[str]:
        return next(results)

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    assert (
        await poll_oauth_device_code(
            fake_poll,
            interval_seconds=1,
            expires_in_seconds=60,
            sleep=fake_sleep,
        )
        == "done"
    )
    assert sleeps == [6]

    cancel_event = asyncio.Event()
    cancel_event.set()
    with pytest.raises(OAuthError, match="Login cancelled"):
        await poll_oauth_device_code(fake_poll, cancel_event=cancel_event)


# -- oauth_registry.py ---------------------------------------------------


def test_builtin_oauth_registry_matches_supported_subscription_providers() -> None:
    assert oauth_provider_ids() == {"anthropic", "github-copilot", "openai-codex"}
    anthropic = get_oauth_provider("anthropic")
    assert anthropic is not None
    assert anthropic.name == "Anthropic (Claude Pro/Max)"
    assert get_oauth_provider("missing") is None


# -- rio-specific: credential path moved under ~/.rio --------------------


def test_credentials_path_is_rooted_under_rio_home(tmp_path) -> None:
    from rio_coding.credentials import credentials_path
    from rio_coding.paths import RioPaths

    assert credentials_path().name == "credentials.json"
    assert str(RioPaths().home).endswith(".rio")

    custom = RioPaths(home=tmp_path / ".rio")
    assert credentials_path(custom) == tmp_path / ".rio" / "credentials.json"


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_cancelling_login_cleans_up_callback_and_manual_waiters(provider):
    from rio_coding.oauth import _wait_for_authorization_code
    from rio_coding.oauth_anthropic import _wait_for_input

    started = asyncio.Event()
    stopped = []

    class Server:
        cancelled = False

        async def wait_for_code(self):
            try:
                await asyncio.Future()
            finally:
                stopped.append("server")

        def cancel_wait(self):
            self.cancelled = True

    async def manual():
        started.set()
        try:
            await asyncio.Future()
        finally:
            stopped.append("manual")

    server = Server()
    if provider == "openai":
        waiter = _wait_for_authorization_code(
            flow=create_openai_codex_authorization_flow(),
            server=server,
            on_manual_code_input=manual,
        )
    else:
        waiter = _wait_for_input(server, manual)
    task = asyncio.create_task(waiter)
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sorted(stopped) == ["manual", "server"]
    assert server.cancelled
