"""Streamable HTTP MCP mock state machine, socket server, and in-memory urllib adapter."""

from __future__ import annotations

import http.client
import io
import json
import threading
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

HTTP_MOCK_TOOLS: list[dict[str, Any]] = [
    {
        "name": "http_weather_forecast",
        "description": (
            "Fetch multi-day weather forecast for a city using SSE streaming response. "
            "Use when checking weather conditions. Do not use for historical climate archives."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "City name."},
                "days": {"type": "integer", "minimum": 1, "maximum": 7, "description": "Day count."},
            },
            "required": ["city"],
            "additionalProperties": False,
        },
        "outputSchema": {"type": "object", "properties": {"city": {"type": "string"}}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "http_search_docs",
        "description": "Search remote documentation pages by keyword query.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Documentation query."},
            },
            "required": ["query"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "http_get_ticket",
        "description": "Retrieve support ticket metadata and conversation timeline by ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "string", "description": "Ticket identifier."},
            },
            "required": ["ticket_id"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "http_create_ticket",
        "description": "Create a new support ticket in the remote tracker.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Ticket summary."},
                "priority": {"type": "string", "enum": ["low", "medium", "high"], "description": "Priority."},
            },
            "required": ["title"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    },
    {
        "name": "http_update_ticket",
        "description": "Update an existing support ticket status or assignee.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "string", "description": "Ticket identifier."},
                "status": {"type": "string", "description": "Target status."},
            },
            "required": ["ticket_id", "status"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "http_delete_ticket",
        "description": "Permanently delete a support ticket from the tracker.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "string", "description": "Ticket identifier."},
            },
            "required": ["ticket_id"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False},
    },
    {
        "name": "http_list_metrics",
        "description": "List service latency and error-rate telemetry metrics.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "Service name."},
            },
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "http_ping_health",
        "description": "Check remote Streamable HTTP MCP health and active session state.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
]


class MockMCPHTTPState:
    """Pure-Python Streamable HTTP MCP protocol state machine."""

    def __init__(self) -> None:
        self.sessions: set[str] = set()
        self.deleted_sessions: list[str] = []
        self.call_count: int = 0
        self.last_headers: dict[str, str] = {}
        self.lock = threading.Lock()

    def expire_all_sessions(self) -> None:
        with self.lock:
            self.sessions.clear()

    @staticmethod
    def _get_header_ci(headers: dict[str, str], name: str) -> str | None:
        target = name.lower()
        for k, v in headers.items():
            if k.lower() == target:
                return v
        return None

    def handle_request(
        self,
        method: str,
        headers: dict[str, str],
        body: bytes,
    ) -> tuple[int, dict[str, str], bytes]:
        with self.lock:
            self.last_headers = dict(headers)

        upper_method = method.upper()
        if upper_method == "DELETE":
            sid = self._get_header_ci(headers, "Mcp-Session-Id")
            with self.lock:
                if sid:
                    self.sessions.discard(sid)
                    self.deleted_sessions.append(sid)
            raw = json.dumps({"closed": True}, separators=(",", ":")).encode("utf-8")
            return 200, {"Content-Type": "application/json"}, raw

        if upper_method != "POST":
            raw = json.dumps({"error": f"Unsupported method {method}"}).encode("utf-8")
            return 405, {"Content-Type": "application/json"}, raw

        msg = json.loads(body.decode("utf-8"))
        rpc_method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params") or {}

        if rpc_method == "initialize":
            new_sid = uuid.uuid4().hex
            with self.lock:
                self.sessions.add(new_sid)
            payload = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "mock-http-server", "version": "1.0.0"},
                },
            }
            return (
                200,
                {"Content-Type": "application/json", "Mcp-Session-Id": new_sid},
                json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            )

        sid = self._get_header_ci(headers, "Mcp-Session-Id")
        with self.lock:
            valid = bool(sid and sid in self.sessions)
        if not valid:
            return (
                404,
                {"Content-Type": "application/json"},
                json.dumps({"error": "Session expired or invalid"}).encode("utf-8"),
            )

        if "id" not in msg:
            return 202, {"Mcp-Session-Id": sid}, b""

        if rpc_method == "tools/list":
            payload = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": HTTP_MOCK_TOOLS},
            }
            return (
                200,
                {"Content-Type": "application/json", "Mcp-Session-Id": sid},
                json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            )

        if rpc_method == "tools/call":
            with self.lock:
                self.call_count += 1
            tool_name = params.get("name", "")
            tool_args = params.get("arguments") or {}
            payload_res = {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {"transport": "http", "tool": tool_name, "arguments": tool_args}
                        ),
                    }
                ],
                "structuredContent": {
                    "transport": "http",
                    "tool": tool_name,
                    "arguments": tool_args,
                },
                "isError": False,
            }
            rpc_resp = {"jsonrpc": "2.0", "id": req_id, "result": payload_res}
            if tool_name == "http_weather_forecast":
                sse_bytes = (
                    "event: message\n"
                    f"data: {json.dumps(rpc_resp, separators=(',', ':'))}\n\n"
                ).encode("utf-8")
                return (
                    200,
                    {"Content-Type": "text/event-stream", "Mcp-Session-Id": sid},
                    sse_bytes,
                )
            return (
                200,
                {"Content-Type": "application/json", "Mcp-Session-Id": sid},
                json.dumps(rpc_resp, separators=(",", ":")).encode("utf-8"),
            )

        err_payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Unknown method: {rpc_method}"},
        }
        return (
            200,
            {"Content-Type": "application/json", "Mcp-Session-Id": sid},
            json.dumps(err_payload, separators=(",", ":")).encode("utf-8"),
        )


class _InMemoryHTTPResponse:
    """Context-manager response object matching urllib.request.urlopen return shape."""

    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self._body = body
        self.headers = http.client.HTTPMessage()
        for k, v in headers.items():
            self.headers[k] = v

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _InMemoryHTTPResponse:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None


class InMemoryHTTPAdapter:
    """In-process urllib opener adapter routing requests directly to MockMCPHTTPState."""

    def __init__(self, state: MockMCPHTTPState) -> None:
        self.state = state

    def urlopen(
        self,
        request: urllib.request.Request,
        timeout: float = 30.0,
    ) -> _InMemoryHTTPResponse:
        _ = timeout
        body = request.data if isinstance(request.data, bytes) else b""
        headers = {str(k): str(v) for k, v in request.header_items()}
        status, resp_headers, resp_body = self.state.handle_request(
            request.get_method(), headers, body
        )
        if status >= 400:
            msg = http.client.HTTPMessage()
            for k, v in resp_headers.items():
                msg[k] = v
            raise urllib.error.HTTPError(
                request.full_url,
                status,
                f"HTTP {status}",
                msg,
                io.BytesIO(resp_body),
            )
        return _InMemoryHTTPResponse(status, resp_headers, resp_body)


class MockMCPHTTPServer:
    """Thread-safe Streamable HTTP MCP mock server backed by MockMCPHTTPState."""

    def __init__(self) -> None:
        self.state = MockMCPHTTPState()
        state = self.state

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def _dispatch(self, method: str, body: bytes) -> None:
                headers = {k: v for k, v in self.headers.items()}
                status, resp_headers, resp_body = state.handle_request(method, headers, body)
                self.send_response(status)
                for k, v in resp_headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(resp_body)))
                self.end_headers()
                if resp_body:
                    self.wfile.write(resp_body)

            def do_DELETE(self) -> None:  # noqa: N802
                self._dispatch("DELETE", b"")

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                self._dispatch("POST", raw)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def sessions(self) -> set[str]:
        return self.state.sessions

    @property
    def deleted_sessions(self) -> list[str]:
        return self.state.deleted_sessions

    @property
    def call_count(self) -> int:
        return self.state.call_count

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address
        return f"http://{host}:{port}/mcp"

    def start(self) -> str:
        self._thread.start()
        return self.url

    def expire_all_sessions(self) -> None:
        self.state.expire_all_sessions()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=2.0)
