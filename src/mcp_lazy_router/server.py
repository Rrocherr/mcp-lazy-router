"""MCP Stdio Server exposing the 4 progressive meta-tools for mcp-lazy-router."""

from __future__ import annotations

import json
import sys
from typing import Any

from . import __version__
from .protocol import (
    DEFAULT_PROTOCOL_VERSION,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    JSONRPC_VERSION,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    encode_stdio_message,
    make_error,
    make_result,
    negotiate_protocol_version,
    supports_structured_output,
)
from .router import LazyRouter
from .validator import validate_arguments

_META_TOOLS_FULL: list[dict[str, Any]] = [
    {
        "name": "search_tools",
        "description": (
            "Search downstream MCP tools via BM25 + synonyms. Use when finding tools before "
            "fetching schemas. Do not use to invoke tools; limit 1-50 per page."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keywords, tool name, or '*'."},
                "server": {"type": "string", "description": "Exact server name filter."},
                "read_only_only": {"type": "boolean", "description": "Only read-only tools."},
                "min_tdqs": {"type": "integer", "minimum": 0, "maximum": 100, "description": "Min TDQS score."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Page size (1-50)."},
                "offset": {"type": "integer", "minimum": 0, "description": "Pagination offset."},
            },
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "required": ["total_matches", "results"],
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
        },
    },
    {
        "name": "get_tool_schema",
        "description": (
            "Fetch full JSON schemas and TDQS metadata for 1-5 tools. Use when search_tools "
            "identifies candidates. Do not use for broad search or >5 tools."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tools": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 5,
                    "description": "1-5 {server, tool} entries.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "server": {"type": "string", "description": "Server name."},
                            "tool": {"type": "string", "description": "Tool name."},
                        },
                        "required": ["server", "tool"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["tools"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "required": ["schemas"],
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
        },
    },
    {
        "name": "call_tool",
        "description": (
            "Validate args and execute a downstream MCP tool. Use when schema is already verified "
            "via get_tool_schema. Do not use without confirmation for destructive tools."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "server": {"type": "string", "description": "Target server name."},
                "tool": {"type": "string", "description": "Target tool name."},
                "arguments": {"type": "object", "description": "Tool JSON arguments."},
                "confirm_destructive": {"type": "boolean", "description": "Confirm destructive run."},
            },
            "required": ["server", "tool"],
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "required": ["content"],
        },
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
        },
    },
    {
        "name": "router_status",
        "description": (
            "Inspect downstream connection state, cache, token savings, and TDQS audit. Use when "
            "diagnosing or refreshing indexes. Do not use to run upstream tools."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "refresh_index": {"type": "boolean", "description": "Re-index enabled servers."},
                "include_tdqs_audit": {"type": "boolean", "description": "Include TDQS audit report."},
            },
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "required": ["total_indexed_tools", "token_savings", "servers"],
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
        },
    },
]


def get_meta_tools(include_output_schema: bool = True) -> list[dict[str, Any]]:
    """Return the 4 router meta-tool definitions, optionally including outputSchema."""
    tools: list[dict[str, Any]] = []
    for raw in _META_TOOLS_FULL:
        item = {
            "name": raw["name"],
            "description": raw["description"],
            "inputSchema": raw["inputSchema"],
            "annotations": raw["annotations"],
        }
        if include_output_schema and "outputSchema" in raw:
            item["outputSchema"] = raw["outputSchema"]
        tools.append(item)
    return tools


_META_TOOL_BY_NAME: dict[str, dict[str, Any]] = {t["name"]: t for t in _META_TOOLS_FULL}


class MCPLazyRouterServer:
    """JSON-RPC 2.0 stdio server exposing mcp-lazy-router meta-tools."""

    def __init__(self, router: LazyRouter) -> None:
        self.router = router
        self.protocol_version: str = DEFAULT_PROTOCOL_VERSION
        self.initialized: bool = False

    def _format_structured_tool_result(
        self,
        payload: dict[str, Any],
        is_error: bool = False,
    ) -> dict[str, Any]:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        result: dict[str, Any] = {
            "content": [{"type": "text", "text": text}],
            "isError": is_error,
        }
        if supports_structured_output(self.protocol_version):
            result["structuredContent"] = payload
        return result

    def _dispatch_meta_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        meta_def = _META_TOOL_BY_NAME.get(name)
        if meta_def is None:
            raise KeyError(name)

        validation_errors = validate_arguments(arguments, meta_def["inputSchema"])
        if validation_errors:
            return self._format_structured_tool_result(
                {"error": "Invalid meta-tool arguments", "details": validation_errors},
                is_error=True,
            )

        if name == "search_tools":
            res = self.router.search_tools(
                query=arguments.get("query", "*"),
                server=arguments.get("server"),
                read_only_only=bool(arguments.get("read_only_only", False)),
                min_tdqs=arguments.get("min_tdqs"),
                limit=int(arguments.get("limit", 8)),
                offset=int(arguments.get("offset", 0)),
            )
            return self._format_structured_tool_result(res, is_error=False)

        if name == "get_tool_schema":
            try:
                res = self.router.get_tool_schemas(arguments["tools"])
            except ValueError as exc:
                return self._format_structured_tool_result({"error": str(exc)}, is_error=True)
            return self._format_structured_tool_result(res, is_error=False)

        if name == "call_tool":
            res = self.router.call_tool(
                server=arguments["server"],
                tool=arguments["tool"],
                arguments=arguments.get("arguments") or {},
                confirm_destructive=bool(arguments.get("confirm_destructive", False)),
            )
            if not supports_structured_output(self.protocol_version) and "structuredContent" in res:
                res = {k: v for k, v in res.items() if k != "structuredContent"}
            return res

        if name == "router_status":
            res = self.router.get_status(
                refresh_index=bool(arguments.get("refresh_index", False)),
                include_tdqs_audit=bool(arguments.get("include_tdqs_audit", False)),
                router_meta_tools=get_meta_tools(include_output_schema=True),
            )
            return self._format_structured_tool_result(res, is_error=False)

        raise KeyError(name)

    def handle_message(self, msg: Any) -> dict[str, Any] | None:
        """Process a single decoded JSON-RPC message; returns response dict or None for notifications."""
        if not isinstance(msg, dict):
            return make_error(None, INVALID_REQUEST, "JSON-RPC message must be an object")

        has_id = "id" in msg
        req_id = msg.get("id")

        if msg.get("jsonrpc") != JSONRPC_VERSION or not isinstance(msg.get("method"), str):
            if not has_id:
                return None
            return make_error(req_id, INVALID_REQUEST, "Invalid JSON-RPC 2.0 request")

        # Notifications have no 'id' member and MUST NOT receive a JSON-RPC response.
        if not has_id:
            return None

        method = msg["method"]
        params = msg.get("params")

        if method == "initialize":
            if params is not None and not isinstance(params, dict):
                return make_error(req_id, INVALID_PARAMS, "'params' must be an object")
            requested_ver = params.get("protocolVersion") if isinstance(params, dict) else None
            self.protocol_version = negotiate_protocol_version(
                requested_ver if isinstance(requested_ver, str) else None
            )
            self.initialized = True
            return make_result(
                req_id,
                {
                    "protocolVersion": self.protocol_version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "mcp-lazy-router", "version": __version__},
                    "instructions": (
                        "Use search_tools -> get_tool_schema -> call_tool to discover and execute "
                        "downstream MCP tools with minimal context overhead."
                    ),
                },
            )

        if method == "ping":
            return make_result(req_id, {})

        if method == "tools/list":
            include_out = supports_structured_output(self.protocol_version)
            return make_result(
                req_id,
                {"tools": get_meta_tools(include_output_schema=include_out)},
            )

        if method == "tools/call":
            if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                return make_error(
                    req_id,
                    INVALID_PARAMS,
                    "tools/call requires params object with string 'name'",
                )
            tool_name = params["name"]
            raw_args = params.get("arguments", {})
            if raw_args is None:
                raw_args = {}
            if not isinstance(raw_args, dict):
                return make_error(
                    req_id,
                    INVALID_PARAMS,
                    "tools/call 'arguments' must be an object",
                )
            try:
                tool_res = self._dispatch_meta_tool(tool_name, raw_args)
            except KeyError:
                return make_error(req_id, INVALID_PARAMS, f"Unknown tool: {tool_name}")
            except Exception as exc:
                return make_error(req_id, INTERNAL_ERROR, f"Internal router error: {exc}")
            return make_result(req_id, tool_res)

        return make_error(req_id, METHOD_NOT_FOUND, f"Method not found: {method}")

    def run_stdio(self) -> None:
        """Run the blocking stdio JSON-RPC loop on sys.stdin.buffer / sys.stdout.buffer."""
        stdin = sys.stdin.buffer
        stdout = sys.stdout.buffer
        try:
            while True:
                raw_line = stdin.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError as exc:
                    err_resp = make_error(None, PARSE_ERROR, f"Parse error: {exc}")
                    stdout.write(encode_stdio_message(err_resp))
                    stdout.flush()
                    continue

                response = self.handle_message(msg)
                if response is not None:
                    stdout.write(encode_stdio_message(response))
                    stdout.flush()
        finally:
            self.router.close()
