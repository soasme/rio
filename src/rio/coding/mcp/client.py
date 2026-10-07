"""A synchronous MCP client over stdio and streamable HTTP.

Synchronous so Python scripts can call tools without `await`. The stdio
transport reads the server's stdout on a thread; the HTTP transport reads
each response (JSON or an SSE stream) inline. Server-to-client requests are
answered: `ping`, and `roots/list` with the session's working directory.
The optional server-to-client GET stream is not opened.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import os
import subprocess
import threading
from collections.abc import Callable, Iterator
from typing import Any, Protocol

import httpx

from rio.version import current_version

LATEST_PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = (LATEST_PROTOCOL_VERSION, "2025-06-18", "2025-03-26", "2024-11-05")
METHOD_NOT_FOUND = -32601
MAX_LIST_PAGES = 1000
STDERR_TAIL_BYTES = 8 * 1024

Message = dict[str, Any]
RequestHandler = Callable[[str, Any], Any]


class McpError(Exception):
    """A JSON-RPC error the server answered with."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


class McpConnectionError(RuntimeError):
    """The server could not be reached, closed the connection, or broke the protocol."""


class McpTimeoutError(McpConnectionError):
    pass


class McpAuthRequiredError(McpConnectionError):
    """The server needs (new) OAuth authorization."""

    def __init__(self, message: str, www_authenticate: str | None = None) -> None:
        super().__init__(message)
        self.www_authenticate = www_authenticate


class McpSessionExpiredError(McpConnectionError):
    pass


class Transport(Protocol):
    def start(self) -> None: ...
    def request(self, message: Message, timeout: float) -> Message: ...
    def notify(self, message: Message) -> None: ...
    def set_protocol_version(self, version: str) -> None: ...
    def close(self) -> None: ...


class Auth(Protocol):
    """Bearer tokens for an HTTP server, and what to do when the server rejects one."""

    def token(self) -> str | None: ...
    def on_unauthorized(self, response: httpx.Response, sent: str | None) -> None: ...


def _response_error(message: Message) -> McpError | None:
    error = message.get("error")
    if error is None:
        return None
    if not isinstance(error, dict):
        return McpError(-32603, str(error))
    return McpError(error.get("code", -32603), str(error.get("message", "")), error.get("data"))


def _answer(handler: RequestHandler, message: Message) -> Message:
    try:
        result = handler(message["method"], message.get("params"))
    except McpError as exc:
        error = {"code": exc.code, "message": str(exc)}
        return {"jsonrpc": "2.0", "id": message["id"], "error": error}
    except Exception as exc:  # noqa: BLE001 - reported to the server
        error = {"code": -32603, "message": str(exc)}
        return {"jsonrpc": "2.0", "id": message["id"], "error": error}
    return {"jsonrpc": "2.0", "id": message["id"], "result": result if result is not None else {}}


def _is_request(message: Message) -> bool:
    return "method" in message and "id" in message


def _is_response(message: Message) -> bool:
    return "method" not in message and "id" in message


# -- stdio ---------------------------------------------------------------------


class StdioTransport:
    """Newline-delimited JSON-RPC over a child process's stdin and stdout."""

    def __init__(
        self,
        command: str,
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        base_env: dict[str, str] | None = None,
        cwd: str | None = None,
        handle_request: RequestHandler,
    ) -> None:
        self._argv = [command, *args]
        self._env = {**(os.environ if base_env is None else base_env), **(env or {})}
        self._cwd = cwd
        self._handle_request = handle_request
        self._process: subprocess.Popen[bytes] | None = None
        self._pending: dict[Any, tuple[threading.Event, list[Message]]] = {}
        self._write_lock = threading.Lock()
        self._stderr = bytearray()
        self._closed = threading.Event()

    def start(self) -> None:
        try:
            self._process = subprocess.Popen(
                self._argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._env,
                cwd=self._cwd,
            )
        except OSError as exc:
            raise McpConnectionError(f"could not start {self._argv[0]}: {exc}") from exc
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def set_protocol_version(self, version: str) -> None:
        pass

    def request(self, message: Message, timeout: float) -> Message:
        event, slot = threading.Event(), []
        self._pending[message["id"]] = (event, slot)
        try:
            self._write(message)
            if not event.wait(timeout):
                raise McpTimeoutError(f"no response to {message['method']} in {timeout:g}s")
        finally:
            self._pending.pop(message["id"], None)
        if not slot:
            raise McpConnectionError(f"server exited{self._stderr_tail()}")
        return slot[0]

    def notify(self, message: Message) -> None:
        self._write(message)

    def close(self) -> None:
        process = self._process
        if process is None or self._closed.is_set() and process.poll() is not None:
            return
        # Shutdown per the spec: close stdin, then SIGTERM, then SIGKILL.
        try:
            assert process.stdin is not None
            process.stdin.close()
        except OSError:
            pass
        for signal in (None, process.terminate, process.kill):
            if signal is not None:
                signal()
            try:
                process.wait(timeout=1)
                return
            except subprocess.TimeoutExpired:
                continue

    def _write(self, message: Message) -> None:
        process = self._process
        if process is None or self._closed.is_set():
            raise McpConnectionError(f"server is not running{self._stderr_tail()}")
        data = (json.dumps(message) + "\n").encode()
        try:
            with self._write_lock:
                assert process.stdin is not None
                process.stdin.write(data)
                process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise McpConnectionError(f"server closed its input{self._stderr_tail()}") from exc

    def _read_stdout(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        for line in self._process.stdout:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue  # Servers sometimes log to stdout; skip what is not JSON-RPC.
            if not isinstance(message, dict):
                continue
            if _is_response(message):
                pending = self._pending.get(message["id"])
                if pending is not None:
                    pending[1].append(message)
                    pending[0].set()
            elif _is_request(message):
                try:
                    self._write(_answer(self._handle_request, message))
                except McpConnectionError:
                    break
        self._closed.set()
        for event, _ in list(self._pending.values()):
            event.set()

    def _read_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        for chunk in iter(lambda: self._process.stderr.read1(4096), b""):  # type: ignore[union-attr]
            self._stderr.extend(chunk)
            del self._stderr[:-STDERR_TAIL_BYTES]

    def _stderr_tail(self) -> str:
        text = self._stderr.decode(errors="replace").strip()
        return f"\n{text[-2000:]}" if text else ""


# -- streamable HTTP -------------------------------------------------------------


def iter_sse(lines: Iterator[str]) -> Iterator[tuple[str | None, str]]:
    """Yield `(event, data)` for each server-sent event with data."""
    event: str | None = None
    data: list[str] = []
    for raw in lines:
        line = raw.rstrip("\r")
        if line == "":
            if data:
                yield event, "\n".join(data)
            event, data = None, []
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if name == "data":
            data.append(value)
        elif name == "event":
            event = value
    if data:
        yield event, "\n".join(data)


def _needs_authorization(response: httpx.Response) -> bool:
    if response.status_code == 401:
        return True
    challenge = response.headers.get("www-authenticate", "")
    return response.status_code == 403 and "insufficient_scope" in challenge


class HttpTransport:
    """JSON-RPC POSTed to one endpoint; responses come back as JSON or as an SSE stream."""

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        auth: Auth | None = None,
        handle_request: RequestHandler,
    ) -> None:
        self.url = url
        self._headers = headers or {}
        self._auth = auth
        self._handle_request = handle_request
        self._http = httpx.Client(follow_redirects=True)
        self._session_id: str | None = None
        self._protocol_version: str | None = None

    def start(self) -> None:
        pass

    def set_protocol_version(self, version: str) -> None:
        self._protocol_version = version

    def request(self, message: Message, timeout: float) -> Message:
        try:
            with self._post(message, timeout) as response:
                if response.status_code in (202, 204):
                    method = message["method"]
                    raise McpConnectionError(f"server accepted {method} without a response")
                kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if kind == "application/json":
                    body = json.loads(response.read())
                    for item in body if isinstance(body, list) else [body]:
                        if isinstance(item, dict) and item.get("id") == message["id"]:
                            return item
                    raise McpConnectionError(f"no response to {message['method']} in the reply")
                if kind == "text/event-stream":
                    return self._read_stream(response, message)
                raise McpConnectionError(f"unsupported response content type: {kind or 'missing'}")
        except httpx.TimeoutException as exc:
            raise McpTimeoutError(f"no response to {message['method']} in {timeout:g}s") from exc
        except httpx.HTTPError as exc:
            raise McpConnectionError(f"{type(exc).__name__}: {exc}") from exc

    def notify(self, message: Message) -> None:
        try:
            with self._post(message, 30):
                pass
        except httpx.HTTPError as exc:
            raise McpConnectionError(f"{type(exc).__name__}: {exc}") from exc

    def close(self) -> None:
        if self._session_id is not None:
            # On failure the session expires on the server.
            with contextlib.suppress(httpx.HTTPError):
                self._http.delete(self.url, headers=self._request_headers()[0], timeout=1)
            self._session_id = None
        self._http.close()

    def _read_stream(self, response: httpx.Response, message: Message) -> Message:
        for event, data in iter_sse(response.iter_lines()):
            if event not in (None, "message") or not data.strip():
                continue
            try:
                item = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            if _is_response(item) and item["id"] == message["id"]:
                return item
            if _is_request(item):
                self.notify(_answer(self._handle_request, item))
        raise McpConnectionError(f"stream ended without a response to {message['method']}")

    def _request_headers(self) -> tuple[dict[str, str], str | None]:
        headers = dict(self._headers)
        if self._session_id is not None:
            headers["Mcp-Session-Id"] = self._session_id
        if self._protocol_version is not None:
            headers["MCP-Protocol-Version"] = self._protocol_version
        token = self._auth.token() if self._auth is not None else None
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers, token

    def _post(self, message: Message, timeout: float) -> _Response:
        body = json.dumps(message)
        for attempt in range(2):
            headers, token = self._request_headers()
            headers["Accept"] = "application/json, text/event-stream"
            headers["Content-Type"] = "application/json"
            request = self._http.build_request(
                "POST", self.url, headers=headers, content=body, timeout=httpx.Timeout(timeout)
            )
            response = self._http.send(request, stream=True)
            if attempt == 0 and self._auth is not None and _needs_authorization(response):
                response.read()
                response.close()
                self._auth.on_unauthorized(response, token)
                continue
            break
        if response.status_code >= 400:
            text = response.read().decode(errors="replace").strip()[:500]
            response.close()
            if response.status_code == 401 or _needs_authorization(response):
                raise McpAuthRequiredError(
                    "server requires authorization", response.headers.get("www-authenticate")
                )
            if response.status_code == 404 and self._session_id is not None:
                self._session_id = None
                raise McpSessionExpiredError("session expired")
            raise McpConnectionError(f"HTTP {response.status_code}{': ' + text if text else ''}")
        session = response.headers.get("mcp-session-id")
        if session:
            self._session_id = session
        return _Response(response)


class _Response:
    """Closes a streamed response when the `with` block ends."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response

    def __enter__(self) -> httpx.Response:
        return self.response

    def __exit__(self, *exc: object) -> None:
        self.response.close()


# -- client ----------------------------------------------------------------------


class McpClient:
    """One connection to one server: initialize, then requests."""

    def __init__(
        self,
        create_transport: Callable[[RequestHandler], Transport],
        *,
        timeout: float = 60.0,
        roots: list[dict[str, str]] | None = None,
    ) -> None:
        self._create_transport = create_transport
        self.timeout = timeout
        self._roots = roots
        self._transport: Transport | None = None
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self.server_info: dict[str, Any] = {}
        self.capabilities: dict[str, Any] = {}
        self.instructions: str | None = None
        self.protocol_version: str | None = None

    @property
    def connected(self) -> bool:
        return self._transport is not None

    def connect(self) -> None:
        if self._transport is not None:
            return
        transport = self._create_transport(self._handle_request)
        transport.start()
        try:
            capabilities: dict[str, Any] = {"roots": {}} if self._roots is not None else {}
            result = self._send(
                transport,
                "initialize",
                {
                    "protocolVersion": LATEST_PROTOCOL_VERSION,
                    "capabilities": capabilities,
                    "clientInfo": {"name": "rio", "version": current_version()},
                },
            )
            version = result.get("protocolVersion") if isinstance(result, dict) else None
            if version not in SUPPORTED_PROTOCOL_VERSIONS:
                raise McpConnectionError(f"server chose unsupported protocol version {version}")
            transport.set_protocol_version(version)
            transport.notify({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except BaseException:
            transport.close()
            raise
        self.protocol_version = version
        self.server_info = result.get("serverInfo") or {}
        self.capabilities = result.get("capabilities") or {}
        instructions = result.get("instructions")
        self.instructions = instructions if isinstance(instructions, str) else None
        self._transport = transport

    def close(self) -> None:
        transport, self._transport = self._transport, None
        if transport is not None:
            transport.close()

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Send a request, reconnecting once when an HTTP session expired."""
        self.connect()
        assert self._transport is not None
        try:
            return self._send(self._transport, method, params)
        except McpSessionExpiredError:
            self.close()
            self.connect()
            assert self._transport is not None
            return self._send(self._transport, method, params)

    def _send(self, transport: Transport, method: str, params: dict[str, Any] | None) -> Any:
        with self._lock:
            request_id = next(self._ids)
        message: Message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        try:
            response = transport.request(message, self.timeout)
        except McpTimeoutError:
            if method != "initialize":  # The spec forbids cancelling initialize.
                cancelled = {"requestId": request_id, "reason": "timed out"}
                with contextlib.suppress(McpConnectionError):
                    transport.notify(
                        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": cancelled}
                    )
            raise
        error = _response_error(response)
        if error is not None:
            raise error
        return response.get("result")

    def _handle_request(self, method: str, params: Any) -> Any:
        if method == "ping":
            return {}
        if method == "roots/list" and self._roots is not None:
            return {"roots": self._roots}
        raise McpError(METHOD_NOT_FOUND, f"Method not found: {method}")

    def _list(self, method: str, key: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(MAX_LIST_PAGES):
            page = self.request(method, {"cursor": cursor} if cursor else None) or {}
            items.extend(item for item in page.get(key, ()) if isinstance(item, dict))
            cursor = page.get("nextCursor") or None
            if cursor is None:
                return items
        raise McpConnectionError(f"{method} exceeded {MAX_LIST_PAGES} pages")

    def _offers(self, capability: str) -> bool:
        self.connect()
        return capability in self.capabilities

    def list_tools(self) -> list[dict[str, Any]]:
        if not self._offers("tools"):
            return []
        tools = self._list("tools/list", "tools")
        return [tool for tool in tools if isinstance(tool.get("name"), str)]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        if not isinstance(result, dict):
            raise McpConnectionError("invalid tools/call result")
        return result

    def list_resources(self) -> list[dict[str, Any]]:
        if not self._offers("resources"):
            return []
        return self._list("resources/list", "resources")

    def list_resource_templates(self) -> list[dict[str, Any]]:
        if not self._offers("resources"):
            return []
        return self._list("resources/templates/list", "resourceTemplates")

    def read_resource(self, uri: str) -> list[dict[str, Any]]:
        result = self.request("resources/read", {"uri": uri}) or {}
        return list(result.get("contents", ()))

    def list_prompts(self) -> list[dict[str, Any]]:
        if not self._offers("prompts"):
            return []
        return self._list("prompts/list", "prompts")

    def get_prompt(self, name: str, arguments: dict[str, str]) -> dict[str, Any]:
        return self.request("prompts/get", {"name": name, "arguments": arguments}) or {}
