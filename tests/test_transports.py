"""Integration tests for StdioClientTransport, StreamableHTTPClientTransport, and router HTTP retry rules."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TESTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = TESTS_DIR.parent
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from mock_http_server import InMemoryHTTPAdapter, MockMCPHTTPState

from mcp_lazy_router.config import PolicyConfig, RouterConfig, ServerConfig
from mcp_lazy_router.router import LazyRouter
from mcp_lazy_router.transports import (
    SessionExpiredError,
    StdioClientTransport,
    StreamableHTTPClientTransport,
    TransportError,
    TransportTimeoutError,
)


class TransportIntegrationTests(unittest.TestCase):
    def test_stdio_transport_pagination_call_timeout_and_close(self) -> None:
        srv_cfg = ServerConfig(
            name="mock-stdio",
            transport="stdio",
            command=sys.executable,
            args=[str(TESTS_DIR / "mock_stdio_server.py")],
            timeout_seconds=1.0,
        )
        transport = StdioClientTransport(srv_cfg)
        try:
            transport.connect()
            self.assertTrue(transport.is_alive())

            tools = transport.list_tools()
            self.assertEqual(len(tools), 22)
            names = {t["name"] for t in tools}
            self.assertIn("list_repositories", names)
            self.assertIn("purge_stale_cache", names)
            self.assertIn("crash_mutating", names)

            echo = transport.call_tool("echo_args", {"message": "ping", "count": 3})
            self.assertFalse(echo.get("isError", False))
            payload = json.loads(echo["content"][0]["text"])
            self.assertEqual(payload["echoed"], {"message": "ping", "count": 3})

            with self.assertRaises(TransportTimeoutError):
                transport.call_tool("slow_operation", {"delay_seconds": 2.2})
        finally:
            transport.close()
        self.assertFalse(transport.is_alive())

    def test_stdio_missing_env_raises_transport_error(self) -> None:
        srv_cfg = ServerConfig(
            name="bad-env",
            transport="stdio",
            command=sys.executable,
            args=[str(TESTS_DIR / "mock_stdio_server.py")],
            env={"SECRET": "${NON_EXISTENT_TEST_VAR_XYZ}"},
        )
        transport = StdioClientTransport(srv_cfg)
        with self.assertRaises(TransportError):
            transport.connect()

    def test_streamable_http_transport_json_sse_session_expiry_and_close(self) -> None:
        state = MockMCPHTTPState()
        adapter = InMemoryHTTPAdapter(state)

        # Missing env var in HTTP headers raises TransportError at connect time
        bad_cfg = ServerConfig(
            name="bad-http",
            transport="http",
            url="https://mock.local/mcp",
            headers={"Authorization": "Bearer ${MISSING_HTTP_TOKEN_XYZ}"},
        )
        bad_tr = StreamableHTTPClientTransport(bad_cfg, opener=adapter.urlopen)
        with self.assertRaises(TransportError):
            bad_tr.connect()

        cfg = ServerConfig(
            name="mock-http",
            transport="http",
            url="https://mock.local/mcp",
            headers={"Authorization": "Bearer ${TEST_HTTP_MOCK_TOKEN}"},
            timeout_seconds=3.0,
        )
        transport = StreamableHTTPClientTransport(cfg, opener=adapter.urlopen)
        with patch.dict(os.environ, {"TEST_HTTP_MOCK_TOKEN": "tok-secret-123"}):
            try:
                transport.connect()
                self.assertTrue(transport.is_alive())
                self.assertEqual(len(state.sessions), 1)
                self.assertEqual(state.last_headers.get("Authorization"), "Bearer tok-secret-123")

                tools = transport.list_tools()
                self.assertEqual(len(tools), 8)

                # JSON response tool
                ping_res = transport.call_tool("http_ping_health", {})
                self.assertFalse(ping_res.get("isError", False))
                self.assertIn("http_ping_health", ping_res["content"][0]["text"])

                # SSE text/event-stream response tool
                sse_res = transport.call_tool("http_weather_forecast", {"city": "Baku", "days": 3})
                self.assertFalse(sse_res.get("isError", False))
                parsed = json.loads(sse_res["content"][0]["text"])
                self.assertEqual(parsed["arguments"], {"city": "Baku", "days": 3})

                # Simulate server expiring sessions -> HTTP 404 -> SessionExpiredError
                state.expire_all_sessions()
                with self.assertRaises(SessionExpiredError):
                    transport.call_tool("http_ping_health", {})

                # Reconnect and verify DELETE on close
                transport.connect()
                active_sid = next(iter(state.sessions))
                transport.close()
                self.assertIn(active_sid, state.deleted_sessions)
            finally:
                transport.close()

    def test_router_http_session_expiry_retries_readonly_not_mutating(self) -> None:
        state = MockMCPHTTPState()
        adapter = InMemoryHTTPAdapter(state)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            srv_cfg = ServerConfig(
                name="mock-http",
                transport="http",
                url="https://mock.local/mcp",
                timeout_seconds=3.0,
            )
            router_cfg = RouterConfig(
                servers={"mock-http": srv_cfg},
                cache_path=tmp_dir / "cache.json",
                spill_dir=tmp_dir / "spills",
                policy=PolicyConfig(),
            )

            with patch(
                "mcp_lazy_router.router.create_transport",
                side_effect=lambda c: StreamableHTTPClientTransport(c, opener=adapter.urlopen),
            ):
                router = LazyRouter(router_cfg)
                self.addCleanup(router.close)

                # Index tools first and establish active session via read-only call
                router.ensure_indexed(refresh=True)
                first_call = router.call_tool("mock-http", "http_ping_health", {})
                self.assertFalse(first_call["isError"])

                # Expire session -> read-only tool MUST transparently reconnect and succeed
                state.expire_all_sessions()
                ro_retry = router.call_tool("mock-http", "http_ping_health", {})
                self.assertFalse(ro_retry["isError"])

                # Expire session again -> non-idempotent mutating tool MUST NOT retry
                state.expire_all_sessions()
                mut_fail = router.call_tool(
                    "mock-http",
                    "http_create_ticket",
                    {"title": "Do not duplicate"},
                )
                self.assertTrue(mut_fail["isError"])
                self.assertIn("not retried: non-idempotent", mut_fail["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
