"""OpenAI Codex (ChatGPT subscription) OAuth provider."""

from __future__ import annotations

import asyncio
import base64
import secrets
import time
import webbrowser
from dataclasses import dataclass
from json import loads
from typing import Any
from urllib.parse import urlencode

import httpx

from rio.ai.env import RuntimeProviderAuth
from rio.coding.credentials import OAuthCredential
from rio.coding.oauth import (
    AuthCallback,
    OAuthAuthInfo,
    OAuthError,
    OAuthLoginCallbacks,
    OAuthPrompt,
    ProgressCallback,
    PromptCallback,
    create_pkce_pair,
    error_detail,
    oauth_client,
    optional_string,
    parse_authorization_input,
    required_string,
    start_local_oauth_server,
    validate_state,
)

OPENAI_CODEX_OAUTH_PROVIDER = "openai-codex"
OPENAI_CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
OPENAI_CODEX_AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
OPENAI_CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
OPENAI_CODEX_REDIRECT_URI = "http://localhost:1455/auth/callback"
OPENAI_CODEX_SCOPE = "openid profile email offline_access"
OPENAI_CODEX_ACCOUNT_CLAIM = "https://api.openai.com/auth"
OPENAI_CODEX_CALLBACK_PORT = 1455


@dataclass(frozen=True, slots=True)
class AuthorizationFlow:
    """OpenAI Codex OAuth authorization flow state."""

    verifier: str
    state: str
    url: str


@dataclass(frozen=True, slots=True)
class TokenResponse:
    """Successful OAuth token response."""

    access: str
    refresh: str
    expires: int


class OpenAICodexOAuthProvider:
    """Registered OpenAI Codex subscription OAuth behavior."""

    id = OPENAI_CODEX_OAUTH_PROVIDER
    name = "OpenAI Codex (ChatGPT subscription)"

    async def login(self, callbacks: OAuthLoginCallbacks) -> OAuthCredential:
        if callbacks.method == "device_code":
            raise OAuthError("OpenAI Codex device-code login is not implemented in rio yet")
        return await login_openai_codex(
            on_auth=callbacks.on_auth,
            on_prompt=callbacks.on_prompt,
            on_progress=callbacks.on_progress,
        )

    async def refresh(self, credential: OAuthCredential) -> OAuthCredential:
        return await refresh_openai_codex_token(credential.refresh)

    def runtime_auth(self, credential: OAuthCredential) -> RuntimeProviderAuth:
        return RuntimeProviderAuth(api_key=credential.access)


def create_openai_codex_authorization_flow(
    *,
    originator: str = "rio",
) -> AuthorizationFlow:
    """Create an OpenAI Codex OAuth authorization URL."""
    verifier, challenge = create_pkce_pair()
    state = secrets.token_hex(16)
    params = {
        "response_type": "code",
        "client_id": OPENAI_CODEX_CLIENT_ID,
        "redirect_uri": OPENAI_CODEX_REDIRECT_URI,
        "scope": OPENAI_CODEX_SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "originator": originator,
    }
    return AuthorizationFlow(
        verifier=verifier,
        state=state,
        url=f"{OPENAI_CODEX_AUTHORIZE_URL}?{urlencode(params)}",
    )


async def login_openai_codex(
    *,
    on_auth: AuthCallback,
    on_prompt: PromptCallback,
    on_progress: ProgressCallback | None = None,
    originator: str = "rio",
    client: httpx.AsyncClient | None = None,
) -> OAuthCredential:
    """Run OpenAI Codex OAuth and return refreshable credentials."""
    flow = create_openai_codex_authorization_flow(originator=originator)
    server = await start_local_oauth_server(
        flow.state,
        callback_port=OPENAI_CODEX_CALLBACK_PORT,
        callback_path="/auth/callback",
        success_message="OpenAI authentication completed. You can close this window.",
    )

    try:
        on_auth(
            OAuthAuthInfo(
                url=flow.url,
                instructions="Complete login in your browser, or paste the redirect URL below.",
            )
        )
        await asyncio.to_thread(webbrowser.open, flow.url)

        code = await server.wait_for_code() if server is not None else None
        if code is None:
            manual_input = await on_prompt(
                OAuthPrompt(message="Paste the authorization code or full redirect URL:")
            )
            parsed = parse_authorization_input(manual_input)
            validate_state(parsed.state, flow.state)
            code = parsed.code
        if not code:
            raise OAuthError("Missing authorization code")
        on_progress and on_progress("Exchanging authorization code...")
        token = await exchange_openai_codex_authorization_code(
            code,
            flow.verifier,
            client=client,
        )
        account_id = account_id_from_access_token(token.access)
        if account_id is None:
            raise OAuthError("Failed to extract OpenAI account id from access token")
        return OAuthCredential(
            access=token.access,
            refresh=token.refresh,
            expires=token.expires,
            account_id=account_id,
        )
    finally:
        if server is not None:
            server.close()


async def exchange_openai_codex_authorization_code(
    code: str,
    verifier: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> TokenResponse:
    """Exchange an OpenAI Codex authorization code for OAuth tokens."""
    raw = await _post_openai_codex_token(
        {
            "grant_type": "authorization_code",
            "client_id": OPENAI_CODEX_CLIENT_ID,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": OPENAI_CODEX_REDIRECT_URI,
        },
        client=client,
        action="exchange",
    )
    access_token = required_string(raw, "access_token", "OpenAI Codex token exchange")
    refresh_token = required_string(raw, "refresh_token", "OpenAI Codex token exchange")
    return TokenResponse(
        access=access_token,
        refresh=refresh_token,
        expires=_token_expiry(raw, access_token, action="exchange"),
    )


async def refresh_openai_codex_token(
    refresh_token: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> OAuthCredential:
    """Refresh OpenAI Codex OAuth credentials."""
    raw = await _post_openai_codex_token(
        {
            "grant_type": "refresh_token",
            "client_id": OPENAI_CODEX_CLIENT_ID,
            "refresh_token": refresh_token,
        },
        client=client,
        action="refresh",
    )
    access_token = required_string(raw, "access_token", "OpenAI Codex token refresh")
    next_refresh_token = optional_string(raw, "refresh_token") or refresh_token
    account_id = account_id_from_access_token(access_token)
    if account_id is None:
        raise OAuthError("Failed to extract OpenAI account id from refreshed access token")
    return OAuthCredential(
        access=access_token,
        refresh=next_refresh_token,
        expires=_token_expiry(raw, access_token, action="refresh"),
        account_id=account_id,
    )


def account_id_from_access_token(access_token: str) -> str | None:
    """Extract the ChatGPT account id from an OpenAI Codex access JWT."""
    payload = _access_token_payload(access_token)
    if payload is None:
        return None
    auth = payload.get(OPENAI_CODEX_ACCOUNT_CLAIM)
    if not isinstance(auth, dict):
        return None
    account_id = auth.get("chatgpt_account_id")
    if not isinstance(account_id, str) or not account_id.strip():
        return None
    return account_id.strip()


def _access_token_expiry(access_token: str) -> int | None:
    payload = _access_token_payload(access_token)
    if payload is None:
        return None
    exp = payload.get("exp")
    if isinstance(exp, int | float) and not isinstance(exp, bool) and exp > 0:
        return int(exp * 1000)
    return None


def _access_token_payload(access_token: str) -> dict[str, Any] | None:
    try:
        parts = access_token.split(".")
        if len(parts) != 3:
            return None
        payload = loads(_base64url_decode(parts[1]).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


async def _post_openai_codex_token(
    data: dict[str, str],
    *,
    client: httpx.AsyncClient | None,
    action: str,
) -> dict[str, Any]:
    async with oauth_client(client, timeout=60) as active_client:
        response = await active_client.post(
            OPENAI_CODEX_TOKEN_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    if response.status_code >= 400:
        detail = error_detail(response, secrets=data.values())
        raise OAuthError(f"OpenAI Codex token {action} failed ({response.status_code}): {detail}")

    raw = response.json()
    if not isinstance(raw, dict):
        raise OAuthError(f"OpenAI Codex token {action} response must be a JSON object")
    return raw


def _token_expiry(raw: dict[str, Any], access_token: str, *, action: str) -> int:
    expires_in = raw.get("expires_in")
    if isinstance(expires_in, int | float) and not isinstance(expires_in, bool):
        return int(time.time() * 1000) + int(expires_in * 1000)
    if expires_in is not None:
        raise OAuthError(f"OpenAI Codex token {action} response has invalid expires_in")
    expires = _access_token_expiry(access_token)
    if expires is not None:
        return expires
    raise OAuthError(f"OpenAI Codex token {action} response missing expiry")


def _base64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
