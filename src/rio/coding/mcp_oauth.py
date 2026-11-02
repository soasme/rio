"""MCP OAuth Client Credentials extension: get a token without a browser.

Follows the MCP authorization discovery (RFC 9728 protected resource metadata, then
RFC 8414 or OpenID Connect authorization server metadata) and requests a token with
``grant_type=client_credentials`` and an RFC 8707 ``resource``. The client authenticates
with a client secret; ``private_key_jwt`` is not supported.
"""

from __future__ import annotations

import base64
import re
from urllib.parse import quote_plus, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field

_PARAM = re.compile(r'(\w+)=(?:"([^"]*)"|([^\s,]+))')


class McpOAuth(BaseModel):
    """Client credentials for one HTTP server."""

    model_config = ConfigDict(extra="ignore")

    client_id: str = Field(alias="clientId")
    client_secret: str = Field(alias="clientSecret")
    scope: str | None = None


class McpAuthError(RuntimeError):
    """Discovery or the token request failed."""


def challenge(header: str) -> dict[str, str]:
    """Return the parameters of a ``WWW-Authenticate: Bearer ...`` header."""
    return {name: quoted or bare for name, quoted, bare in _PARAM.findall(header)}


def client_credentials_token(server_url: str, oauth: McpOAuth, www_authenticate: str) -> str:
    """Discover the authorization server for ``server_url`` and return an access token."""
    params = challenge(www_authenticate)
    with httpx.Client(timeout=60) as http:
        resource = _protected_resource(http, server_url, params.get("resource_metadata"))
        servers = resource.get("authorization_servers") or []
        if not servers:
            raise McpAuthError(f"{server_url} names no authorization server")
        metadata = _authorization_server(http, servers[0])
        scope = oauth.scope or params.get("scope") or " ".join(resource.get("scopes_supported", []))
        form = {
            "grant_type": "client_credentials",
            "resource": resource.get("resource", server_url),
        }
        if scope:
            form["scope"] = scope
        methods = metadata.get("token_endpoint_auth_methods_supported", ["client_secret_basic"])
        headers = {}
        if "client_secret_post" in methods:
            form |= {"client_id": oauth.client_id, "client_secret": oauth.client_secret}
        elif "client_secret_basic" in methods:
            user = f"{quote_plus(oauth.client_id)}:{quote_plus(oauth.client_secret)}"
            headers["Authorization"] = "Basic " + base64.b64encode(user.encode()).decode()
        else:
            raise McpAuthError(f"{servers[0]} supports no client secret authentication")
        endpoint = metadata.get("token_endpoint", "")
        _require_secure(endpoint)
        response = http.post(endpoint, data=form, headers=headers)
        body = _json(response)
        if response.is_error or "access_token" not in body:
            reason = body.get("error_description") or body.get("error") or response.status_code
            raise McpAuthError(f"Token request to {endpoint} failed: {reason}")
        token: str = body["access_token"]
        return token


def _protected_resource(http: httpx.Client, server_url: str, metadata_url: str | None) -> dict:
    """Fetch RFC 9728 metadata and check that it describes ``server_url``."""
    url = urlsplit(server_url)
    root = f"{url.scheme}://{url.netloc}/.well-known/oauth-protected-resource"
    candidates = [metadata_url] if metadata_url else []
    if url.path.strip("/"):
        candidates.append(root + url.path.rstrip("/"))
    candidates.append(root)
    metadata = _first(http, candidates)
    resource = metadata.get("resource")
    if resource is not None and not server_url.startswith(resource):
        raise McpAuthError(f"Protected resource {resource} does not match {server_url}")
    return metadata


def _authorization_server(http: httpx.Client, issuer: str) -> dict:
    """Fetch RFC 8414 or OpenID Connect metadata in the MCP priority order."""
    url = urlsplit(issuer)
    origin, path = f"{url.scheme}://{url.netloc}", url.path.rstrip("/")
    if path:
        candidates = [
            f"{origin}/.well-known/oauth-authorization-server{path}",
            f"{origin}/.well-known/openid-configuration{path}",
            f"{origin}{path}/.well-known/openid-configuration",
        ]
    else:
        candidates = [
            f"{origin}/.well-known/oauth-authorization-server",
            f"{origin}/.well-known/openid-configuration",
        ]
    metadata = _first(http, candidates)
    if metadata.get("issuer", "").rstrip("/") != issuer.rstrip("/"):
        raise McpAuthError(f"Authorization server metadata does not match issuer {issuer}")
    return metadata


def _first(http: httpx.Client, urls: list[str]) -> dict:
    for url in urls:
        _require_secure(url)
        response = http.get(url, headers={"Accept": "application/json"})
        if response.is_success:
            return _json(response)
    raise McpAuthError(f"No metadata at {', '.join(urls)}")


def _require_secure(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise McpAuthError(f"Authorization URL must use https: {url or '(missing)'}")


def _json(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
