from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from rio.coding import codemode
from rio.coding.mcp import Mcp, McpError, McpServer, load_servers

SERVER = str(Path(__file__).parent / "fixtures" / "mcp_stdio_server.py")


def _write(path: Path, servers: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def test_project_entry_replaces_global_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOKEN", "secret")
    home, cwd = tmp_path / "home", tmp_path / "project"
    _write(home / "mcp.json", {"a": {"command": "global"}, "b": {"url": "https://b/mcp"}})
    _write(cwd / ".mcp.json", {"a": {"command": "local", "env": {"T": "${TOKEN}"}}})

    servers = load_servers(cwd, home)

    assert servers == {
        "a": McpServer(command="local", env={"T": "secret"}),
        "b": McpServer(url="https://b/mcp"),
    }


def test_missing_files_mean_no_servers(tmp_path: Path) -> None:
    assert load_servers(tmp_path, tmp_path) == {}


def test_malformed_config_names_the_file(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match=".mcp.json"):
        load_servers(tmp_path, tmp_path / "home")


@pytest.fixture
def mcp() -> Iterator[Mcp]:
    client = Mcp({"fx": McpServer(command=sys.executable, args=[SERVER])})
    yield client
    client.close()


def test_tools_match_every_query_word(mcp: Mcp) -> None:
    assert [tool["name"] for tool in mcp.tools()] == ["fx.echo", "fx.add", "fx.fail"]
    assert mcp.tools("fx numbers") == [
        {"name": "fx.add", "description": "Add two numbers.", "input_schema": {"type": "object"}}
    ]


def test_call_returns_structured_content_then_text(mcp: Mcp) -> None:
    assert mcp.call("fx.add", {"a": 1, "b": 2}) == {"sum": 3}
    assert mcp.call("fx.echo", {"text": "hi"}) == "hi"
    assert len(mcp.connections) == 1


def test_call_errors(mcp: Mcp) -> None:
    with pytest.raises(McpError, match="boom"):
        mcp.call("fx.fail")
    with pytest.raises(McpError, match="server.tool"):
        mcp.call("echo")
    with pytest.raises(McpError, match="Unknown MCP server: nope"):
        mcp.call("nope.echo")


def test_http_server_keeps_session_and_reads_event_stream() -> None:
    seen: list[tuple[str | None, str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        message = json.loads(request.content)
        seen.append((request.headers.get("mcp-session-id"), message["method"]))
        if "id" not in message:
            return httpx.Response(202)
        if message["method"] == "initialize":
            reply = {"jsonrpc": "2.0", "id": message["id"], "result": {}}
            return httpx.Response(200, json=reply, headers={"Mcp-Session-Id": "s1"})
        text = {"content": [{"type": "text", "text": "pong"}]}
        body = f"event: message\ndata: {json.dumps({'id': message['id'], 'result': text})}\n\n"
        return httpx.Response(200, text=body, headers={"Content-Type": "text/event-stream"})

    client = Mcp({"web": McpServer(url="https://example.test/mcp", headers={"X": "1"})})
    transport = httpx.MockTransport(handle)
    original = httpx.Client.__init__

    def init(self: httpx.Client, **kwargs: object) -> None:
        original(self, transport=transport, **kwargs)  # type: ignore[arg-type]

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(httpx.Client, "__init__", init)
        assert client.call("web.ping") == "pong"
    client.close()
    assert seen == [
        (None, "initialize"),
        ("s1", "notifications/initialized"),
        ("s1", "tools/call"),
    ]


def test_code_cell_calls_mcp_builtins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _write(tmp_path / ".mcp.json", {"fx": {"command": sys.executable, "args": [SERVER]}})
    source = """print([tool["name"] for tool in mcp_tools("echo")])
print(mcp_call("fx.add", {"a": 2, "b": 3}))
try:
    mcp_call("fx.fail")
except RuntimeError as exc:
    print("error:", exc)
"""

    assert codemode.run(source, "cell.py") == 0

    assert capsys.readouterr().out == "['fx.echo']\n{'sum': 5}\nerror: boom\n"
