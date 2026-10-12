"""Anthropic Claude Pro/Max OAuth provider."""

from __future__ import annotations

import time
import webbrowser
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

ANTHROPIC_OAUTH_PROVIDER = "anthropic"
ANTHROPIC_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
ANTHROPIC_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
ANTHROPIC_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
ANTHROPIC_REDIRECT_URI = "http://localhost:53692/callback"
ANTHROPIC_SCOPE = (
    "org:create_api_key user:profile user:inference user:sessions:claude_code "
    "user:mcp_servers user:file_upload"
)
ANTHROPIC_CALLBACK_PORT = 53692
ANTHROPIC_TOKEN_SKEW_MS = 5 * 60 * 1000
# Token-request fields worth scrubbing out of anything the server echoes back.
_SECRET_REQUEST_FIELDS = ("refresh_token", "code", "code_verifier")


async def login_anthropic(
    *,
    on_auth: AuthCallback,
    on_prompt: PromptCallback,
    on_progress: ProgressCallback | None = None,
    client: httpx.AsyncClient | None = None,
) -> OAuthCredential:
    """Run Anthropic's authorization-code + PKCE login flow."""
    verifier, challenge = create_pkce_pair()
    params = {
        "code": "true",
        "client_id": ANTHROPIC_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": ANTHROPIC_REDIRECT_URI,
        "scope": ANTHROPIC_SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": verifier,
    }
    url = f"{ANTHROPIC_AUTHORIZE_URL}?{urlencode(params)}"
    server = await start_local_oauth_server(
        verifier,
        callback_port=ANTHROPIC_CALLBACK_PORT,
        callback_path="/callback",
        success_message="Anthropic authentication completed. You can close this window.",
    )
    on_auth(
        OAuthAuthInfo(
            url=url,
            instructions=(
                "Complete login in your browser. If the browser is on another machine, "
                "paste the final redirect URL here."
            ),
        )
    )
    webbrowser.open(url)

    try:
        value = await server.wait_for_code() if server is not None else None
        if value is None:
            value = await on_prompt(
                OAuthPrompt(
                    message="Paste the authorization code or full redirect URL:",
                    placeholder=ANTHROPIC_REDIRECT_URI,
                )
            )
        parsed = parse_authorization_input(value)
        validate_state(parsed.state, verifier)
        if not parsed.code:
            raise OAuthError("Missing authorization code")
        on_progress and on_progress("Exchanging authorization code for tokens...")
        return await _anthropic_token_request(
            {
                "grant_type": "authorization_code",
                "client_id": ANTHROPIC_CLIENT_ID,
                "code": parsed.code,
                "state": parsed.state or verifier,
                "redirect_uri": ANTHROPIC_REDIRECT_URI,
                "code_verifier": verifier,
            },
            client=client,
            action="exchange",
        )
    finally:
        if server is not None:
            server.close()


async def refresh_anthropic_token(
    refresh_token: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> OAuthCredential:
    """Refresh Anthropic OAuth credentials."""
    return await _anthropic_token_request(
        {
            "grant_type": "refresh_token",
            "client_id": ANTHROPIC_CLIENT_ID,
            "refresh_token": refresh_token,
        },
        client=client,
        action="refresh",
        previous_refresh=refresh_token,
    )


async def _anthropic_token_request(
    data: dict[str, str],
    *,
    client: httpx.AsyncClient | None,
    action: str,
    previous_refresh: str | None = None,
) -> OAuthCredential:
    async with oauth_client(client) as active_client:
        response = await active_client.post(
            ANTHROPIC_TOKEN_URL,
            json=data,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
    if response.status_code >= 400:
        detail = error_detail(
            response,
            secrets=[data[field] for field in _SECRET_REQUEST_FIELDS if field in data],
        )
        raise OAuthError(f"Anthropic token {action} failed ({response.status_code}): {detail}")
    raw = response.json()
    if not isinstance(raw, dict):
        raise OAuthError(f"Anthropic token {action} response must be an object")
    access = required_string(raw, "access_token", f"Anthropic token {action}")
    refresh = optional_string(raw, "refresh_token") or previous_refresh
    expires_in = raw.get("expires_in")
    if refresh is None:
        raise OAuthError(f"Anthropic token {action} response missing refresh_token")
    if not isinstance(expires_in, int | float) or isinstance(expires_in, bool) or expires_in <= 0:
        raise OAuthError(f"Anthropic token {action} response missing expires_in")
    return OAuthCredential(
        access=access,
        refresh=refresh,
        expires=int(time.time() * 1000 + expires_in * 1000 - ANTHROPIC_TOKEN_SKEW_MS),
    )


class AnthropicOAuthProvider:
    """Registered Anthropic Claude subscription OAuth behavior."""

    id = ANTHROPIC_OAUTH_PROVIDER
    name = "Anthropic (Claude Pro/Max)"

    async def login(self, callbacks: OAuthLoginCallbacks) -> OAuthCredential:
        return await login_anthropic(
            on_auth=callbacks.on_auth,
            on_prompt=callbacks.on_prompt,
            on_progress=callbacks.on_progress,
        )

    async def refresh(self, credential: OAuthCredential) -> OAuthCredential:
        return await refresh_anthropic_token(credential.refresh)

    def runtime_auth(self, credential: OAuthCredential) -> RuntimeProviderAuth:
        return RuntimeProviderAuth(
            api_key=credential.access,
            headers={
                "Authorization": f"Bearer {credential.access}",
                "anthropic-beta": "claude-code-20250219,oauth-2025-04-20",
                "user-agent": "claude-cli/rio",
                "x-app": "cli",
            },
        )
