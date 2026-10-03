"""OAuth for HTTP MCP servers, per the MCP authorization spec.

Discovery follows the server's 401 challenge to its protected resource
metadata (RFC 9728), then the authorization server metadata (RFC 8414 or
OpenID Connect). The client is pre-registered (`oauth.clientId`) or registered
dynamically (RFC 7591). Sign-in is an authorization code grant with PKCE
(S256), a loopback redirect (RFC 8252), and the `resource` parameter
(RFC 8707). Tokens are refreshed when they expire or the server rejects them.

Sign-in happens only in `rio mcp login`; a running session cannot open a
browser, so it reports the server as needing sign-in instead.

Credentials live in `~/.rio/mcp-auth.json` (mode 0600), keyed by server name
and bound to its URL, so a renamed or repointed server never receives another
server's token.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import httpx

from rio.coding.mcp.client import LATEST_PROTOCOL_VERSION, McpAuthRequiredError
from rio.coding.paths import RioPaths

#: Refresh tokens this long before they expire.
EXPIRY_SKEW_SECONDS = 60


class McpOAuthError(RuntimeError):
    """OAuth discovery, registration, or a token request failed."""

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


# -- credential store -------------------------------------------------------------


def auth_store_path(paths: RioPaths | None = None) -> Path:
    return (paths or RioPaths()).mcp_auth_path


class McpAuthStore:
    """Per-server OAuth state: client registration, tokens, and discovery results."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or auth_store_path()
        self._lock = threading.Lock()

    def load(self, name: str, server_url: str) -> dict[str, Any]:
        state = self._read().get(name)
        if isinstance(state, dict) and state.get("serverUrl") == server_url:
            return state
        return {"serverUrl": server_url}

    def save(self, name: str, state: dict[str, Any]) -> None:
        with self._lock:
            data = self._read()
            data[name] = state
            self._write(data)

    def remove(self, name: str, server_url: str) -> bool:
        with self._lock:
            data = self._read()
            state = data.get(name)
            if not isinstance(state, dict) or state.get("serverUrl") != server_url:
                return False
            del data[name]
            self._write(data)
            return True

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
        temporary.replace(self.path)


# -- discovery ---------------------------------------------------------------------


def parse_www_authenticate(header: str | None) -> dict[str, str]:
    """`resource_metadata`, `scope`, and `error` of a Bearer challenge."""
    if not header or header.split(None, 1)[0].lower() not in ("bearer", "dpop"):
        return {}
    fields: dict[str, str] = {}
    for name in ("resource_metadata", "scope", "error"):
        match = re.search(rf'(?:^|[,\s]){name}=(?:"([^"]*)"|([^\s,]+))', header, re.IGNORECASE)
        if match and (match.group(1) or match.group(2)):
            fields[name] = match.group(1) or match.group(2)
    return fields


def _well_known(base: str, kind: str, *, path_first: bool = False) -> str:
    url = urlparse(base)
    path = url.path.rstrip("/")
    if path_first:
        return urlunparse((url.scheme, url.netloc, f"{path}/.well-known/{kind}", "", "", ""))
    return urlunparse((url.scheme, url.netloc, f"/.well-known/{kind}{path}", "", "", ""))


def _fetch_json(http: httpx.Client, url: str) -> dict[str, Any] | None:
    """The JSON document at `url`, or None when it is not there (4xx, 502)."""
    response = http.get(
        url, headers={"Accept": "application/json", "MCP-Protocol-Version": LATEST_PROTOCOL_VERSION}
    )
    if 400 <= response.status_code < 500 or response.status_code == 502:
        return None
    if response.status_code >= 400:
        raise McpOAuthError(f"HTTP {response.status_code} loading {url}")
    data = response.json()
    if not isinstance(data, dict):
        raise McpOAuthError(f"{url} is not a JSON object")
    return data


def discover_protected_resource(
    http: httpx.Client, server_url: str, metadata_url: str | None
) -> dict[str, Any] | None:
    """RFC 9728: from the challenge's `resource_metadata`, else the well-known URIs."""
    candidates = (
        [metadata_url]
        if metadata_url
        else [
            _well_known(server_url, "oauth-protected-resource"),
            _well_known(urljoin(server_url, "/"), "oauth-protected-resource"),
        ]
    )
    for url in dict.fromkeys(candidates):
        metadata = _fetch_json(http, url)
        if metadata is not None:
            if not isinstance(metadata.get("resource"), str):
                raise McpOAuthError(f"{url}: protected resource metadata has no resource")
            return metadata
    return None


def discover_authorization_server(http: httpx.Client, issuer: str) -> dict[str, Any] | None:
    """RFC 8414, then OpenID Connect discovery. The metadata must name `issuer`."""
    candidates = [
        _well_known(issuer, "oauth-authorization-server"),
        _well_known(issuer, "openid-configuration"),
    ]
    if urlparse(issuer).path.strip("/"):
        candidates.append(_well_known(issuer, "openid-configuration", path_first=True))
    for url in candidates:
        metadata = _fetch_json(http, url)
        if metadata is None:
            continue
        if str(metadata.get("issuer", "")).rstrip("/") != issuer.rstrip("/"):
            raise McpOAuthError(f"{url}: issuer {metadata.get('issuer')} does not match {issuer}")
        for key in ("authorization_endpoint", "token_endpoint"):
            if not isinstance(metadata.get(key), str):
                raise McpOAuthError(f"{url}: authorization server metadata has no {key}")
        return metadata
    return None


def select_resource(server_url: str, resource_metadata: dict[str, Any] | None) -> str | None:
    """The `resource` to request tokens for (RFC 8707): it must contain the server URL."""
    if resource_metadata is None:
        return None
    resource = resource_metadata["resource"]
    requested, configured = urlparse(server_url), urlparse(resource)
    if (requested.scheme, requested.netloc) != (configured.scheme, configured.netloc) or not (
        requested.path.rstrip("/") + "/"
    ).startswith(configured.path.rstrip("/") + "/"):
        raise McpOAuthError(f"protected resource {resource} does not match {server_url}")
    return resource


def discover(http: httpx.Client, server_url: str, challenge: dict[str, str]) -> dict[str, Any]:
    resource_metadata = discover_protected_resource(
        http, server_url, challenge.get("resource_metadata")
    )
    servers = (resource_metadata or {}).get("authorization_servers") or []
    issuer = servers[0] if servers else urljoin(server_url, "/")
    metadata = discover_authorization_server(http, issuer)
    if metadata is None:
        raise McpOAuthError(f"no authorization server metadata found for {issuer}")
    _require_secure(metadata["token_endpoint"])
    return {"issuer": issuer, "metadata": metadata, "resourceMetadata": resource_metadata}


def _require_secure(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise McpOAuthError(f"refusing to send credentials to insecure endpoint {url}")


# -- token requests ------------------------------------------------------------------


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _client_auth(
    client: dict[str, Any], metadata: dict[str, Any], headers: dict[str, str], form: dict[str, str]
) -> None:
    supported = metadata.get("token_endpoint_auth_methods_supported") or []
    secret = client.get("client_secret")
    method = client.get("token_endpoint_auth_method")
    if method not in ("client_secret_basic", "client_secret_post", "none") or (
        supported and method not in supported
    ):
        if secret and (not supported or "client_secret_basic" in supported):
            method = "client_secret_basic"
        elif secret and "client_secret_post" in supported:
            method = "client_secret_post"
        else:
            method = "none"
    if method == "client_secret_basic" and secret:
        pair = f"{client['client_id']}:{secret}".encode()
        headers["Authorization"] = "Basic " + base64.b64encode(pair).decode("ascii")
        return
    form["client_id"] = client["client_id"]
    if method == "client_secret_post" and secret:
        form["client_secret"] = secret


def token_request(
    http: httpx.Client,
    discovery: dict[str, Any],
    client: dict[str, Any],
    form: dict[str, str],
    resource: str | None,
) -> dict[str, Any]:
    metadata = discovery["metadata"]
    url = metadata["token_endpoint"]
    _require_secure(url)
    headers = {"Accept": "application/json"}
    form = dict(form)
    if resource:
        form["resource"] = resource
    _client_auth(client, metadata, headers, form)
    response = http.post(url, data=form, headers=headers)
    try:
        body = response.json()
    except ValueError:
        body = None
    # Servers may report OAuth errors with any status, so check the body first.
    if isinstance(body, dict) and isinstance(body.get("error"), str):
        description = body.get("error_description") or body["error"]
        raise McpOAuthError(f"token request failed: {description}", body["error"])
    if response.status_code >= 400 or not isinstance(body, dict):
        raise McpOAuthError(f"token request failed: HTTP {response.status_code}", "server_error")
    if not isinstance(body.get("access_token"), str):
        raise McpOAuthError("token response has no access_token")
    return body


def _save_tokens(state: dict[str, Any], tokens: dict[str, Any], scope: str | None) -> None:
    previous = state.get("tokens") or {}
    tokens = dict(tokens)
    # A refresh may omit the refresh token (keep the old one) and the scope (unchanged grant).
    if "refresh_token" not in tokens and previous.get("refresh_token"):
        tokens["refresh_token"] = previous["refresh_token"]
    if not tokens.get("scope") and scope:
        tokens["scope"] = scope
    state["tokens"] = tokens
    expires_in = tokens.get("expires_in")
    if isinstance(expires_in, int | float) and not isinstance(expires_in, bool):
        state["expiresAt"] = time.time() + expires_in
    else:
        state.pop("expiresAt", None)


def configured_client(oauth: dict[str, Any], secret: str | None) -> dict[str, Any] | None:
    if not oauth.get("clientId"):
        return None
    client: dict[str, Any] = {"client_id": oauth["clientId"]}
    if secret:
        client["client_secret"] = secret
    return client


# -- runtime auth ----------------------------------------------------------------------


class OAuthAuth:
    """Bearer tokens from the store. Refreshes them; a new sign-in needs `rio mcp login`."""

    def __init__(
        self,
        name: str,
        server_url: str,
        store: McpAuthStore,
        *,
        client: dict[str, Any] | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        self.name = name
        self.server_url = server_url
        self.store = store
        self._configured_client = client
        self._http = http or httpx.Client(follow_redirects=True, timeout=30)
        self._lock = threading.Lock()

    def token(self) -> str | None:
        with self._lock:
            state = self.store.load(self.name, self.server_url)
            tokens = state.get("tokens")
            if not tokens:
                return None
            expires_at = state.get("expiresAt")
            if expires_at is not None and time.time() > expires_at - EXPIRY_SKEW_SECONDS:
                if not self._refresh(state):
                    return None
                tokens = state["tokens"]
            return tokens.get("access_token")

    def on_unauthorized(self, response: httpx.Response, sent: str | None) -> None:
        challenge = parse_www_authenticate(response.headers.get("www-authenticate"))
        hint = f"run `rio mcp login {self.name}` to sign in"
        if challenge.get("error") == "insufficient_scope":
            scope = challenge.get("scope", "unknown scope")
            raise McpAuthRequiredError(
                f"MCP server {self.name} needs more permissions ({scope}); {hint}",
                response.headers.get("www-authenticate"),
            )
        with self._lock:
            state = self.store.load(self.name, self.server_url)
            current = (state.get("tokens") or {}).get("access_token")
            if current and current != sent:
                return  # Another process refreshed the token; retry with it.
            if self._refresh(state):
                return
        raise McpAuthRequiredError(
            f"MCP server {self.name} requires sign-in; {hint}",
            response.headers.get("www-authenticate"),
        )

    def _refresh(self, state: dict[str, Any]) -> bool:
        refresh_token = (state.get("tokens") or {}).get("refresh_token")
        client = self._configured_client or state.get("client")
        discovery = state.get("discovery")
        if not refresh_token or not client or not discovery:
            return False
        try:
            tokens = token_request(
                self._http,
                discovery,
                client,
                {"grant_type": "refresh_token", "refresh_token": refresh_token},
                discovery.get("resource"),
            )
        except (McpOAuthError, httpx.HTTPError):
            state.pop("tokens", None)
            state.pop("expiresAt", None)
            self.store.save(self.name, state)
            return False
        _save_tokens(state, tokens, (state.get("tokens") or {}).get("scope"))
        self.store.save(self.name, state)
        return True


# -- sign-in ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Callback:
    code: str | None
    state: str | None
    iss: str | None
    error: str | None


class _CallbackServer:
    """Loopback HTTP server that receives the authorization redirect."""

    def __init__(self, port: int) -> None:
        received: list[Callback] = []
        done = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                url = urlparse(self.path)
                if url.path != "/callback":
                    self.send_error(404)
                    return
                query = {key: values[0] for key, values in parse_qs(url.query).items()}
                error = query.get("error_description") or query.get("error")
                received.append(
                    Callback(query.get("code"), query.get("state"), query.get("iss"), error)
                )
                done.set()
                text = "Sign-in failed: " + error if error else "Signed in. You can close this tab."
                body = f"<html><body><p>{text}</p></body></html>".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._received = received
        self._done = done
        self.redirect_uri = f"http://127.0.0.1:{self._server.server_address[1]}/callback"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def wait(self, timeout: float) -> Callback | None:
        return self._received[0] if self._done.wait(timeout) else None

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _bind_callback(port: int | None, previous_uris: list[str]) -> _CallbackServer:
    """Listen on the configured port, else the port a registered client used, else any port."""
    if port is not None:
        return _CallbackServer(port)
    for uri in previous_uris:
        parsed = urlparse(uri)
        if parsed.hostname == "127.0.0.1" and parsed.port:
            try:
                return _CallbackServer(parsed.port)
            except OSError:
                break
    return _CallbackServer(0)


def sign_in(
    name: str,
    server_url: str,
    oauth: dict[str, Any],
    client_secret: str | None,
    store: McpAuthStore,
    *,
    www_authenticate: str | None,
    open_url: Callable[[str], None],
    timeout: float = 300.0,
    http: httpx.Client | None = None,
) -> None:
    """Run the authorization code flow and store the tokens."""
    http = http or httpx.Client(follow_redirects=True, timeout=30)
    challenge = parse_www_authenticate(www_authenticate)
    discovery = discover(http, server_url, challenge)
    metadata = discovery["metadata"]
    if "code" not in (metadata.get("response_types_supported") or ["code"]):
        raise McpOAuthError("authorization server does not support authorization codes")
    methods = metadata.get("code_challenge_methods_supported")
    if methods is not None and "S256" not in methods:
        raise McpOAuthError("authorization server does not support PKCE S256")
    resource = select_resource(server_url, discovery["resourceMetadata"])
    discovery["resource"] = resource
    scope = (
        oauth.get("scope")
        or challenge.get("scope")
        or " ".join((discovery["resourceMetadata"] or {}).get("scopes_supported") or ())
        or None
    )

    state = store.load(name, server_url)
    client = configured_client(oauth, client_secret)
    stored = state.get("client") if client is None else None
    callback = _bind_callback(oauth.get("callbackPort"), (stored or {}).get("redirect_uris", []))
    try:
        if client is None:
            if stored and callback.redirect_uri in stored.get("redirect_uris", ()):
                client = stored
            else:
                client = register_client(
                    http, metadata, callback.redirect_uri, oauth.get("clientName") or "rio", scope
                )
                state["client"] = client
        verifier, challenge_value = pkce_pair()
        expected_state = secrets.token_urlsafe(24)
        params = {
            "response_type": "code",
            "client_id": client["client_id"],
            "redirect_uri": callback.redirect_uri,
            "code_challenge": challenge_value,
            "code_challenge_method": "S256",
            "state": expected_state,
        }
        if scope:
            params["scope"] = scope
        if resource:
            params["resource"] = resource
        endpoint = metadata["authorization_endpoint"]
        separator = "&" if urlparse(endpoint).query else "?"
        open_url(f"{endpoint}{separator}{urlencode(params)}")
        result = callback.wait(timeout)
    finally:
        callback.close()
    if result is None:
        raise McpOAuthError(f"sign-in was not completed within {timeout:g} seconds")
    if result.error:
        raise McpOAuthError(f"authorization failed: {result.error}")
    if result.state != expected_state:
        raise McpOAuthError("authorization response has the wrong state")
    # RFC 9207: never send a code from another authorization server to this one.
    issuer = metadata["issuer"]
    iss_expected = metadata.get("authorization_response_iss_parameter_supported")
    if (result.iss is not None or iss_expected) and result.iss != issuer:
        raise McpOAuthError(f"authorization response came from {result.iss}, not {issuer}")
    if not result.code:
        raise McpOAuthError("authorization response has no code")
    tokens = token_request(
        http,
        discovery,
        client,
        {
            "grant_type": "authorization_code",
            "code": result.code,
            "code_verifier": verifier,
            "redirect_uri": callback.redirect_uri,
        },
        resource,
    )
    state["discovery"] = discovery
    _save_tokens(state, tokens, scope)
    store.save(name, state)


def register_client(
    http: httpx.Client,
    metadata: dict[str, Any],
    redirect_uri: str,
    client_name: str,
    scope: str | None,
) -> dict[str, Any]:
    """Dynamic client registration (RFC 7591) as a public client."""
    endpoint = metadata.get("registration_endpoint")
    if not endpoint:
        raise McpOAuthError(
            "the authorization server does not support dynamic client registration; "
            "configure oauth.clientId"
        )
    body: dict[str, Any] = {
        "client_name": client_name,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    if scope:
        body["scope"] = scope
    response = http.post(endpoint, json=body, headers={"Accept": "application/json"})
    if response.status_code >= 400:
        raise McpOAuthError(
            f"client registration failed: HTTP {response.status_code} {response.text[:300]}"
        )
    client = response.json()
    if not isinstance(client, dict) or not isinstance(client.get("client_id"), str):
        raise McpOAuthError("client registration response has no client_id")
    client.setdefault("redirect_uris", [redirect_uri])
    return client
