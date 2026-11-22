"""Command-line interface for mcp-lazy-router."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import ConfigError, import_client_config, load_config
from .router import LazyRouter
from .server import MCPLazyRouterServer, get_meta_tools


def _cmd_serve(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    router = LazyRouter(cfg)
    try:
        if args.log_level.upper() in ("DEBUG", "INFO"):
            print(
                f"[mcp-lazy-router] Starting stdio server with {len(cfg.servers)} configured server(s)",
                file=sys.stderr,
            )
        if args.eager_index:
            errs = router.ensure_indexed(refresh=False)
            if errs and args.log_level.upper() in ("DEBUG", "INFO", "WARNING"):
                print(f"[mcp-lazy-router] Eager index warnings: {errs}", file=sys.stderr)
        MCPLazyRouterServer(router).run_stdio()
        return 0
    finally:
        router.close()


def _cmd_index(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    router = LazyRouter(cfg)
    try:
        errors = router.ensure_indexed(refresh=True, server_name=args.server)
        status = router.get_status(
            refresh_index=False,
            include_tdqs_audit=False,
            router_meta_tools=get_meta_tools(include_output_schema=True),
        )
        output = {
            "indexed_tools": status["total_indexed_tools"],
            "cache_path": status["cache_path"],
            "servers": status["servers"],
            "errors": errors,
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
        if args.server and args.server in errors:
            return 1
        return 0
    finally:
        router.close()


def _format_audit_table(audit_data: dict[str, Any]) -> str:
    tools = audit_data["tools"]
    lines = [
        f"TDQS Audit Summary: {len(tools)} tool(s), avg={audit_data['average_score']}/100, "
        f"grades={audit_data['grade_distribution']}",
        "-" * 88,
        f"{'Tool':<34} {'Score':>5} {'Grade':>5}  {'Safety':<18}  {'Top Smell'}",
        "-" * 88,
    ]
    for item in tools:
        smell = item["smells"][0] if item["smells"] else "None"
        lines.append(
            f"{item['qualified_name'][:34]:<34} {item['score']:>5} {item['grade']:>5}  "
            f"{item['safety']:<18}  {smell}"
        )
    return "\n".join(lines)


def _format_audit_markdown(audit_data: dict[str, Any]) -> str:
    tools = audit_data["tools"]
    lines = [
        "# MCP Tool Description Quality Score (TDQS) Audit",
        "",
        f"- **Total Indexed Tools:** {len(tools)}",
        f"- **Average TDQS Score:** {audit_data['average_score']}/100",
        f"- **Grade Distribution:** {json.dumps(audit_data['grade_distribution'])}",
        "",
        "| Tool | Score | Grade | Safety | Smells |",
        "| :--- | :---: | :---: | :--- | :--- |",
    ]
    for item in tools:
        smells = "; ".join(item["smells"]) if item["smells"] else "None"
        lines.append(
            f"| `{item['qualified_name']}` | {item['score']} | {item['grade']} | "
            f"{item['safety']} | {smells} |"
        )
    return "\n".join(lines)


def _cmd_audit(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    router = LazyRouter(cfg)
    try:
        errors = router.ensure_indexed(refresh=args.refresh)
        all_tools = sorted(router.get_all_tools(), key=lambda t: (t.tdqs.score, t.qualified_name))
        grades = {"A": 0, "B": 0, "C": 0, "D": 0, "F": 0}
        tool_rows: list[dict[str, Any]] = []
        for t in all_tools:
            grades[t.tdqs.grade] = grades.get(t.tdqs.grade, 0) + 1
            safety = (
                "destructive"
                if t.inferred_destructive
                else ("read-only" if t.inferred_read_only else "mutating")
            )
            tool_rows.append({
                "qualified_name": t.qualified_name,
                "score": t.tdqs.score,
                "grade": t.tdqs.grade,
                "safety": f"{safety} ({t.safety_source})",
                "smells": t.tdqs.smells,
                "suggestions": t.tdqs.suggestions,
                "breakdown": t.tdqs.breakdown,
            })

        avg = round(sum(t.tdqs.score for t in all_tools) / len(all_tools), 2) if all_tools else 0.0
        audit_data = {
            "average_score": avg,
            "grade_distribution": grades,
            "server_errors": errors,
            "tools": tool_rows,
        }

        if args.format == "json":
            print(json.dumps(audit_data, ensure_ascii=False, indent=2))
        elif args.format == "markdown":
            print(_format_audit_markdown(audit_data))
        else:
            print(_format_audit_table(audit_data))
        return 0
    finally:
        router.close()


def _cmd_search(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    router = LazyRouter(cfg)
    try:
        res = router.search_tools(
            query=args.query,
            server=args.server,
            read_only_only=args.read_only,
            limit=args.limit,
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    finally:
        router.close()


def _cmd_inspect(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    router = LazyRouter(cfg)
    try:
        res = router.get_tool_schemas([{"server": args.server, "tool": args.tool}])
        print(json.dumps(res, ensure_ascii=False, indent=2))
        schemas = res.get("schemas", [])
        if not schemas or "error" in schemas[0]:
            return 1
        return 0
    finally:
        router.close()


def _cmd_import_config(args: argparse.Namespace) -> int:
    imported = import_client_config(args.source)
    rendered = json.dumps(imported, ensure_ascii=False, indent=2)
    if args.out:
        out_path = Path(args.out).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered + "\n", encoding="utf-8")
        print(
            f"Imported {len(imported['servers'])} server(s) into {out_path}"
        )
    else:
        print(rendered)
    return 0


def _cmd_benchmark(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    router = LazyRouter(cfg)
    try:
        errors = router.ensure_indexed(refresh=False)
        status = router.get_status(
            refresh_index=False,
            include_tdqs_audit=False,
            router_meta_tools=get_meta_tools(include_output_schema=True),
        )
        telem = status["token_savings"]
        print("MCP Lazy Router Context Token Benchmark")
        print("=" * 46)
        print(f"Enabled Servers:         {status['enabled_servers']}")
        print(f"Total Indexed Tools:     {status['total_indexed_tools']}")
        print(f"Upstream Raw Tokens:     {telem['upstream_raw_tokens']}")
        print(f"Router Meta-Tool Tokens: {telem['router_meta_tokens']}")
        print(f"Tokens Saved Upfront:    {telem['tokens_saved']}")
        print(f"Context Reduction:       {telem['reduction_percent']}%")
        if errors:
            print(f"Server Errors:           {json.dumps(errors)}")
        return 0
    finally:
        router.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-lazy-router",
        description="Dynamic lazy-loading MCP router and TDQS tool-description optimizer.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="Run the MCP stdio server.")
    p_serve.add_argument("--config", required=True, help="Path to router config JSON.")
    p_serve.add_argument(
        "--eager-index",
        action="store_true",
        help="Eagerly index uncached servers before serving requests.",
    )
    p_serve.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Stderr logging verbosity.",
    )
    p_serve.set_defaults(func=_cmd_serve)

    p_index = sub.add_parser("index", help="Refresh and persist downstream tool indexes.")
    p_index.add_argument("--config", required=True, help="Path to router config JSON.")
    p_index.add_argument("--server", default=None, help="Optional single server name to index.")
    p_index.set_defaults(func=_cmd_index)

    p_audit = sub.add_parser("audit", help="Audit downstream tool descriptions with TDQS.")
    p_audit.add_argument("--config", required=True, help="Path to router config JSON.")
    p_audit.add_argument(
        "--format",
        default="table",
        choices=["table", "markdown", "json"],
        help="Output format (default: table).",
    )
    p_audit.add_argument(
        "--refresh",
        action="store_true",
        help="Re-fetch tools from downstream servers before auditing.",
    )
    p_audit.set_defaults(func=_cmd_audit)

    p_search = sub.add_parser("search", help="Search indexed tools via BM25 + synonyms.")
    p_search.add_argument("--config", required=True, help="Path to router config JSON.")
    p_search.add_argument("query", help="Search query or '*'.")
    p_search.add_argument("--server", default=None, help="Filter by server name.")
    p_search.add_argument("--read-only", action="store_true", help="Only return read-only tools.")
    p_search.add_argument("--limit", type=int, default=8, help="Max results (default: 8).")
    p_search.set_defaults(func=_cmd_search)

    p_inspect = sub.add_parser("inspect", help="Inspect full normalized schema and TDQS for a tool.")
    p_inspect.add_argument("--config", required=True, help="Path to router config JSON.")
    p_inspect.add_argument("server", help="Downstream server name.")
    p_inspect.add_argument("tool", help="Downstream tool name.")
    p_inspect.set_defaults(func=_cmd_inspect)

    p_import = sub.add_parser(
        "import-config",
        help="Import Claude/Cursor/Antigravity mcpServers config into router format.",
    )
    p_import.add_argument("source", help="Path to existing MCP client config JSON.")
    p_import.add_argument("--out", default=None, help="Optional output file path.")
    p_import.set_defaults(func=_cmd_import_config)

    p_bench = sub.add_parser("benchmark", help="Measure upfront context token savings.")
    p_bench.add_argument("--config", required=True, help="Path to router config JSON.")
    p_bench.set_defaults(func=_cmd_benchmark)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
