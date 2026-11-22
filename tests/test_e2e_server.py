"""End-to-end tests spawning mcp-lazy-router serve over real stdio pipes + CLI smoke checks."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

TESTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = TESTS_DIR.parent
SRC_DIR = ROOT_DIR / "src"


class _StdioRPCClient:
    """Minimal JSON-RPC stdio client driving python -m mcp_lazy_router serve."""

    def __init__(self, config_path: Path) -> None:
        env = dict(os.environ)
        existing_pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            f"{SRC_DIR}{os.pathsep}{existing_pp}" if existing_pp else str(SRC_DIR)
        )
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "mcp_lazy_router", "serve", "--config", str(config_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(ROOT_DIR),
            env=env,
            bufsize=0,
        )
        self._next_id = 1

    def send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        assert self.proc.stdin is not None
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        raw = (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")
        self.proc.stdin.write(raw)
        self.proc.stdin.flush()

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        assert self.proc.stdin is not None and self.proc.stdout is not None
        req_id = self._next_id
        self._next_id += 1
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            msg["params"] = params
        raw = (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")
        self.proc.stdin.write(raw)
        self.proc.stdin.flush()

        line = self.proc.stdout.readline()
        if not line:
            stderr_txt = self.proc.stderr.read().decode("utf-8", errors="replace") if self.proc.stderr else ""
            raise AssertionError(f"Router closed stdout unexpectedly. Stderr: {stderr_txt}")
        return json.loads(line.decode("utf-8"))

    def call_meta(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        resp = self.request("tools/call", {"name": name, "arguments": arguments})
        self.assert_no_rpc_error(resp)
        return resp["result"]

    @staticmethod
    def assert_no_rpc_error(resp: dict[str, Any]) -> None:
        if "error" in resp:
            raise AssertionError(f"Unexpected JSON-RPC error: {resp['error']}")

    def close(self) -> None:
        if self.proc.stdin is not None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        if self.proc.poll() is None:
            try:
                self.proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=2.0)
        for pipe in (self.proc.stdout, self.proc.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass


def _run_cli(args: list[str]) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{SRC_DIR}{os.pathsep}{existing_pp}" if existing_pp else str(SRC_DIR)
    return subprocess.run(
        [sys.executable, "-m", "mcp_lazy_router", *args],
        cwd=str(ROOT_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )


class EndToEndRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_obj = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_obj.cleanup)
        self.tmp_dir = Path(self.tmp_obj.name)

        self.marker_file = self.tmp_dir / "stdio_start_markers.log"
        self.docs_marker_file = self.tmp_dir / "docs_start_markers.log"
        self.config_path = self.tmp_dir / "router_config.json"
        self.ro_config_path = self.tmp_dir / "router_readonly.json"

        base_config = {
            "cache_path": str(self.tmp_dir / "cache.json"),
            "spill_dir": str(self.tmp_dir / "spills"),
            "cache_ttl_seconds": 86400,
            "policy": {
                "read_only": False,
                "require_confirmation_for_destructive": True,
                "strict_destructive_heuristics": True,
                "max_response_chars": 2000,
                "spill_oversized_responses": True,
            },
            "servers": {
                "mock-stdio": {
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": [str(TESTS_DIR / "mock_stdio_server.py")],
                    "env": {"MOCK_STDIO_MARKER_FILE": str(self.marker_file)},
                    "timeout_seconds": 5.0,
                    "tags": ["git", "repo"],
                },
                "mock-docs": {
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": [str(TESTS_DIR / "mock_stdio_server.py"), "--preset", "docs"],
                    "env": {"MOCK_STDIO_MARKER_FILE": str(self.docs_marker_file)},
                    "timeout_seconds": 5.0,
                    "tags": ["docs", "tickets"],
                },
            },
        }
        self.config_path.write_text(json.dumps(base_config, indent=2), encoding="utf-8")

        ro_config = json.loads(json.dumps(base_config))
        ro_config["servers"]["mock-stdio"]["read_only"] = True
        self.ro_config_path.write_text(json.dumps(ro_config, indent=2), encoding="utf-8")

    @staticmethod
    def _count_markers(marker_path: Path) -> int:
        if not marker_path.exists():
            return 0
        return len([ln for ln in marker_path.read_text(encoding="utf-8").splitlines() if ln.strip()])

    def test_e2e_warm_cache_lazy_spawn_workflow_guardrails_spill_and_crash_recovery(self) -> None:
        # 1. Populate persistent cache across both independent stdio servers (22 + 8 = 30 tools)
        idx_proc = _run_cli(["index", "--config", str(self.config_path)])
        self.assertEqual(idx_proc.returncode, 0, msg=idx_proc.stderr)
        idx_json = json.loads(idx_proc.stdout)
        self.assertEqual(idx_json["indexed_tools"], 30)
        self.assertEqual(idx_json["errors"], {})
        initial_stdio_spawns = self._count_markers(self.marker_file)
        initial_docs_spawns = self._count_markers(self.docs_marker_file)
        self.assertEqual(initial_stdio_spawns, 1)
        self.assertEqual(initial_docs_spawns, 1)

        # 2. Start router serve over stdio pipes with WARM cache
        client = _StdioRPCClient(self.config_path)
        self.addCleanup(client.close)

        init_resp = client.request(
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test"}},
        )
        client.assert_no_rpc_error(init_resp)
        self.assertEqual(init_resp["result"]["protocolVersion"], "2025-06-18")

        # Notifications must NOT produce any response on stdout; next request id matches ping
        client.send_notification("notifications/initialized")
        ping_resp = client.request("ping")
        self.assertEqual(ping_resp["id"], init_resp["id"] + 1)
        self.assertEqual(ping_resp["result"], {})

        # 3. tools/list exposes ONLY the 4 meta-tools with >75% token reduction
        list_resp = client.request("tools/list")
        meta_tools = list_resp["result"]["tools"]
        self.assertEqual(
            [t["name"] for t in meta_tools],
            ["search_tools", "get_tool_schema", "call_tool", "router_status"],
        )
        self.assertIn("outputSchema", meta_tools[0])

        status_res = client.call_meta("router_status", {"include_tdqs_audit": True})
        status_data = status_res["structuredContent"]
        self.assertEqual(status_data["total_indexed_tools"], 30)
        self.assertGreater(status_data["token_savings"]["reduction_percent"], 75.0)
        self.assertIn("tdqs_audit", status_data)
        self.assertEqual(self._count_markers(self.marker_file), initial_stdio_spawns)
        self.assertEqual(self._count_markers(self.docs_marker_file), initial_docs_spawns)

        # 4. search_tools & batch get_tool_schema across both servers spawn 0 downstream processes
        search_res = client.call_meta("search_tools", {"query": "pull request", "limit": 5})
        search_data = search_res["structuredContent"]
        self.assertGreaterEqual(search_data["total_matches"], 2)
        self.assertIn("get_pull_request", [r["tool"] for r in search_data["results"][:2]])

        ro_search = client.call_meta(
            "search_tools", {"query": "pull request", "read_only_only": True, "limit": 5}
        )
        self.assertEqual(ro_search["structuredContent"]["results"][0]["tool"], "get_pull_request")

        schema_res = client.call_meta(
            "get_tool_schema",
            {
                "tools": [
                    {"server": "mock-stdio", "tool": "echo_args"},
                    {"server": "mock-docs", "tool": "http_weather_forecast"},
                ]
            },
        )
        schemas = schema_res["structuredContent"]["schemas"]
        self.assertEqual(len(schemas), 2)
        self.assertIn("input_schema", schemas[0])
        self.assertIn("input_schema", schemas[1])
        self.assertEqual(self._count_markers(self.marker_file), initial_stdio_spawns)
        self.assertEqual(self._count_markers(self.docs_marker_file), initial_docs_spawns)

        # 5. Pre-flight schema validation error prevents spawning mock-docs downstream
        bad_docs = client.call_meta(
            "call_tool",
            {
                "server": "mock-docs",
                "tool": "http_weather_forecast",
                "arguments": {"city": "Baku", "days": "three", "extra": 1},
            },
        )
        self.assertTrue(bad_docs["isError"])
        self.assertIn("Pre-flight schema validation failed", bad_docs["content"][0]["text"])
        self.assertEqual(self._count_markers(self.docs_marker_file), initial_docs_spawns)

        # 6. First real call_tool lazily spawns mock-stdio and mock-docs independently
        ok_stdio = client.call_meta(
            "call_tool",
            {
                "server": "mock-stdio",
                "tool": "echo_args",
                "arguments": {"message": "salam", "count": 2},
            },
        )
        self.assertFalse(ok_stdio["isError"])
        self.assertEqual(self._count_markers(self.marker_file), initial_stdio_spawns + 1)

        ok_docs = client.call_meta(
            "call_tool",
            {
                "server": "mock-docs",
                "tool": "http_weather_forecast",
                "arguments": {"city": "Baku", "days": 3},
            },
        )
        self.assertFalse(ok_docs["isError"])
        self.assertEqual(self._count_markers(self.docs_marker_file), initial_docs_spawns + 1)

        # 7. Destructive guardrail blocks delete_repository without confirm_destructive=true
        blocked_del = client.call_meta(
            "call_tool",
            {
                "server": "mock-stdio",
                "tool": "delete_repository",
                "arguments": {"repo": "org/demo"},
            },
        )
        self.assertTrue(blocked_del["isError"])
        self.assertIn("Confirmation required", blocked_del["content"][0]["text"])

        confirmed_del = client.call_meta(
            "call_tool",
            {
                "server": "mock-stdio",
                "tool": "delete_repository",
                "arguments": {"repo": "org/demo"},
                "confirm_destructive": True,
            },
        )
        self.assertFalse(confirmed_del["isError"])

        # 8. Oversized 50,000-char payload truncates cleanly and writes readable spill file
        huge_res = client.call_meta(
            "call_tool",
            {
                "server": "mock-stdio",
                "tool": "get_huge_log",
                "arguments": {"run_id": "run-99"},
            },
        )
        self.assertFalse(huge_res["isError"])
        huge_text = huge_res["content"][0]["text"]
        self.assertLessEqual(len(huge_text), 2000)
        self.assertIn("spill_file=", huge_text)
        spill_files = list((self.tmp_dir / "spills").glob("*.txt"))
        self.assertEqual(len(spill_files), 1)
        spill_contents = spill_files[0].read_text(encoding="utf-8")
        self.assertGreater(len(spill_contents), 50000)
        self.assertTrue(spill_contents.startswith("LOG_START(run-99)"))

        # 9. Crash recovery: crash_now kills downstream -> subsequent read-only call auto-reconnects;
        # crash_mutating fails without retrying non-idempotent call
        spawns_before_crash = self._count_markers(self.marker_file)
        crash_res = client.call_meta(
            "call_tool",
            {"server": "mock-stdio", "tool": "crash_now", "arguments": {}},
        )
        self.assertTrue(crash_res["isError"])
        recovered = client.call_meta(
            "call_tool",
            {
                "server": "mock-stdio",
                "tool": "echo_args",
                "arguments": {"message": "recovered"},
            },
        )
        self.assertFalse(recovered["isError"])
        self.assertGreater(self._count_markers(self.marker_file), spawns_before_crash)

        spawns_before_mut_crash = self._count_markers(self.marker_file)
        crash_mut = client.call_meta(
            "call_tool",
            {"server": "mock-stdio", "tool": "crash_mutating", "arguments": {}},
        )
        self.assertTrue(crash_mut["isError"])
        self.assertIn("not retried: non-idempotent", crash_mut["content"][0]["text"])
        self.assertEqual(self._count_markers(self.marker_file), spawns_before_mut_crash)

    def test_protocol_2025_03_26_omits_output_schema_and_read_only_blocks_mutation(self) -> None:
        idx_proc = _run_cli(["index", "--config", str(self.ro_config_path)])
        self.assertEqual(idx_proc.returncode, 0, msg=idx_proc.stderr)

        client = _StdioRPCClient(self.ro_config_path)
        self.addCleanup(client.close)

        init_resp = client.request(
            "initialize",
            {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "legacy"}},
        )
        self.assertEqual(init_resp["result"]["protocolVersion"], "2025-03-26")

        tools = client.request("tools/list")["result"]["tools"]
        self.assertFalse(any("outputSchema" in t for t in tools))

        blocked_mut = client.call_meta(
            "call_tool",
            {
                "server": "mock-stdio",
                "tool": "create_issue",
                "arguments": {"repo": "org/repo", "title": "Should fail"},
            },
        )
        self.assertTrue(blocked_mut["isError"])
        self.assertIn("read_only mode", blocked_mut["content"][0]["text"])
        self.assertNotIn("structuredContent", blocked_mut)

    def test_cli_smoke_commands(self) -> None:
        idx = _run_cli(["index", "--config", str(self.config_path)])
        self.assertEqual(idx.returncode, 0, msg=idx.stderr)

        audit_md = _run_cli(["audit", "--config", str(self.config_path), "--format", "markdown"])
        self.assertEqual(audit_md.returncode, 0, msg=audit_md.stderr)
        self.assertIn("MCP Tool Description Quality Score (TDQS) Audit", audit_md.stdout)

        audit_json = _run_cli(["audit", "--config", str(self.config_path), "--format", "json"])
        self.assertEqual(audit_json.returncode, 0, msg=audit_json.stderr)
        parsed_audit = json.loads(audit_json.stdout)
        self.assertEqual(len(parsed_audit["tools"]), 30)

        search_proc = _run_cli(["search", "--config", str(self.config_path), "sql", "--read-only"])
        self.assertEqual(search_proc.returncode, 0, msg=search_proc.stderr)
        search_data = json.loads(search_proc.stdout)
        self.assertGreater(search_data["total_matches"], 0)

        inspect_proc = _run_cli(
            ["inspect", "--config", str(self.config_path), "mock-stdio", "echo_args"]
        )
        self.assertEqual(inspect_proc.returncode, 0, msg=inspect_proc.stderr)
        inspect_data = json.loads(inspect_proc.stdout)
        self.assertEqual(inspect_data["schemas"][0]["tdqs"]["score"], 100)

        bench = _run_cli(["benchmark", "--config", str(self.config_path)])
        self.assertEqual(bench.returncode, 0, msg=bench.stderr)
        self.assertIn("Context Reduction:", bench.stdout)

        src_client_cfg = self.tmp_dir / "client_mcp.json"
        src_client_cfg.write_text(
            json.dumps({
                "mcpServers": {
                    "mcp-lazy-router": {"command": "python", "args": ["run_router.py"]},
                    "demo-stdio": {"command": sys.executable, "args": ["--version"]},
                }
            }),
            encoding="utf-8",
        )
        imported_out = self.tmp_dir / "imported_router.json"
        imp = _run_cli(["import-config", str(src_client_cfg), "--out", str(imported_out)])
        self.assertEqual(imp.returncode, 0, msg=imp.stderr)
        imp_data = json.loads(imported_out.read_text(encoding="utf-8"))
        self.assertNotIn("mcp-lazy-router", imp_data["servers"])
        self.assertIn("demo-stdio", imp_data["servers"])


if __name__ == "__main__":
    unittest.main()
