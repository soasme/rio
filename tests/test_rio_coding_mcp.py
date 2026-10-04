"""Tests for MCP support: configuration, the client, OAuth, the `mcp` object, and `rio mcp`."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import pytest

from rio.coding.mcp import prepare, probe, prompt_section
from rio.coding.mcp.client import McpError, iter_sse
from rio.coding.mcp.commands import AddOptions, McpCommandError, add, list_servers, login, logout
from rio.coding.mcp.commands import remove as remove_command
from rio.coding.mcp.config import (
    McpConfigError,
    McpServer,
    add_server,
    expand,
    load_mcp_config,
    remove_server,
    validate_server,
)
from rio.coding.mcp.connection import create_client
from rio.coding.mcp.kernel import Mcp, McpToolError, host_environ, startup_code, tool_result
from rio.coding.mcp.oauth import McpAuthStore, parse_www_authenticate, select_resource
from rio.coding.paths import RioPaths
from rio.coding.project_trust import CanonicalProjectPath, ProtectedResourceDetector

FAKE_SERVER = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"
STDIO = {"command": sys.executable, "args": [str(FAKE_SERVER)]}


@pytest.fixture
def paths(tmp_path) -> RioPaths:
    return RioPaths(home=tmp_path / "home" / ".rio", agents_home=tmp_path / "home" / ".agents")


@pytest.fixture
def repo(tmp_path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    return path


def write(path: Path, servers: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}))


# -- configuration -----------------------------------------------------------------


class TestConfig:
    def test_project_entries_replace_global_ones_when_trusted(self, paths, repo):
        write(paths.mcp_config_path, {"a": {"url": "https://a/mcp"}, "b": STDIO})
        write(paths.project_mcp_config_path(repo), {"a": STDIO})
        config = load_mcp_config(repo, project_trusted=True, paths=paths)
        assert [(s.name, s.scope) for s in config.servers] == [("a", "project"), ("b", "global")]
        assert config.get("a").config == STDIO

    def test_untrusted_project_file_is_ignored_and_reported(self, paths, repo):
        write(paths.project_mcp_config_path(repo), {"a": STDIO})
        config = load_mcp_config(repo, project_trusted=False, paths=paths)
        assert config.servers == ()
        assert config.ignored_project_file == paths.project_mcp_config_path(repo)

    def test_project_can_disable_a_global_server(self, paths, repo):
        write(paths.mcp_config_path, {"a": {"url": "https://a/mcp", "headers": {"X": "1"}}})
        write(paths.project_mcp_config_path(repo), {"a": {"enabled": False}})
        config = load_mcp_config(repo, project_trusted=True, paths=paths)
        server = config.get("a")
        assert not server.enabled and server.config["headers"] == {"X": "1"}
        assert server.scope == "global" and server.override is not None
        assert config.enabled == ()

    def test_override_rules(self, paths, repo):
        write(paths.mcp_config_path, {"a": STDIO})
        write(
            paths.project_mcp_config_path(repo),
            {"a": {"enabled": False, "timeout": 5}, "missing": {"enabled": False}},
        )
        config = load_mcp_config(repo, project_trusted=True, paths=paths)
        assert len(config.errors) == 2
        assert config.get("a").enabled

    def test_invalid_entries_are_reported_and_skipped(self, paths, repo):
        write(
            paths.mcp_config_path,
            {
                "a-b": STDIO,
                "bad name": STDIO,
                "sse": {"type": "sse", "url": "https://x"},
                "neither": {},
                "a_b": STDIO,
            },
        )
        config = load_mcp_config(repo, project_trusted=True, paths=paths)
        assert [s.name for s in config.servers] == ["a-b"]
        assert len(config.errors) == 4
        assert any("conflicts" in error for error in config.errors)

    def test_broken_json_is_an_error(self, paths, repo):
        paths.mcp_config_path.parent.mkdir(parents=True)
        paths.mcp_config_path.write_text("{")
        config = load_mcp_config(repo, project_trusted=True, paths=paths)
        assert config.servers == () and len(config.errors) == 1

    @pytest.mark.parametrize(
        "entry",
        [
            {"url": "ftp://x"},
            {"url": "https://x", "headers": {"a": 1}},
            {"url": "https://x", "oauth": {"callbackPort": 0}},
            {"url": "https://x", "oauth": {"bogus": 1}},
            {"command": "x", "args": "y"},
            {"command": "x", "timeout": -1},
            {"command": "x", "enabled": "no"},
        ],
    )
    def test_validation(self, entry):
        with pytest.raises(McpConfigError):
            validate_server("s", entry)

    def test_expand(self):
        env = {"A": "1"}
        assert expand("x${A}y${B:-2}", env) == "x1y2"
        with pytest.raises(McpConfigError, match="B"):
            expand("${B}", env)

    def test_add_and_remove_keep_other_content(self, tmp_path):
        path = tmp_path / "mcp.json"
        path.write_text(json.dumps({"other": 1, "mcpServers": {"a": STDIO}}))
        assert add_server(path, "b", {"url": "https://b"}) is False
        assert add_server(path, "b", {"url": "https://c"}) is True
        assert remove_server(path, "a") is True
        assert remove_server(path, "a") is False
        assert json.loads(path.read_text()) == {
            "other": 1,
            "mcpServers": {"b": {"url": "https://c"}},
        }

    def test_project_mcp_json_needs_trust(self, repo):
        write(repo / ".rio" / "mcp.json", {"a": STDIO})
        summary = ProtectedResourceDetector().detect(CanonicalProjectPath(repo))
        assert "mcp" in summary.categories


# -- stdio client and the `mcp` object -------------------------------------------------


def stdio_server(tmp_path, **extra) -> McpServer:
    return McpServer("fake-srv", {**STDIO, **extra}, tmp_path / "mcp.json", "global")


class TestStdio:
    def test_client(self, tmp_path):
        store = McpAuthStore(tmp_path / "auth.json")
        client = create_client("fake", STDIO, tmp_path, store)
        try:
            client.connect()
            assert client.instructions == "Use add for sums."
            assert [tool["name"] for tool in client.list_tools()] == [
                "add",
                "echo-text",
                "fail",
                "roots",
                "venv",
            ]
            result = client.call_tool("add", {"a": 1, "b": 2})
            assert result["structuredContent"] == {"sum": 3}
            roots = json.loads(client.call_tool("roots", {})["content"][0]["text"])
            assert roots == {"roots": [{"uri": tmp_path.resolve().as_uri(), "name": tmp_path.name}]}
            with pytest.raises(McpError, match="nope"):
                client.request("unknown/method")
        finally:
            client.close()

    def test_missing_command_fails_cleanly(self, tmp_path):
        status = probe(
            McpServer("x", {"command": str(tmp_path / "nope")}, tmp_path, "global"),
            tmp_path,
            McpAuthStore(tmp_path / "auth.json"),
        )
        assert not status.connected and "could not start" in status.error

    def test_mcp_object(self, tmp_path):
        mcp = Mcp({"fake-srv": STDIO}, tmp_path, McpAuthStore(tmp_path / "auth.json"))
        try:
            assert "fake_srv" in dir(mcp)
            server = mcp.fake_srv
            assert server is mcp["fake-srv"]
            assert server.add(a=2, b=3) == {"sum": 5}
            assert server.echo_text(text="hi") == "hi"
            assert server.call("echo-text", {"text": "yo"}) == "yo"
            with pytest.raises(McpToolError, match="boom"):
                server.fail()
            with pytest.raises(AttributeError):
                server.nope  # noqa: B018
            assert "Add two numbers." in server.add.__doc__
            assert "a: number (required)" in server.add.__doc__
            assert str(server.add.__signature__) == "(*, a: float, b: float)"
            assert server.read_resource("mem://a") == "hello"
            assert server.resources()[0]["uri"] == "mem://a"
            assert server.get_prompt("greet", who="bob")[0]["content"]["text"] == "hi bob"
            assert "add, echo_text" in repr(server)
        finally:
            mcp.close()

    def test_tool_result(self):
        text = {"type": "text", "text": "a"}
        image = {"type": "image", "data": "x", "mimeType": "image/png"}
        assert tool_result({"content": [text, text]}) == "a\na"
        assert tool_result({"content": [text, image]}) == [text, image]
        assert tool_result({"content": [], "structuredContent": {"k": 1}}) == {"k": 1}
        with pytest.raises(McpToolError):
            tool_result({"content": [text], "isError": True})

    def test_startup_code_installs_mcp(self, tmp_path):
        namespace: dict = {}

        class IPython:
            def push(self, values, interactive):
                namespace.update(values)

        code = startup_code({"fake": STDIO}, tmp_path, tmp_path / "auth.json")
        exec(code, {"get_ipython": IPython})
        assert isinstance(namespace["mcp"], Mcp)
        assert namespace["McpToolError"] is McpToolError

    def test_prepare_does_not_connect(self, paths, repo):
        # A server that never answers must not hold up session start.
        hang = {"command": sys.executable, "args": ["-c", "import time; time.sleep(60)"]}
        write(paths.mcp_config_path, {"fake-srv": {**hang, "description": "Math."}})
        started = time.monotonic()
        setup = prepare(repo, project_trusted=True, paths=paths)
        assert time.monotonic() - started < 1
        assert "- `fake_srv`: Math." in setup.section.body
        assert "print(mcp.<server>)" in setup.section.body
        assert "_rio_mcp_install" in setup.startup

    def test_prepare_without_servers(self, paths, repo):
        assert prepare(repo, project_trusted=True, paths=paths).section is None

    def test_prompt_section_reports_unset_variables(self, tmp_path, monkeypatch):
        monkeypatch.delenv("RIO_TEST_UNSET", raising=False)
        server = stdio_server(tmp_path, env={"TOKEN": "${RIO_TEST_UNSET}"})
        section = prompt_section((server,))
        assert "(unavailable: environment variable RIO_TEST_UNSET is not set)" in section.body

    def test_servers_connect_in_the_background(self, tmp_path):
        mcp = Mcp({"fake-srv": STDIO}, tmp_path, McpAuthStore(tmp_path / "auth.json"))
        try:
            mcp.start()
            server = mcp.fake_srv
            assert server._ready.wait(10)
            assert server._tools is not None  # listed before any cell asked
            assert "add, echo_text" in repr(server)
            assert "Use add for sums." in repr(server)
        finally:
            mcp.close()

    def test_a_failed_background_connection_is_retried_on_use(self, tmp_path):
        script = tmp_path / "server.py"
        mcp = Mcp(
            {"late": {"command": sys.executable, "args": [str(script)]}},
            tmp_path,
            McpAuthStore(tmp_path / "auth.json"),
        )
        try:
            mcp.start()
            assert mcp.late._ready.wait(10) and mcp.late._tools is None
            script.write_text(FAKE_SERVER.read_text())
            assert mcp.late.add(a=1, b=1) == {"sum": 2}
        finally:
            mcp.close()

    def test_host_environ_drops_the_session_venv(self):
        venv = "/tmp/rio-session-x"
        env = host_environ(
            {
                "VIRTUAL_ENV": venv,
                "PYTHONNOUSERSITE": "1",
                "PATH": f"{venv}/bin{os.pathsep}/usr/bin",
                "HOME": "/h",
            }
        )
        assert env == {"PATH": "/usr/bin", "HOME": "/h"}


# -- streamable HTTP and OAuth -------------------------------------------------------


class FakeHttpServer:
    """An MCP server behind OAuth, which is also its own authorization server."""

    def __init__(self) -> None:
        self.tokens = {"at1"}
        self.require_auth = True
        self.expire_session = False
        self.sessions_deleted = 0
        self.challenge: str | None = None
        self.refreshes = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, body=None, headers=None, kind="application/json"):
                data = (
                    b""
                    if body is None
                    else (body if isinstance(body, bytes) else json.dumps(body).encode())
                )
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                if data:
                    self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                base = fake.base
                if self.path == "/.well-known/oauth-protected-resource/mcp":
                    return self.reply(
                        200, {"resource": f"{base}/mcp", "authorization_servers": [base]}
                    )
                if self.path == "/.well-known/oauth-authorization-server":
                    return self.reply(
                        200,
                        {
                            "issuer": base,
                            "authorization_endpoint": f"{base}/authorize",
                            "token_endpoint": f"{base}/token",
                            "registration_endpoint": f"{base}/register",
                            "response_types_supported": ["code"],
                            "code_challenge_methods_supported": ["S256"],
                        },
                    )
                self.reply(404)

            def do_DELETE(self):
                fake.sessions_deleted += 1
                self.reply(200)

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if self.path == "/register":
                    data = json.loads(body)
                    return self.reply(201, {"client_id": "c1", **data})
                if self.path == "/token":
                    return fake.token(self, parse_qs(body.decode()))
                token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
                if fake.require_auth and token not in fake.tokens:
                    metadata = f"{fake.base}/.well-known/oauth-protected-resource/mcp"
                    challenge = f'Bearer resource_metadata="{metadata}"'
                    return self.reply(401, headers={"WWW-Authenticate": challenge})
                message = json.loads(body)
                if "method" not in message or "id" not in message:
                    return self.reply(202)
                if self.headers.get("Mcp-Session-Id") and fake.expire_session:
                    fake.expire_session = False
                    return self.reply(404)
                return fake.handle(self, message)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.url = f"{self.base}/mcp"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def handle(self, handler, message):
        method = message["method"]
        if method == "initialize":
            assert handler.headers.get("MCP-Protocol-Version") is None
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "http", "version": "1"},
            }
            return handler.reply(
                200,
                {"jsonrpc": "2.0", "id": message["id"], "result": result},
                {"Mcp-Session-Id": "s1"},
            )
        assert handler.headers.get("MCP-Protocol-Version") == "2025-06-18"
        assert handler.headers.get("Mcp-Session-Id") == "s1"
        if method == "tools/list":
            tools = [{"name": "hello", "inputSchema": {"type": "object"}}]
            events = [
                {"jsonrpc": "2.0", "id": "p1", "method": "ping"},
                {"jsonrpc": "2.0", "method": "notifications/message", "params": {}},
                {"jsonrpc": "2.0", "id": message["id"], "result": {"tools": tools}},
            ]
            stream = "".join(f"event: message\ndata: {json.dumps(e)}\n\n" for e in events)
            return handler.reply(200, (": comment\n" + stream).encode(), kind="text/event-stream")
        if method == "tools/call":
            content = [{"type": "text", "text": "hello from http"}]
            return handler.reply(
                200, {"jsonrpc": "2.0", "id": message["id"], "result": {"content": content}}
            )
        handler.reply(
            200, {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "x"}}
        )

    def token(self, handler, form):
        form = {key: values[0] for key, values in form.items()}
        if form["grant_type"] == "authorization_code":
            digest = hashlib.sha256(form["code_verifier"].encode()).digest()
            challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
            if form["code"] != "code1" or challenge != self.challenge:
                return handler.reply(400, {"error": "invalid_grant"})
            assert form["resource"] == self.url and form["client_id"] == "c1"
            self.tokens = {"at1"}
            return handler.reply(
                200,
                {
                    "access_token": "at1",
                    "token_type": "Bearer",
                    "refresh_token": "rt1",
                    "expires_in": 3600,
                },
            )
        if form["grant_type"] == "refresh_token" and form["refresh_token"] == "rt1":
            self.refreshes += 1
            self.tokens = {"at2"}
            return handler.reply(200, {"access_token": "at2", "token_type": "Bearer"})
        handler.reply(400, {"error": "invalid_grant"})

    def authorize(self, url: str) -> None:
        """Play the user approving access in the browser."""
        query = {key: values[0] for key, values in parse_qs(urlparse(url).query).items()}
        assert url.startswith(f"{self.base}/authorize?")
        assert query["code_challenge_method"] == "S256" and query["resource"] == self.url
        self.challenge = query["code_challenge"]
        callback = (
            f"{query['redirect_uri']}?{urlencode({'code': 'code1', 'state': query['state']})}"
        )
        assert httpx.get(callback).status_code == 200

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def http_server():
    server = FakeHttpServer()
    yield server
    server.close()


class TestHttp:
    def test_sse_parsing(self):
        lines = ["event: message", "data: a", "data: b", "", ": c", "data: d"]
        assert list(iter_sse(iter(lines))) == [("message", "a\nb"), (None, "d")]

    def test_session_and_sse(self, http_server, tmp_path):
        http_server.require_auth = False
        client = create_client(
            "h", {"url": http_server.url}, tmp_path, McpAuthStore(tmp_path / "a")
        )
        client.connect()
        assert [tool["name"] for tool in client.list_tools()] == ["hello"]
        http_server.expire_session = True
        assert tool_result(client.call_tool("hello", {})) == "hello from http"
        client.close()
        assert http_server.sessions_deleted == 1

    def test_oauth_login_refresh_logout(self, http_server, paths, repo):
        write(paths.mcp_config_path, {"h": {"url": http_server.url}})
        text, ok = list_servers(repo, paths=paths)
        assert not ok and "sign in with: rio mcp login h" in text

        message = login("h", repo, paths=paths, open_url=http_server.authorize, log=lambda _: None)
        assert message == 'Signed in to MCP server "h" (1 tools).'
        stored = json.loads(paths.mcp_auth_path.read_text())["h"]
        assert stored["client"]["client_id"] == "c1" and stored["tokens"]["access_token"] == "at1"
        assert paths.mcp_auth_path.stat().st_mode & 0o777 == 0o600

        # The server revokes the token: the client refreshes it and retries.
        http_server.tokens = set()
        mcp = Mcp({"h": {"url": http_server.url}}, repo, McpAuthStore(paths.mcp_auth_path))
        http_server.tokens = {"stale"}
        assert mcp.h.hello() == "hello from http"
        assert http_server.refreshes == 1
        mcp.close()

        assert logout("h", repo, paths=paths) == 'Signed out of MCP server "h".'
        assert logout("h", repo, paths=paths) == 'No stored credentials for MCP server "h".'

    def test_headers_disable_oauth(self, http_server, paths, repo):
        write(
            paths.mcp_config_path,
            {"h": {"url": http_server.url, "headers": {"Authorization": "x"}}},
        )
        with pytest.raises(McpCommandError, match="does not use OAuth"):
            login("h", repo, paths=paths)

    def test_www_authenticate(self):
        header = 'Bearer error="insufficient_scope", scope="a b", resource_metadata="https://x/m"'
        assert parse_www_authenticate(header) == {
            "resource_metadata": "https://x/m",
            "scope": "a b",
            "error": "insufficient_scope",
        }
        assert parse_www_authenticate("Basic realm=x") == {}

    def test_select_resource(self):
        assert select_resource("https://a/mcp/x", {"resource": "https://a/mcp"}) == "https://a/mcp"
        with pytest.raises(Exception, match="does not match"):
            select_resource("https://a/other", {"resource": "https://a/mcp"})
        with pytest.raises(Exception, match="does not match"):
            select_resource("https://b/mcp", {"resource": "https://a/mcp"})


# -- rio mcp add/remove ----------------------------------------------------------------


class TestCommands:
    def test_add_stdio_and_http(self, paths, repo):
        add(AddOptions("fs", command=("npx", "-y", "fs"), env=("A=1",)), repo, paths)
        add(
            AddOptions(
                "docs",
                url="https://d/mcp",
                local=True,
                headers=("X=1",),
                oauth={"clientId": "c"},
            ),
            repo,
            paths,
        )
        assert json.loads(paths.mcp_config_path.read_text())["mcpServers"]["fs"] == {
            "command": "npx",
            "args": ["-y", "fs"],
            "env": {"A": "1"},
        }
        project = json.loads(paths.project_mcp_config_path(repo).read_text())
        assert project["mcpServers"]["docs"]["oauth"] == {"clientId": "c"}

    @pytest.mark.parametrize(
        "options",
        [
            AddOptions("x"),
            AddOptions("x", command=("a",), url="https://x"),
            AddOptions("x", command=("a",), headers=("A=1",)),
            AddOptions("x", url="https://x", env=("A=1",)),
            AddOptions("x", command=("a",), env=("novalue",)),
            AddOptions("bad name", command=("a",)),
        ],
    )
    def test_add_rejects(self, options, paths, repo):
        with pytest.raises(McpCommandError):
            add(options, repo, paths)

    def test_remove_points_at_the_other_file(self, paths, repo):
        add(AddOptions("fs", command=("a",), local=True), repo, paths)
        with pytest.raises(McpCommandError, match="use --local"):
            remove_command("fs", local=False, cwd=repo, paths=paths)
        assert "Removed project" in remove_command("fs", local=True, cwd=repo, paths=paths)

    def test_list_json(self, paths, repo):
        write(paths.mcp_config_path, {"fake": STDIO, "off": {**STDIO, "enabled": False}})
        text, ok = list_servers(repo, as_json=True, paths=paths)
        data = json.loads(text)
        assert ok
        assert [(s["name"], s["state"]) for s in data["servers"]] == [
            ("fake", "connected"),
            ("off", "disabled"),
        ]
        assert data["servers"][0]["tools"] == ["add", "echo-text", "fail", "roots", "venv"]


# -- a real kernel -----------------------------------------------------------------------


async def test_cells_call_tools_through_mcp_in_the_kernel(tmp_path):
    from conftest import add_code
    from rio.agent import KernelExecutor, apply_patch, new_notebook

    startup = startup_code({"fake-srv": STDIO}, tmp_path, tmp_path / "auth.json")
    kernel = KernelExecutor(tmp_path, startup=startup)
    try:
        notebook = apply_patch(
            new_notebook(),
            [add_code("total = mcp.fake_srv.add(a=1, b=2)\nprint(total, mcp.fake_srv.venv())")],
        )
        result = await kernel(notebook, [0])
    finally:
        await kernel.shutdown()
    cell = result["cells"][0]
    assert "".join(output.get("text", "") for output in cell["outputs"]) == "{'sum': 3} none\n"
    assert cell["metadata"]["rio"]["defines"] == ["total"]
