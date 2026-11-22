"""Core LazyRouter: caching, indexing, policy guardrails, retries, and spill management."""

from __future__ import annotations

import atexit
import fnmatch
import json
import os
import re
import sys
import tempfile
import threading
import time
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import RouterConfig, ServerConfig
from .protocol import estimate_tokens
from .search import ToolSearchIndex
from .tdqs import NormalizedTool, normalize_tool
from .transports import (
    MCPClientTransport,
    SessionExpiredError,
    TransportError,
    create_transport,
)
from .validator import validate_arguments


@dataclass
class _CachedServerEntry:
    fingerprint: str
    indexed_at: float
    tools: list[NormalizedTool]
    raw_tokens: int


def _matches_any(patterns: list[str], server: str, tool: str) -> bool:
    if not patterns:
        return False
    qualified = f"{server}/{tool}"
    return any(
        fnmatch.fnmatchcase(tool, pat) or fnmatch.fnmatchcase(qualified, pat)
        for pat in patterns
    )


def _sanitize_filename_part(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_")
    return cleaned[:48] or "item"


def _tool_error_result(message: str, **metadata: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "content": [{"type": "text", "text": message}],
        "isError": True,
    }
    if metadata:
        result["structuredContent"] = {"error": message, **metadata}
    return result


class LazyRouter:
    """Lazy-loading MCP multiplexer with TDQS auditing, pre-flight validation, and safety rules."""

    def __init__(self, config: RouterConfig) -> None:
        self.config = config
        self._lock = threading.RLock()
        self._server_cache: dict[str, _CachedServerEntry] = {}
        self._tool_map: dict[tuple[str, str], NormalizedTool] = {}
        self._indexing_errors: dict[str, str] = {}
        self._transports: dict[str, MCPClientTransport] = {}
        self._search_index = ToolSearchIndex()
        self._closed = False

        self.load_cache()
        self_ref = weakref.ref(self)

        def _cleanup_on_exit() -> None:
            instance = self_ref()
            if instance is not None:
                instance.close()

        atexit.register(_cleanup_on_exit)

    def _is_entry_fresh(self, server: ServerConfig, entry: _CachedServerEntry) -> bool:
        if entry.fingerprint != server.fingerprint():
            return False
        ttl = self.config.cache_ttl_seconds
        if ttl <= 0:
            return False
        return (time.time() - entry.indexed_at) <= ttl

    def _rebuild_views(self) -> None:
        tool_map: dict[tuple[str, str], NormalizedTool] = {}
        all_tools: list[NormalizedTool] = []
        for srv_name, srv in self.config.servers.items():
            if not srv.enabled:
                continue
            entry = self._server_cache.get(srv_name)
            if entry is None:
                continue
            for tool in entry.tools:
                tool_map[(srv_name, tool.name)] = tool
                all_tools.append(tool)
        self._tool_map = tool_map
        self._search_index.rebuild(all_tools)

    def load_cache(self) -> None:
        """Load persisted tool index from disk, discarding stale or mismatched entries."""
        with self._lock:
            cache_file = self.config.cache_path
            if not cache_file.is_file():
                self._rebuild_views()
                return
            try:
                raw = json.loads(cache_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                print(f"[mcp-lazy-router] Failed to read cache {cache_file}: {exc}", file=sys.stderr)
                self._rebuild_views()
                return

            if not isinstance(raw, dict) or raw.get("version") != 1:
                self._rebuild_views()
                return

            servers_blob = raw.get("servers")
            if not isinstance(servers_blob, dict):
                self._rebuild_views()
                return

            loaded: dict[str, _CachedServerEntry] = {}
            for srv_name, entry_raw in servers_blob.items():
                srv = self.config.servers.get(srv_name)
                if srv is None or not srv.enabled or not isinstance(entry_raw, dict):
                    continue
                fp = entry_raw.get("fingerprint")
                indexed_at = entry_raw.get("indexed_at")
                raw_tools = entry_raw.get("tools")
                if (
                    not isinstance(fp, str)
                    or not isinstance(indexed_at, (int, float))
                    or not isinstance(raw_tools, list)
                ):
                    continue
                try:
                    norm_tools = [NormalizedTool.from_dict(t) for t in raw_tools if isinstance(t, dict)]
                except Exception:
                    continue
                candidate = _CachedServerEntry(
                    fingerprint=fp,
                    indexed_at=float(indexed_at),
                    tools=norm_tools,
                    raw_tokens=int(
                        entry_raw.get(
                            "raw_tokens",
                            sum(t.raw_token_estimate for t in norm_tools),
                        )
                    ),
                )
                if self._is_entry_fresh(srv, candidate):
                    loaded[srv_name] = candidate

            self._server_cache = loaded
            self._rebuild_views()

    def save_cache(self) -> None:
        """Atomically persist the indexed tool metadata cache to disk."""
        with self._lock:
            cache_file = self.config.cache_path
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "servers": {
                    srv_name: {
                        "fingerprint": entry.fingerprint,
                        "indexed_at": entry.indexed_at,
                        "raw_tokens": entry.raw_tokens,
                        "tools": [t.to_dict() for t in entry.tools],
                    }
                    for srv_name, entry in self._server_cache.items()
                },
            }
            fd, tmp_path = tempfile.mkstemp(
                prefix=".mcp_cache_",
                suffix=".tmp",
                dir=str(cache_file.parent),
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, cache_file)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise

    def _reap_idle_transports(self) -> None:
        now = time.monotonic()
        to_close: list[str] = []
        for srv_name, transport in self._transports.items():
            srv = self.config.servers.get(srv_name)
            idle_limit = srv.idle_timeout_seconds if srv else 300.0
            if not transport.is_alive() or (
                idle_limit > 0 and (now - transport.last_used) > idle_limit
            ):
                to_close.append(srv_name)
        for srv_name in to_close:
            tr = self._transports.pop(srv_name, None)
            if tr is not None:
                try:
                    tr.close()
                except Exception:
                    pass

    def _get_or_connect_transport(self, server: ServerConfig) -> MCPClientTransport:
        with self._lock:
            self._reap_idle_transports()
            existing = self._transports.get(server.name)
            if existing is not None and existing.is_alive():
                existing.touch()
                return existing
            if existing is not None:
                self._transports.pop(server.name, None)
                try:
                    existing.close()
                except Exception:
                    pass

            transport = create_transport(server)
            transport.connect()
            self._transports[server.name] = transport
            return transport

    def _evict_transport(self, server_name: str) -> None:
        with self._lock:
            tr = self._transports.pop(server_name, None)
        if tr is not None:
            try:
                tr.close()
            except Exception:
                pass

    def ensure_indexed(
        self,
        refresh: bool = False,
        server_name: str | None = None,
    ) -> dict[str, str]:
        """Index stale or uncached downstream servers, isolating per-server errors."""
        with self._lock:
            if server_name is not None:
                srv = self.config.servers.get(server_name)
                if srv is None:
                    return {server_name: f"Unknown server '{server_name}'"}
                if not srv.enabled:
                    return {server_name: f"Server '{server_name}' is disabled"}
                targets = [srv]
            else:
                targets = [s for s in self.config.servers.values() if s.enabled]

            updated = False
            for srv in targets:
                cached = self._server_cache.get(srv.name)
                if not refresh and cached is not None and self._is_entry_fresh(srv, cached):
                    continue

                try:
                    transport = self._get_or_connect_transport(srv)
                    raw_tools = transport.list_tools()
                    norm_tools = [
                        normalize_tool(srv.name, raw, tags=srv.tags) for raw in raw_tools
                    ]
                    raw_tokens = sum(t.raw_token_estimate for t in norm_tools)
                    self._server_cache[srv.name] = _CachedServerEntry(
                        fingerprint=srv.fingerprint(),
                        indexed_at=time.time(),
                        tools=norm_tools,
                        raw_tokens=raw_tokens,
                    )
                    self._indexing_errors.pop(srv.name, None)
                    updated = True
                except Exception as exc:
                    err_msg = str(exc)
                    self._indexing_errors[srv.name] = err_msg
                    self._evict_transport(srv.name)
                    print(
                        f"[mcp-lazy-router] Failed to index server '{srv.name}': {err_msg}",
                        file=sys.stderr,
                    )

            self._rebuild_views()
            if updated:
                try:
                    self.save_cache()
                except Exception as exc:
                    print(f"[mcp-lazy-router] Failed saving cache: {exc}", file=sys.stderr)

            return {
                s.name: self._indexing_errors[s.name]
                for s in targets
                if s.name in self._indexing_errors
            }

    def search_tools(
        self,
        query: str = "*",
        server: str | None = None,
        read_only_only: bool = False,
        min_tdqs: int | None = None,
        limit: int = 8,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Search indexed tools across downstream servers (lazily indexing uncached ones)."""
        errors = self.ensure_indexed(refresh=False, server_name=server)
        res = self._search_index.search(
            query=query,
            server=server,
            read_only_only=read_only_only,
            min_tdqs=min_tdqs,
            limit=limit,
            offset=offset,
        )
        if errors:
            res["server_errors"] = errors
        return res

    def get_tool_schemas(self, tools: list[dict[str, Any]]) -> dict[str, Any]:
        """Batch-fetch full schemas, normalized descriptions, and TDQS metadata for 1-5 tools."""
        if not isinstance(tools, list) or not (1 <= len(tools) <= 5):
            raise ValueError("'tools' must be a list containing 1 to 5 {server, tool} items")

        resolved: list[dict[str, Any]] = []
        for idx, item in enumerate(tools):
            if not isinstance(item, dict):
                resolved.append({"index": idx, "error": "Each item must be an object with 'server' and 'tool'"})
                continue
            srv_name = item.get("server")
            tool_name = item.get("tool")
            if not isinstance(srv_name, str) or not isinstance(tool_name, str):
                resolved.append({"index": idx, "error": "Both 'server' and 'tool' must be strings"})
                continue

            errors = self.ensure_indexed(refresh=False, server_name=srv_name)
            norm = self._tool_map.get((srv_name, tool_name))
            if norm is None:
                err_detail = errors.get(srv_name) or f"Tool '{srv_name}/{tool_name}' not found"
                resolved.append({"server": srv_name, "tool": tool_name, "error": err_detail})
                continue

            resolved.append({
                "server": norm.server,
                "tool": norm.name,
                "qualified_name": norm.qualified_name,
                "normalized_description": norm.normalized_description,
                "input_schema": norm.input_schema,
                "output_schema": norm.output_schema,
                "annotations": norm.annotations,
                "read_only": norm.inferred_read_only,
                "destructive": norm.inferred_destructive,
                "safety_source": norm.safety_source,
                "tdqs": norm.tdqs.to_dict(),
            })

        return {"schemas": resolved}

    def _apply_response_spill(
        self,
        server_name: str,
        tool_name: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        max_chars = self.config.policy.max_response_chars
        content = result.get("content")
        if not isinstance(content, list):
            return result

        text_indices = [
            i
            for i, block in enumerate(content)
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
        ]
        if not text_indices:
            return result

        total_chars = sum(len(content[i]["text"]) for i in text_indices)
        if total_chars <= max_chars:
            return result

        full_text = "\n\n".join(content[i]["text"] for i in text_indices)
        spill_path_str: str | None = None

        if self.config.policy.spill_oversized_responses:
            spill_dir = self.config.spill_dir
            spill_dir.mkdir(parents=True, exist_ok=True)
            ts = int(time.time() * 1000)
            fname = (
                f"{ts}_{_sanitize_filename_part(server_name)}_"
                f"{_sanitize_filename_part(tool_name)}.txt"
            )
            spill_path = (spill_dir / fname).resolve()
            spill_path.write_text(full_text, encoding="utf-8")
            spill_path_str = str(spill_path)

        notice = (
            f"\n\n[mcp-lazy-router truncated output: original_chars={total_chars}, "
            f"limit={max_chars}"
        )
        if spill_path_str:
            notice += f", spill_file={spill_path_str}"
        notice += "]"

        keep_budget = max(200, max_chars - len(notice))
        truncated_text = full_text[:keep_budget] + notice

        new_content = [
            block
            for i, block in enumerate(content)
            if i not in text_indices
        ]
        new_content.insert(0, {"type": "text", "text": truncated_text})

        updated = dict(result)
        updated["content"] = new_content
        if "structuredContent" in updated and isinstance(updated["structuredContent"], dict):
            updated["structuredContent"] = {
                **updated["structuredContent"],
                "truncated": True,
                "original_chars": total_chars,
                **({"spill_file": spill_path_str} if spill_path_str else {}),
            }
        return updated

    def call_tool(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any] | None = None,
        confirm_destructive: bool = False,
    ) -> dict[str, Any]:
        """Execute a downstream tool with policy checks, pre-flight validation, and safe retries."""
        args = {} if arguments is None else arguments
        if not isinstance(args, dict):
            return _tool_error_result(
                f"Invalid arguments for '{server}/{tool}': expected an object",
                server=server,
                tool=tool,
            )

        srv = self.config.servers.get(server)
        if srv is None:
            return _tool_error_result(f"Unknown downstream server '{server}'", server=server, tool=tool)
        if not srv.enabled:
            return _tool_error_result(f"Downstream server '{server}' is disabled", server=server, tool=tool)

        # 1. Resolve tool (index server lazily if needed)
        errors = self.ensure_indexed(refresh=False, server_name=server)
        norm_tool = self._tool_map.get((server, tool))
        if norm_tool is None:
            detail = errors.get(server) or f"Tool '{tool}' not found on server '{server}'"
            return _tool_error_result(detail, server=server, tool=tool)

        # 2. Allow / block globs
        if _matches_any(srv.blocked_tools, server, tool) or _matches_any(
            self.config.policy.blocked_tools, server, tool
        ):
            return _tool_error_result(
                f"Tool '{server}/{tool}' is blocked by router policy.",
                server=server,
                tool=tool,
                policy_reason="blocked_tools",
            )
        if srv.allowed_tools and not _matches_any(srv.allowed_tools, server, tool):
            return _tool_error_result(
                f"Tool '{server}/{tool}' is not in server '{server}' allowed_tools list.",
                server=server,
                tool=tool,
                policy_reason="server_allowed_tools",
            )
        if self.config.policy.allowed_tools and not _matches_any(
            self.config.policy.allowed_tools, server, tool
        ):
            return _tool_error_result(
                f"Tool '{server}/{tool}' is not in global allowed_tools policy.",
                server=server,
                tool=tool,
                policy_reason="global_allowed_tools",
            )

        # 3. Read-only mode enforcement
        effective_read_only = (
            srv.read_only if srv.read_only is not None else self.config.policy.read_only
        )
        if effective_read_only and not norm_tool.inferred_read_only:
            return _tool_error_result(
                f"Blocked '{server}/{tool}': server/policy is configured in read_only mode, "
                f"and this tool is classified as non-read-only ({norm_tool.safety_source}).",
                server=server,
                tool=tool,
                policy_reason="read_only",
            )

        # 4. Destructive guardrail
        is_destructive = (
            norm_tool.inferred_destructive
            if norm_tool.safety_source == "declared"
            else (
                norm_tool.inferred_destructive
                and self.config.policy.strict_destructive_heuristics
            )
        )
        if (
            self.config.policy.require_confirmation_for_destructive
            and is_destructive
            and not confirm_destructive
        ):
            preview = json.dumps(args, ensure_ascii=False, separators=(",", ":"))[:300]
            return _tool_error_result(
                f"Confirmation required before executing destructive tool '{server}/{tool}' "
                f"(safety={norm_tool.safety_source}). Arguments preview: {preview}. "
                "Re-invoke call_tool with confirm_destructive=true only after explicit user confirmation.",
                server=server,
                tool=tool,
                requires_confirmation=True,
                safety_source=norm_tool.safety_source,
            )

        # 5. Pre-flight JSON Schema validation
        validation_errors = validate_arguments(args, norm_tool.input_schema)
        if validation_errors:
            return _tool_error_result(
                f"Pre-flight schema validation failed for '{server}/{tool}':\n- "
                + "\n- ".join(validation_errors),
                server=server,
                tool=tool,
                validation_errors=validation_errors,
            )

        # 6. Lazy connect + safe idempotent/read-only retry
        is_retryable = norm_tool.inferred_read_only or (
            norm_tool.annotations.get("idempotentHint") is True
        )
        attempts = 2 if is_retryable else 1
        last_exc: Exception | None = None
        upstream_result: dict[str, Any] | None = None

        for attempt in range(attempts):
            try:
                transport = self._get_or_connect_transport(srv)
                upstream_result = transport.call_tool(tool, args)
                break
            except (SessionExpiredError, TransportError) as exc:
                last_exc = exc
                self._evict_transport(server)
                if attempt + 1 >= attempts:
                    break
            except Exception as exc:
                last_exc = exc
                self._evict_transport(server)
                break

        if upstream_result is None:
            retry_note = (
                " (retried once)" if is_retryable and attempts == 2 else " (not retried: non-idempotent)"
            )
            return _tool_error_result(
                f"Downstream execution failed for '{server}/{tool}'{retry_note}: {last_exc}",
                server=server,
                tool=tool,
            )

        # 7. Output size cap & spill
        return self._apply_response_spill(server, tool, upstream_result)

    def compute_token_telemetry(
        self,
        router_meta_tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Compute accurate token savings comparing upstream raw schemas vs router meta-tools."""
        with self._lock:
            upstream_raw_tokens = sum(
                entry.raw_tokens
                for srv_name, entry in self._server_cache.items()
                if srv_name in self.config.servers and self.config.servers[srv_name].enabled
            )
            router_meta_tokens = estimate_tokens(router_meta_tools)
            tokens_saved = max(0, upstream_raw_tokens - router_meta_tokens)
            reduction_percent = (
                round((tokens_saved / upstream_raw_tokens) * 100.0, 2)
                if upstream_raw_tokens > 0
                else 0.0
            )
            return {
                "upstream_raw_tokens": upstream_raw_tokens,
                "router_meta_tokens": router_meta_tokens,
                "tokens_saved": tokens_saved,
                "reduction_percent": reduction_percent,
            }

    def get_status(
        self,
        refresh_index: bool = False,
        include_tdqs_audit: bool = False,
        router_meta_tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Return server connection states, cache health, token savings, and optional TDQS audit."""
        if refresh_index:
            self.ensure_indexed(refresh=True)

        with self._lock:
            self._reap_idle_transports()
            servers_status: dict[str, Any] = {}
            all_tools: list[NormalizedTool] = []

            for srv_name, srv in self.config.servers.items():
                entry = self._server_cache.get(srv_name)
                transport = self._transports.get(srv_name)
                connected = bool(transport is not None and transport.is_alive())
                tool_count = len(entry.tools) if entry else 0
                if entry and srv.enabled:
                    all_tools.extend(entry.tools)
                servers_status[srv_name] = {
                    "enabled": srv.enabled,
                    "transport": srv.transport,
                    "connected": connected,
                    "cached": entry is not None,
                    "fresh": (entry is not None and self._is_entry_fresh(srv, entry)),
                    "tool_count": tool_count,
                    "raw_tokens": entry.raw_tokens if entry else 0,
                    "last_error": self._indexing_errors.get(srv_name),
                }

            telemetry = self.compute_token_telemetry(router_meta_tools or [])

            status_payload: dict[str, Any] = {
                "total_servers": len(self.config.servers),
                "enabled_servers": sum(1 for s in self.config.servers.values() if s.enabled),
                "total_indexed_tools": len(all_tools),
                "cache_path": str(self.config.cache_path),
                "spill_dir": str(self.config.spill_dir),
                "token_savings": telemetry,
                "servers": servers_status,
            }

            if include_tdqs_audit:
                grade_counts = {"A": 0, "B": 0, "C": 0, "D": 0, "F": 0}
                for t in all_tools:
                    grade_counts[t.tdqs.grade] = grade_counts.get(t.tdqs.grade, 0) + 1
                avg_score = (
                    round(sum(t.tdqs.score for t in all_tools) / len(all_tools), 2)
                    if all_tools
                    else 0.0
                )
                lowest = sorted(all_tools, key=lambda t: (t.tdqs.score, t.qualified_name))[:10]
                status_payload["tdqs_audit"] = {
                    "average_score": avg_score,
                    "grade_distribution": grade_counts,
                    "lowest_scoring_tools": [
                        {
                            "qualified_name": t.qualified_name,
                            "score": t.tdqs.score,
                            "grade": t.tdqs.grade,
                            "smells": t.tdqs.smells,
                            "suggestions": t.tdqs.suggestions,
                        }
                        for t in lowest
                    ],
                }

            return status_payload

    def get_all_tools(self) -> list[NormalizedTool]:
        """Return a snapshot list of all currently cached enabled tools."""
        with self._lock:
            return list(self._tool_map.values())

    def close(self) -> None:
        """Cleanly terminate all active downstream transports."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            transports = list(self._transports.values())
            self._transports.clear()
        for tr in transports:
            try:
                tr.close()
            except Exception:
                pass
