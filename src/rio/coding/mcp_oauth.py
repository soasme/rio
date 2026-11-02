"""MCP OAuth Client Credentials extension: get a token without a browser.

Follows the MCP authorization discovery (RFC 9728 protected resource metadata, then
RFC 8414 or OpenID Connect authorization server metadata) and requests a token with
``grant_type=client_credentials`` and an RFC 8707 ``resource``. The client authenticates
with an RFC 7523 ``private_key_jwt`` assertion or a client secret.
"""

from __future__ import annotations

import base64
import json
import re
import time
import uuid
from pathlib import Path
from urllib.parse import quote_plus, urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from pydantic import BaseModel, ConfigDict, Field, model_validator

_PARAM = re.compile(r'(\w+)=(?:"([^"]*)"|([^\s,]+))')


class McpOAuth(BaseModel):
    """Client credentials for one HTTP server: a private key or a client secret."""

    model_config = ConfigDict(extra="ignore")

    client_id: str = Field(alias="clientId")
    client_secret: str | None = Field(default=None, alias="clientSecret")
    private_key_file: str | None = Field(default=None, alias="privateKeyFile")
    key_id: str | None = Field(default=None, alias="keyId")
    scope: str | None = None

    @model_validator(mode="after")
    def _one_credential(self) -> McpOAuth:
        if (self.client_secret is None) == (self.private_key_file is None):
            raise ValueError("oauth needs exactly one of clientSecret or privateKeyFile")
        return self


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
        endpoint = metadata.get("token_endpoint", "")
        _require_secure(endpoint)
        headers = {}
        if oauth.private_key_file is not None:
            if "private_key_jwt" not in methods:
                raise McpAuthError(f"{servers[0]} does not support private_key_jwt")
            algorithms = metadata.get("token_endpoint_auth_signing_alg_values_supported", [])
            form |= {
                "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                "client_assertion": assertion(oauth, endpoint, algorithms),
            }
        elif "client_secret_post" in methods:
            form |= {"client_id": oauth.client_id, "client_secret": oauth.client_secret or ""}
        elif "client_secret_basic" in methods:
            user = f"{quote_plus(oauth.client_id)}:{quote_plus(oauth.client_secret or '')}"
            headers["Authorization"] = "Basic " + base64.b64encode(user.encode()).decode()
        else:
            raise McpAuthError(f"{servers[0]} supports no client secret authentication")
        response = http.post(endpoint, data=form, headers=headers)
        body = _json(response)
        if response.is_error or "access_token" not in body:
            reason = body.get("error_description") or body.get("error") or response.status_code
            raise McpAuthError(f"Token request to {endpoint} failed: {reason}")
        token: str = body["access_token"]
        return token


def assertion(oauth: McpOAuth, audience: str, algorithms: list[str]) -> str:
    """Return an RFC 7523 client assertion signed with ``oauth.private_key_file``."""
    assert oauth.private_key_file is not None
    path = Path(oauth.private_key_file).expanduser()
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as exc:
        raise McpAuthError(f"Cannot load private key {path}: {exc}") from exc
    digest: hashes.HashAlgorithm
    if isinstance(key, rsa.RSAPrivateKey):
        algorithm, digest = "RS256", hashes.SHA256()
    elif isinstance(key, ec.EllipticCurvePrivateKey) and key.curve.name in _CURVES:
        algorithm, digest = _CURVES[key.curve.name]
    else:
        raise McpAuthError(f"Private key {path} must be RSA or EC P-256, P-384, P-521")
    if algorithm not in algorithms:
        raise McpAuthError(f"Authorization server does not accept {algorithm} assertions")
    header = {"alg": algorithm, "typ": "JWT"} | ({"kid": oauth.key_id} if oauth.key_id else {})
    now = int(time.time())
    claims = {
        "iss": oauth.client_id,
        "sub": oauth.client_id,
        "aud": audience,
        "iat": now,
        "exp": now + 300,
        "jti": str(uuid.uuid4()),
    }
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}"
    if isinstance(key, rsa.RSAPrivateKey):
        signature = key.sign(signing_input.encode(), padding.PKCS1v15(), digest)
    else:
        r, s = decode_dss_signature(key.sign(signing_input.encode(), ec.ECDSA(digest)))
        size = (key.curve.key_size + 7) // 8
        signature = r.to_bytes(size, "big") + s.to_bytes(size, "big")
    return f"{signing_input}.{_b64(signature)}"


_CURVES: dict[str, tuple[str, hashes.HashAlgorithm]] = {
    "secp256r1": ("ES256", hashes.SHA256()),
    "secp384r1": ("ES384", hashes.SHA384()),
    "secp521r1": ("ES512", hashes.SHA512()),
}


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


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
