"""Pure-stdlib JSON-RPC 2.0 and MCP protocol helpers."""

from __future__ import annotations

import json
import math
from typing import Any

JSONRPC_VERSION = "2.0"

SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = (
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)
DEFAULT_PROTOCOL_VERSION = "2025-03-26"

# Standard JSON-RPC 2.0 error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def negotiate_protocol_version(requested: str | None) -> str:
    """Negotiate an MCP protocol version, echoing supported versions."""
    if requested and requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    return SUPPORTED_PROTOCOL_VERSIONS[0]


def supports_structured_output(version: str) -> bool:
    """Return True if the negotiated MCP protocol supports structuredContent/outputSchema."""
    return version >= "2025-06-18"


def make_request(
    req_id: int | str,
    method: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 request payload."""
    payload: dict[str, Any] = {
        "jsonrpc": JSONRPC_VERSION,
        "id": req_id,
        "method": method,
    }
    if params is not None:
        payload["params"] = params
    return payload


def make_notification(
    method: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 notification payload (no id)."""
    payload: dict[str, Any] = {
        "jsonrpc": JSONRPC_VERSION,
        "method": method,
    }
    if params is not None:
        payload["params"] = params
    return payload


def make_result(req_id: int | str | None, result: Any) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 success response payload."""
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": req_id,
        "result": result,
    }


def make_error(
    req_id: int | str | None,
    code: int,
    message: str,
    data: Any = None,
) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 error response payload."""
    err: dict[str, Any] = {
        "code": code,
        "message": message,
    }
    if data is not None:
        err["data"] = data
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": req_id,
        "error": err,
    }


def encode_stdio_message(message: dict[str, Any]) -> bytes:
    """Encode a JSON-RPC message as newline-delimited UTF-8 JSON bytes."""
    text = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    return (text + "\n").encode("utf-8")


def estimate_tokens(obj: Any) -> int:
    """Estimate LLM token usage from compact JSON character length (~4 chars/token)."""
    if isinstance(obj, str):
        compact = obj
    else:
        compact = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    if not compact:
        return 0
    return math.ceil(len(compact) / 4)
