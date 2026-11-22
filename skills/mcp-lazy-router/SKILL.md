---
name: mcp-lazy-router
description: >-
  Use this skill when interacting with downstream MCP servers routed through
  mcp-lazy-router. Enforces the 3-step progressive tool workflow (search_tools ->
  get_tool_schema -> call_tool), destructive action confirmation, spill-file
  handling, and router diagnostics.
---

# Dynamic Lazy-Loading MCP Router (`mcp-lazy-router`)

This skill guides you through using `mcp-lazy-router` to discover, inspect, and
execute tools across many downstream MCP servers without bloating the context
window.

## Progressive Tool Workflow

1. **Discover (`search_tools`)**: Search by keyword, intent, or `server` filter
   to get compact tool summaries, safety flags, and TDQS scores. Never guess
   downstream tool names or arguments.
2. **Inspect (`get_tool_schema`)**: Batch-fetch full JSON schemas, normalized
   descriptions, and parameter details for only the 1-5 candidate tools you plan
   to use.
3. **Execute (`call_tool`)**: Invoke the selected `server` + `tool` with
   arguments matching `inputSchema`. Arguments are validated pre-flight before
   any downstream connection is spawned.

## Safety, Oversized Outputs, and Diagnostics

* **Destructive Guardrail**: If `call_tool` returns a confirmation requirement for
  a destructive operation (`destructiveHint: true` or destructive verb heuristic),
  review the target arguments with the user and only re-call with
  `confirm_destructive: true` after explicit approval.
* **Spill Files**: When a downstream response exceeds `max_response_chars`, the
  router truncates the inline payload and writes the complete output to the
  reported spill file path. Read that file selectively (with offsets/ranges)
  instead of re-running the tool.
* **Diagnostics (`router_status`)**: Check connected/cached servers, token
  savings telemetry, or pass `include_tdqs_audit: true` (and `refresh_index:
  true` when downstream servers change) to audit tool description quality.
