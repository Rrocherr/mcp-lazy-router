#!/usr/bin/env python3
"""Standalone mock MCP stdio server with 22 tools across 2 pages for integration/E2E testing."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from mock_http_server import HTTP_MOCK_TOOLS

TOOLS_PAGE_1: list[dict[str, Any]] = [
    {
        "name": "list_repositories",
        "description": (
            "List GitHub repositories accessible to the authenticated user. Use when discovering "
            "available projects. Do not use to inspect individual pull requests; limited to 100 items."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Optional user or organization login handle."},
                "visibility": {
                    "type": "string",
                    "enum": ["all", "public", "private"],
                    "description": "Filter repositories by visibility scope.",
                },
                "affiliation": {
                    "type": "string",
                    "description": "Comma-separated list of owner, collaborator, organization_member.",
                },
                "sort": {
                    "type": "string",
                    "enum": ["created", "updated", "pushed", "full_name"],
                    "description": "Sort field for returned repository entries.",
                },
                "direction": {"type": "string", "enum": ["asc", "desc"], "description": "Sort order."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Max repos per page."},
            },
            "additionalProperties": False,
        },
        "outputSchema": {"type": "object", "properties": {"repositories": {"type": "array"}}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "get_pull_request",
        "description": (
            "Fetch detailed metadata, review status, and diff summary for a pull request. "
            "Use when inspecting a PR by number. Do not use for listing all PRs."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository in owner/name format."},
                "pr_number": {"type": "integer", "minimum": 1, "description": "Pull request number."},
                "include_diff": {"type": "boolean", "description": "Include unified diff patch summary."},
                "include_reviews": {"type": "boolean", "description": "Include reviewer approval states."},
            },
            "required": ["repo", "pr_number"],
        },
        "outputSchema": {"type": "object", "properties": {"title": {"type": "string"}}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "create_issue",
        "description": "Create a new GitHub issue in a repository with labels, assignees, and milestone.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Target repository slug (owner/name)."},
                "title": {"type": "string", "description": "Concise issue title."},
                "body": {"type": "string", "description": "Detailed markdown issue description."},
                "labels": {"type": "array", "items": {"type": "string"}, "description": "Issue label names."},
                "assignees": {"type": "array", "items": {"type": "string"}, "description": "Assigned logins."},
                "milestone": {"type": "integer", "description": "Milestone ID number."},
            },
            "required": ["repo", "title"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    },
    {
        "name": "delete_repository",
        "description": "Permanently delete a repository and all branches, issues, and releases.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository owner/name to delete."},
            },
            "required": ["repo"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False},
    },
    {
        "name": "get_huge_log",
        "description": (
            "Retrieve full verbose build and CI diagnostic logs (returns up to 50,000 characters). "
            "Use when debugging CI failures."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "Workflow run ID."},
            },
            "required": ["run_id"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "echo_args",
        "description": (
            "Echo back provided arguments deterministically for diagnostic validation tests. "
            "Use when verifying argument routing and schema validation. Do not use in production."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "minLength": 2, "description": "Text to echo."},
                "count": {"type": "integer", "minimum": 1, "maximum": 10, "description": "Repeat count."},
            },
            "required": ["message"],
            "additionalProperties": False,
        },
        "outputSchema": {"type": "object", "properties": {"echoed": {"type": "object"}}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    },
    {
        "name": "crash_now",
        "description": "Immediately terminate the mock stdio process to test router crash recovery.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "slow_operation",
        "description": "Sleep for a configurable duration to test transport timeout handling.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "delay_seconds": {"type": "number", "description": "Sleep duration in seconds."},
            },
            "required": ["delay_seconds"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    # Deliberately poor TDQS tools
    {
        "name": "syncData",
        "description": "",
        "inputSchema": {
            "type": "object",
            "properties": {"source": {}, "target": {}},
            "required": ["source"],
        },
    },
    {
        "name": "getUser",
        "description": "Get user",
        "inputSchema": {
            "type": "object",
            "properties": {"userId": {"type": "string"}},
            "required": ["userId"],
        },
    },
    {
        "name": "searchCommits",
        "description": "Search commit history across branches by author, hash, or commit message text.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository slug."},
                "query": {"type": "string", "description": "Commit search query."},
            },
            "required": ["repo", "query"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
]

TOOLS_PAGE_2: list[dict[str, Any]] = [
    {
        "name": "list_branches",
        "description": "List branches in a repository along with head commit SHAs and protection status.",
        "inputSchema": {
            "type": "object",
            "properties": {"repo": {"type": "string", "description": "Repository slug."}},
            "required": ["repo"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "merge_pull_request",
        "description": "Merge an approved pull request into the target base branch.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository slug."},
                "pr_number": {"type": "integer", "description": "Pull request number."},
                "method": {"type": "string", "enum": ["merge", "squash", "rebase"], "description": "Merge mode."},
            },
            "required": ["repo", "pr_number"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    },
    {
        "name": "get_file_contents",
        "description": "Read a file or directory listing from a repository at a specific git ref.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository slug."},
                "path": {"type": "string", "description": "File path."},
                "ref": {"type": "string", "description": "Branch, tag, or SHA."},
            },
            "required": ["repo", "path"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "push_branch_changes",
        "description": "Commit and push file updates directly to a feature branch.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository slug."},
                "branch": {"type": "string", "description": "Target branch."},
                "message": {"type": "string", "description": "Commit message."},
            },
            "required": ["repo", "branch", "message"],
        },
    },
    {
        "name": "force_push_branch",
        "description": "Force push git refs, rewriting remote commit history.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository slug."},
                "branch": {"type": "string", "description": "Target branch."},
            },
            "required": ["repo", "branch"],
        },
    },
    {
        "name": "run_sql_query",
        "description": "Execute a read-only SQL query against the analytics database.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "SELECT statement to execute."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "description": "Row cap."},
            },
            "required": ["sql"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "describe_db_schema",
        "description": "Inspect SQL database tables, columns, primary keys, and foreign key indexes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "table": {"type": "string", "description": "Optional table filter."},
            },
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "send_email_digest",
        "description": "Send a formatted email notification digest to specified recipients.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "array", "items": {"type": "string"}, "description": "Recipient emails."},
                "subject": {"type": "string", "description": "Email subject."},
                "body": {"type": "string", "description": "Email body text."},
            },
            "required": ["to", "subject", "body"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
    {
        "name": "crash_mutating",
        "description": "Mutating non-idempotent operation that exits immediately to test no-retry safety.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    },
    {
        "name": "check_ci_status",
        "description": "Check continuous integration workflow status checks for a commit or PR.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository slug."},
                "ref": {"type": "string", "description": "Git ref."},
            },
            "required": ["repo", "ref"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "purge_stale_cache",
        "description": "Wipe cached artifacts and temporary build blobs from storage.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "Cache namespace."},
            },
            "required": ["namespace"],
        },
    },
]


def _write_startup_marker(preset: str) -> None:
    marker_path = os.environ.get("MOCK_STDIO_MARKER_FILE")
    if not marker_path and preset == "docs":
        marker_path = os.environ.get("MOCK_DOCS_MARKER_FILE")
    if marker_path:
        p = Path(marker_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(f"started:{preset}:{os.getpid()}:{time.time()}\n")
            f.flush()


def _send(msg: dict[str, Any]) -> None:
    raw = (json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()


def _handle_call_tool(name: str, arguments: dict[str, Any], preset: str = "default") -> dict[str, Any]:
    if name in ("crash_now", "crash_mutating"):
        os._exit(1)

    if name == "slow_operation":
        delay = float(arguments.get("delay_seconds", 0.5))
        time.sleep(delay)
        return {"content": [{"type": "text", "text": f"Slept {delay}s"}], "isError": False}

    if name == "get_huge_log":
        run_id = arguments.get("run_id", "0")
        payload = f"LOG_START({run_id})\n" + ("X" * 50000) + "\nLOG_END"
        return {"content": [{"type": "text", "text": payload}], "isError": False}

    if name == "echo_args":
        return {
            "content": [{"type": "text", "text": json.dumps({"echoed": arguments})}],
            "structuredContent": {"echoed": arguments},
            "isError": False,
        }

    if preset == "docs":
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {"transport": "stdio-docs", "tool": name, "arguments": arguments}
                    ),
                }
            ],
            "structuredContent": {"transport": "stdio-docs", "tool": name, "arguments": arguments},
            "isError": False,
        }

    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps({"tool": name, "arguments": arguments, "pid": os.getpid()}),
            }
        ],
        "isError": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preset", default="default", choices=["default", "docs"])
    args = parser.parse_args(argv)
    preset = args.preset

    _write_startup_marker(preset)
    stdin = sys.stdin.buffer
    while True:
        raw_line = stdin.readline()
        if not raw_line:
            break
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue

        if "id" not in msg:
            # Notification: never respond
            continue

        req_id = msg["id"]
        method = msg.get("method")
        params = msg.get("params") or {}

        if method == "initialize":
            _send({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": f"mock-stdio-{preset}", "version": "1.0.0"},
                },
            })
        elif method == "ping":
            _send({"jsonrpc": "2.0", "id": req_id, "result": {}})
        elif method == "tools/list":
            if preset == "docs":
                res = {"tools": HTTP_MOCK_TOOLS}
            else:
                cursor = params.get("cursor")
                if cursor == "page2":
                    res = {"tools": TOOLS_PAGE_2}
                else:
                    res = {"tools": TOOLS_PAGE_1, "nextCursor": "page2"}
            _send({"jsonrpc": "2.0", "id": req_id, "result": res})
        elif method == "tools/call":
            tool_name = params.get("name", "")
            tool_args = params.get("arguments") or {}
            _send({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": _handle_call_tool(tool_name, tool_args, preset=preset),
            })
        else:
            _send({
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            })

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
