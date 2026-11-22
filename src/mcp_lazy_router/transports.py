"""Downstream MCP client transports (Stdio and Streamable HTTP) using pure stdlib."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from . import __version__
from .config import ServerConfig, expand_env_vars
from .protocol import (
    SUPPORTED_PROTOCOL_VERSIONS,
    encode_stdio_message,
    make_notification,
    make_request,
)


class TransportError(RuntimeError):
    """Base exception for downstream MCP transport failures."""


class TransportTimeoutError(TransportError):
    """Raised when a downstream MCP request exceeds its configured timeout."""


class SessionExpiredError(TransportError):
    """Raised when a remote Streamable HTTP session returns HTTP 404."""


class MCPClientTransport(ABC):
    """Abstract base class for downstream MCP transports."""

    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self.last_used: float = time.monotonic()
        self.protocol_version: str = SUPPORTED_PROTOCOL_VERSIONS[0]

    @abstractmethod
    def connect(self) -> None:
        """Initialize the transport connection and negotiate MCP protocol version."""

    @abstractmethod
    def is_alive(self) -> bool:
        """Return True if the transport connection appears healthy."""

    @abstractmethod
    def list_tools(self) -> list[dict[str, Any]]:
        """Retrieve all tools from the downstream server, handling pagination."""

    @abstractmethod
    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Invoke a downstream tool and return its MCP CallToolResult."""

    @abstractmethod
    def close(self) -> None:
        """Cleanly terminate the downstream transport session."""

    def touch(self) -> None:
        self.last_used = time.monotonic()


@dataclass
class _PendingRequest:
    event: threading.Event = field(default_factory=threading.Event)
    response: dict[str, Any] | None = None
    error: Exception | None = None


def _paginate_tools(fetch_page: Any) -> list[dict[str, Any]]:
    """Paginate tools/list across cursor/nextCursor up to 50 pages with cycle guard."""
    tools: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()

    for _ in range(50):
        params: dict[str, Any] = {}
        if cursor is not None:
            params["cursor"] = cursor
        result = fetch_page(params)
        page_tools = result.get("tools")
        if isinstance(page_tools, list):
            for item in page_tools:
                if isinstance(item, dict):
                    tools.append(item)
        next_cursor = result.get("nextCursor")
        if not isinstance(next_cursor, str) or not next_cursor:
            break
        if next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    return tools


class StdioClientTransport(MCPClientTransport):
    """Stdio subprocess transport for local MCP servers."""

    def __init__(self, config: ServerConfig) -> None:
        super().__init__(config)
        self._proc: subprocess.Popen[bytes] | None = None
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._next_id = 1
        self._pending: dict[int, _PendingRequest] = {}
        self._stderr_lines: deque[str] = deque(maxlen=50)
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._closed = False

    def _resolve_command(self, command: str) -> str:
        resolved = shutil.which(command)
        if resolved:
            return resolved
        if os.path.isfile(command):
            return command
        raise TransportError(
            f"Executable '{command}' not found on PATH for server '{self.config.name}'"
        )

    def _build_env(self) -> dict[str, str]:
        merged = dict(os.environ)
        missing_vars: list[str] = []
        for key, raw_val in self.config.env.items():
            expanded, missing = expand_env_vars(raw_val, environ=merged, strict=False)
            for var in missing:
                if var not in missing_vars:
                    missing_vars.append(var)
            merged[key] = expanded
        if missing_vars:
            raise TransportError(
                f"Server '{self.config.name}' missing required environment variable(s): "
                f"{', '.join(missing_vars)}"
            )
        return merged

    def _stderr_tail(self) -> str:
        with self._state_lock:
            if not self._stderr_lines:
                return ""
            return " | ".join(self._stderr_lines)

    def _fail_all_pending(self, exc: Exception) -> None:
        with self._state_lock:
            pending_items = list(self._pending.values())
            self._pending.clear()
        for pending in pending_items:
            pending.error = exc
            pending.event.set()

    def _stdout_reader_loop(self, pipe: Any) -> None:
        try:
            for raw_line in iter(pipe.readline, b""):
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    print(
                        f"[mcp-lazy-router:{self.config.name}] ignoring non-JSON stdout line",
                        file=sys.stderr,
                    )
                    continue
                if not isinstance(msg, dict):
                    continue
                msg_id = msg.get("id")
                if msg_id is None:
                    # Downstream notification/log; ignore gracefully.
                    continue
                with self._state_lock:
                    pending = self._pending.pop(msg_id, None)
                if pending is not None:
                    pending.response = msg
                    pending.event.set()
        except Exception as exc:
            self._fail_all_pending(
                TransportError(f"Server '{self.config.name}' stdout read failed: {exc}")
            )
            return

        tail = self._stderr_tail()
        detail = f" Stderr: {tail}" if tail else ""
        self._fail_all_pending(
            TransportError(f"Server '{self.config.name}' closed stdout unexpectedly.{detail}")
        )

    def _stderr_reader_loop(self, pipe: Any) -> None:
        try:
            for raw_line in iter(pipe.readline, b""):
                text = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if text:
                    with self._state_lock:
                        self._stderr_lines.append(text)
        except Exception:
            pass

    def _send_payload(self, payload: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise TransportError(f"Server '{self.config.name}' process is not running")
        data = encode_stdio_message(payload)
        with self._write_lock:
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
            except OSError as exc:
                raise TransportError(
                    f"Failed writing to server '{self.config.name}' stdin: {exc}"
                ) from exc

    def _request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if not self.is_alive():
            raise TransportError(f"Server '{self.config.name}' is not connected")

        with self._state_lock:
            req_id = self._next_id
            self._next_id += 1
            pending = _PendingRequest()
            self._pending[req_id] = pending

        try:
            self._send_payload(make_request(req_id, method, params))
        except Exception:
            with self._state_lock:
                self._pending.pop(req_id, None)
            raise

        effective_timeout = timeout if timeout is not None else self.config.timeout_seconds
        if not pending.event.wait(timeout=effective_timeout):
            with self._state_lock:
                self._pending.pop(req_id, None)
            raise TransportTimeoutError(
                f"Server '{self.config.name}' timed out after {effective_timeout}s on '{method}'"
            )

        if pending.error is not None:
            raise pending.error

        resp = pending.response or {}
        if "error" in resp and isinstance(resp["error"], dict):
            err = resp["error"]
            raise TransportError(
                f"Downstream MCP error ({err.get('code')}) from '{self.config.name}': "
                f"{err.get('message', 'unknown error')}"
            )

        self.touch()
        result = resp.get("result")
        return result if isinstance(result, dict) else {}

    def connect(self) -> None:
        assert self.config.command is not None
        executable = self._resolve_command(self.config.command)
        env = self._build_env()
        expanded_args: list[str] = []
        missing_arg_vars: list[str] = []
        for arg in self.config.args:
            exp, missing = expand_env_vars(arg, environ=env, strict=False)
            missing_arg_vars.extend(v for v in missing if v not in missing_arg_vars)
            expanded_args.append(exp)
        if missing_arg_vars:
            raise TransportError(
                f"Server '{self.config.name}' args missing env variable(s): "
                f"{', '.join(missing_arg_vars)}"
            )

        self._closed = False
        try:
            self._proc = subprocess.Popen(
                [executable, *expanded_args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.config.cwd,
                env=env,
                bufsize=0,
            )
        except OSError as exc:
            raise TransportError(
                f"Failed to start stdio server '{self.config.name}': {exc}"
            ) from exc

        assert self._proc.stdout is not None and self._proc.stderr is not None
        self._stdout_thread = threading.Thread(
            target=self._stdout_reader_loop,
            args=(self._proc.stdout,),
            daemon=True,
            name=f"mcp-stdout-{self.config.name}",
        )
        self._stderr_thread = threading.Thread(
            target=self._stderr_reader_loop,
            args=(self._proc.stderr,),
            daemon=True,
            name=f"mcp-stderr-{self.config.name}",
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

        try:
            init_res = self._request(
                "initialize",
                {
                    "protocolVersion": SUPPORTED_PROTOCOL_VERSIONS[0],
                    "capabilities": {},
                    "clientInfo": {"name": "mcp-lazy-router", "version": __version__},
                },
            )
            negotiated = init_res.get("protocolVersion")
            if isinstance(negotiated, str) and negotiated:
                self.protocol_version = negotiated
            self._send_payload(make_notification("notifications/initialized"))
            self.touch()
        except Exception:
            self.close()
            raise

    def is_alive(self) -> bool:
        return not self._closed and self._proc is not None and self._proc.poll() is None

    def list_tools(self) -> list[dict[str, Any]]:
        return _paginate_tools(lambda params: self._request("tools/list", params))

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._request("tools/call", {"name": name, "arguments": arguments})

    def close(self) -> None:
        self._closed = True
        proc = self._proc
        self._proc = None
        if proc is None:
            return

        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass

        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=2.0)
                except OSError:
                    pass
            except OSError:
                pass

        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass

        for thread in (self._stdout_thread, self._stderr_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout=0.5)

        self._fail_all_pending(
            TransportError(f"Server '{self.config.name}' transport was closed")
        )


def _parse_sse_jsonrpc(body: str, expected_id: int | str) -> dict[str, Any]:
    """Parse an SSE text/event-stream response and locate the JSON-RPC message for expected_id."""
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    blocks = normalized.split("\n\n")
    fallback_msg: dict[str, Any] | None = None

    for block in blocks:
        if not block.strip():
            continue
        data_lines: list[str] = []
        for line in block.split("\n"):
            if line.startswith(":"):
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip(" "))
        if not data_lines:
            continue
        payload_text = "\n".join(data_lines).strip()
        if not payload_text or payload_text == "[DONE]":
            continue
        try:
            parsed = json.loads(payload_text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict):
                    if item.get("id") == expected_id:
                        return item
                    if "result" in item or "error" in item:
                        fallback_msg = item
        elif isinstance(parsed, dict):
            if parsed.get("id") == expected_id:
                return parsed
            if "result" in parsed or "error" in parsed:
                fallback_msg = parsed

    if fallback_msg is not None:
        return fallback_msg
    raise TransportError("SSE response stream did not contain a valid JSON-RPC response")


class StreamableHTTPClientTransport(MCPClientTransport):
    """Streamable HTTP transport (MCP 2025-03-26 / 2025-06-18) using pure stdlib urllib."""

    def __init__(self, config: ServerConfig, opener: Any = None) -> None:
        super().__init__(config)
        self._opener = opener if opener is not None else urllib.request.urlopen
        self._url: str | None = None
        self._headers: dict[str, str] = {}
        self._session_id: str | None = None
        self._next_id = 1
        self._lock = threading.Lock()
        self._connected = False

    def _expand_config(self) -> tuple[str, dict[str, str]]:
        assert self.config.url is not None
        missing_all: list[str] = []
        url, m_url = expand_env_vars(self.config.url, strict=False)
        missing_all.extend(v for v in m_url if v not in missing_all)

        headers: dict[str, str] = {}
        for k, v in self.config.headers.items():
            exp_v, m_h = expand_env_vars(v, strict=False)
            missing_all.extend(var for var in m_h if var not in missing_all)
            headers[k] = exp_v

        if missing_all:
            raise TransportError(
                f"HTTP server '{self.config.name}' missing required env variable(s): "
                f"{', '.join(missing_all)}"
            )
        return url, headers

    def _post(
        self,
        payload: dict[str, Any],
        expect_response: bool = True,
    ) -> dict[str, Any] | None:
        if not self._url:
            raise TransportError(f"HTTP server '{self.config.name}' is not initialized")

        req_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self.protocol_version,
            **self._headers,
        }
        if self._session_id:
            req_headers["Mcp-Session-Id"] = self._session_id

        raw_body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self._url,
            data=raw_body,
            headers=req_headers,
            method="POST",
        )

        try:
            with self._opener(request, timeout=self.config.timeout_seconds) as resp:
                session_hdr = resp.headers.get("Mcp-Session-Id")
                if session_hdr:
                    self._session_id = session_hdr.strip()

                status = getattr(resp, "status", 200)
                body_bytes = resp.read()
                content_type = (resp.headers.get("Content-Type") or "").lower()
        except urllib.error.HTTPError as exc:
            try:
                if exc.code == 404 and self._session_id is not None:
                    self._session_id = None
                    self._connected = False
                    raise SessionExpiredError(
                        f"HTTP MCP session expired (404) on server '{self.config.name}'"
                    ) from exc
                try:
                    err_text = exc.read().decode("utf-8", errors="replace")[:500]
                except Exception:
                    err_text = str(exc.reason)
                raise TransportError(
                    f"HTTP {exc.code} from server '{self.config.name}': {err_text}"
                ) from exc
            finally:
                try:
                    exc.close()
                except Exception:
                    pass
        except (TimeoutError, socket.timeout) as exc:
            raise TransportTimeoutError(
                f"HTTP server '{self.config.name}' timed out after {self.config.timeout_seconds}s"
            ) from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise TransportTimeoutError(
                    f"HTTP server '{self.config.name}' timed out after {self.config.timeout_seconds}s"
                ) from exc
            raise TransportError(
                f"HTTP connection error for server '{self.config.name}': {exc.reason}"
            ) from exc

        self.touch()
        body_text = body_bytes.decode("utf-8", errors="replace").strip()

        if status == 202 or not body_text:
            if not expect_response:
                return None
            raise TransportError(
                f"Expected JSON-RPC response from '{self.config.name}', got HTTP {status} with empty body"
            )

        if "text/event-stream" in content_type:
            rpc_msg = _parse_sse_jsonrpc(body_text, payload["id"])
        else:
            try:
                rpc_msg = json.loads(body_text)
            except json.JSONDecodeError as exc:
                raise TransportError(
                    f"Invalid JSON response from HTTP server '{self.config.name}': {exc}"
                ) from exc

        if not expect_response:
            return None

        if not isinstance(rpc_msg, dict):
            raise TransportError(f"Expected JSON-RPC object from '{self.config.name}'")

        if "error" in rpc_msg and isinstance(rpc_msg["error"], dict):
            err = rpc_msg["error"]
            raise TransportError(
                f"Downstream MCP error ({err.get('code')}) from '{self.config.name}': "
                f"{err.get('message', 'unknown error')}"
            )

        result = rpc_msg.get("result")
        return result if isinstance(result, dict) else {}

    def _next_req_id(self) -> int:
        with self._lock:
            rid = self._next_id
            self._next_id += 1
            return rid

    def connect(self) -> None:
        self._url, self._headers = self._expand_config()
        self._session_id = None
        self._connected = True
        try:
            init_res = self._post(
                make_request(
                    self._next_req_id(),
                    "initialize",
                    {
                        "protocolVersion": SUPPORTED_PROTOCOL_VERSIONS[0],
                        "capabilities": {},
                        "clientInfo": {"name": "mcp-lazy-router", "version": __version__},
                    },
                ),
                expect_response=True,
            )
            if isinstance(init_res, dict):
                negotiated = init_res.get("protocolVersion")
                if isinstance(negotiated, str) and negotiated:
                    self.protocol_version = negotiated
            self._post(
                make_notification("notifications/initialized"),
                expect_response=False,
            )
            self.touch()
        except Exception:
            self._connected = False
            raise

    def is_alive(self) -> bool:
        return self._connected and bool(self._url)

    def list_tools(self) -> list[dict[str, Any]]:
        return _paginate_tools(
            lambda params: self._post(
                make_request(self._next_req_id(), "tools/list", params),
                expect_response=True,
            )
            or {}
        )

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        res = self._post(
            make_request(
                self._next_req_id(),
                "tools/call",
                {"name": name, "arguments": arguments},
            ),
            expect_response=True,
        )
        return res if res is not None else {}

    def close(self) -> None:
        url = self._url
        session_id = self._session_id
        self._connected = False
        self._session_id = None
        if url and session_id:
            req = urllib.request.Request(
                url,
                headers={
                    "Mcp-Session-Id": session_id,
                    "MCP-Protocol-Version": self.protocol_version,
                    **self._headers,
                },
                method="DELETE",
            )
            try:
                with self._opener(req, timeout=2.0):
                    pass
            except Exception:
                pass


def create_transport(config: ServerConfig) -> MCPClientTransport:
    """Factory creating the appropriate downstream transport for a ServerConfig."""
    if config.transport == "stdio":
        return StdioClientTransport(config)
    if config.transport == "http":
        return StreamableHTTPClientTransport(config)
    raise TransportError(f"Unsupported transport '{config.transport}' for '{config.name}'")
