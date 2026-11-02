from __future__ import annotations

import base64
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from cryptography.hazmat.primitives.asymmetric.types import PrivateKeyTypes
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from pydantic import ValidationError

from rio.coding.mcp import Mcp, McpServer
from rio.coding.mcp_oauth import (
    McpAuthError,
    McpOAuth,
    assertion,
    challenge,
    client_credentials_token,
)

SERVER = "https://mcp.test/mcp"
ISSUER = "https://auth.test/tenant"
OAUTH = McpOAuth(clientId="rio", clientSecret="s3cr&t")
Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def requests(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[httpx.Request]]:
    """Route every httpx.Client through ``routes``; record each request."""
    seen: list[httpx.Request] = []
    original = httpx.Client.__init__

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        handler = routes.get(str(request.url).split("?")[0])
        return handler(request) if handler else httpx.Response(404)

    def init(self: httpx.Client, **kwargs: object) -> None:
        original(self, transport=httpx.MockTransport(handle), **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx.Client, "__init__", init)
    routes.clear()
    yield seen


routes: dict[str, Handler] = {}


def _json(body: dict, status: int = 200) -> Handler:
    return lambda request: httpx.Response(status, json=body)


def _authorization_server(methods: list[str] | None = None, issuer: str = ISSUER) -> None:
    metadata: dict = {"issuer": issuer, "token_endpoint": "https://auth.test/token"}
    if methods is not None:
        metadata["token_endpoint_auth_methods_supported"] = methods
    # The path form of OpenID discovery is the second candidate; the first is missing.
    routes["https://auth.test/.well-known/openid-configuration/tenant"] = _json(metadata)
    routes["https://auth.test/token"] = _json({"access_token": "tok", "token_type": "Bearer"})


def _protected_resource(**extra: object) -> None:
    body = {"resource": SERVER, "authorization_servers": [ISSUER], **extra}
    routes["https://mcp.test/.well-known/oauth-protected-resource/mcp"] = _json(body)


def _both(methods: list[str] | None = None, issuer: str = ISSUER) -> None:
    _protected_resource()
    _authorization_server(methods, issuer)


def _form(request: httpx.Request) -> dict[str, str]:
    return {key: value[0] for key, value in parse_qs(request.content.decode()).items()}


def test_challenge_reads_quoted_and_bare_parameters() -> None:
    header = 'Bearer resource_metadata="https://x/m", scope="a b", error=invalid_token'
    assert challenge(header) == {
        "resource_metadata": "https://x/m",
        "scope": "a b",
        "error": "invalid_token",
    }


def test_token_request_follows_discovery_and_posts_secret(requests: list[httpx.Request]) -> None:
    _protected_resource(scopes_supported=["mcp:read", "mcp:write"])
    _authorization_server(["client_secret_basic", "client_secret_post"])

    assert client_credentials_token(SERVER, OAUTH, "Bearer") == "tok"

    assert [str(r.url) for r in requests] == [
        "https://mcp.test/.well-known/oauth-protected-resource/mcp",
        "https://auth.test/.well-known/oauth-authorization-server/tenant",
        "https://auth.test/.well-known/openid-configuration/tenant",
        "https://auth.test/token",
    ]
    assert _form(requests[-1]) == {
        "grant_type": "client_credentials",
        "resource": SERVER,
        "scope": "mcp:read mcp:write",
        "client_id": "rio",
        "client_secret": "s3cr&t",
    }
    assert "authorization" not in requests[-1].headers


def test_basic_authentication_and_scope_precedence(requests: list[httpx.Request]) -> None:
    routes["https://mcp.test/meta"] = _json({"resource": SERVER, "authorization_servers": [ISSUER]})
    _authorization_server(None)  # RFC 8414 defaults to client_secret_basic.
    header = 'Bearer resource_metadata="https://mcp.test/meta", scope="from:challenge"'

    client_credentials_token(SERVER, OAUTH, header)
    token_request = requests[-1]
    assert _form(token_request)["scope"] == "from:challenge"
    assert "client_secret" not in _form(token_request)
    expected = base64.b64encode(b"rio:s3cr%26t").decode()
    assert token_request.headers["authorization"] == f"Basic {expected}"

    client_credentials_token(SERVER, OAUTH.model_copy(update={"scope": "own"}), header)
    assert _form(requests[-1])["scope"] == "own"


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        (lambda: _protected_resource(resource="https://other.test/mcp"), "does not match"),
        (lambda: _protected_resource(authorization_servers=[]), "no authorization server"),
        (lambda: _both(issuer="https://evil"), "issuer"),
        (lambda: _both(["private_key_jwt"]), "secret"),
        (lambda: None, "No metadata"),
    ],
)
def test_discovery_failures(
    requests: list[httpx.Request], setup: Callable[[], object], message: str
) -> None:
    setup()
    with pytest.raises(McpAuthError, match=message):
        client_credentials_token(SERVER, OAUTH, "Bearer")


def test_token_errors_report_the_server_reason(requests: list[httpx.Request]) -> None:
    _protected_resource()
    _authorization_server()
    routes["https://auth.test/token"] = _json({"error": "invalid_client"}, 401)
    with pytest.raises(McpAuthError, match="invalid_client") as error:
        client_credentials_token(SERVER, OAUTH, "Bearer")
    assert "s3cr" not in str(error.value)


def test_plain_http_authorization_urls_are_refused(requests: list[httpx.Request]) -> None:
    header = 'Bearer resource_metadata="http://mcp.test/meta"'
    with pytest.raises(McpAuthError, match="https"):
        client_credentials_token(SERVER, OAUTH, header)
    assert requests == []


def test_server_401_gets_a_token_and_retries_once(requests: list[httpx.Request]) -> None:
    _protected_resource()
    _authorization_server()
    challenge = (
        'Bearer resource_metadata="https://mcp.test/.well-known/oauth-protected-resource/mcp"'
    )

    def mcp(request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != "Bearer tok":
            return httpx.Response(401, headers={"WWW-Authenticate": challenge})
        message = json.loads(request.content)
        if "id" not in message:
            return httpx.Response(202)
        result = {"content": [{"type": "text", "text": "ok"}]}
        return httpx.Response(200, json={"id": message["id"], "result": result})

    routes[SERVER] = mcp
    client = Mcp({"api": McpServer(url=SERVER, oauth=OAUTH)})
    assert client.call("api.ping") == "ok"
    client.close()
    assert sum(str(r.url) == "https://auth.test/token" for r in requests) == 1


def test_server_401_without_oauth_fails(requests: list[httpx.Request]) -> None:
    routes[SERVER] = lambda request: httpx.Response(401)
    client = Mcp({"api": McpServer(url=SERVER)})
    with pytest.raises(RuntimeError, match="HTTP 401"):
        client.call("api.ping")
    assert len(requests) == 1


def test_config_reads_oauth_aliases() -> None:
    server = McpServer.model_validate(
        {"url": SERVER, "oauth": {"clientId": "a", "clientSecret": "b", "scope": "c"}}
    )
    assert server.oauth == McpOAuth(clientId="a", clientSecret="b", scope="c")


def _key_file(tmp_path: Path, key: PrivateKeyTypes) -> str:
    path = tmp_path / "key.pem"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return str(path)


def _decode(part: str) -> dict:
    decoded: dict = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    return decoded


def _signature(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def test_private_key_jwt_posts_a_signed_assertion(
    requests: list[httpx.Request], tmp_path: Path
) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    oauth = McpOAuth(clientId="rio", privateKeyFile=_key_file(tmp_path, key), keyId="22")
    _protected_resource()
    metadata = {
        "issuer": ISSUER,
        "token_endpoint": "https://auth.test/token",
        "token_endpoint_auth_methods_supported": ["private_key_jwt", "client_secret_post"],
        "token_endpoint_auth_signing_alg_values_supported": ["RS256"],
    }
    routes["https://auth.test/.well-known/openid-configuration/tenant"] = _json(metadata)
    routes["https://auth.test/token"] = _json({"access_token": "tok"})

    assert client_credentials_token(SERVER, oauth, "Bearer") == "tok"

    form = _form(requests[-1])
    assert "client_id" not in form and "client_secret" not in form
    assert form["client_assertion_type"] == "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
    header, claims, signature = form["client_assertion"].split(".")
    assert _decode(header) == {"alg": "RS256", "typ": "JWT", "kid": "22"}
    payload = _decode(claims)
    assert payload["iss"] == payload["sub"] == "rio"
    assert payload["aud"] == "https://auth.test/token"
    assert payload["exp"] - payload["iat"] == 300 and payload["jti"]
    key.public_key().verify(
        _signature(signature), f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256()
    )


def test_ec_assertion_uses_a_raw_jose_signature(tmp_path: Path) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    oauth = McpOAuth(clientId="rio", privateKeyFile=_key_file(tmp_path, key))

    header, claims, signature = assertion(oauth, "https://auth.test/token", ["ES256"]).split(".")

    assert _decode(header) == {"alg": "ES256", "typ": "JWT"}
    raw = _signature(signature)
    assert len(raw) == 64
    der = encode_dss_signature(int.from_bytes(raw[:32]), int.from_bytes(raw[32:]))
    key.public_key().verify(der, f"{header}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))


@pytest.mark.parametrize(
    ("key", "algorithms", "message"),
    [
        (ec.generate_private_key(ec.SECP256R1()), ["RS256"], "does not accept ES256"),
        (ed25519.Ed25519PrivateKey.generate(), ["EdDSA"], "must be RSA or EC"),
    ],
)
def test_assertion_rejects_unusable_keys(
    tmp_path: Path, key: PrivateKeyTypes, algorithms: list[str], message: str
) -> None:
    oauth = McpOAuth(clientId="rio", privateKeyFile=_key_file(tmp_path, key))
    with pytest.raises(McpAuthError, match=message):
        assertion(oauth, "https://auth.test/token", algorithms)


def test_missing_key_file_is_an_auth_error(tmp_path: Path) -> None:
    oauth = McpOAuth(clientId="rio", privateKeyFile=str(tmp_path / "missing.pem"))
    with pytest.raises(McpAuthError, match="Cannot load private key"):
        assertion(oauth, "https://auth.test/token", ["RS256"])


def test_private_key_requires_server_support(requests: list[httpx.Request], tmp_path: Path) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    _both(["client_secret_post"])
    oauth = McpOAuth(clientId="rio", privateKeyFile=_key_file(tmp_path, key))
    with pytest.raises(McpAuthError, match="private_key_jwt"):
        client_credentials_token(SERVER, oauth, "Bearer")


@pytest.mark.parametrize(
    "extra", [{}, {"clientSecret": "s", "privateKeyFile": "k.pem"}], ids=["none", "both"]
)
def test_oauth_needs_exactly_one_credential(extra: dict) -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        McpOAuth.model_validate({"clientId": "rio", **extra})
