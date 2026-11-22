# mcp-lazy-router

**Dynamic Lazy-Loading MCP Router & Tool-Description Optimizer (TDQS)**

`mcp-lazy-router` eliminates upfront MCP context-window bloat ("context tax") across Claude Desktop, Claude Code, Cursor, VS Code, and Antigravity. Instead of injecting dozens or hundreds of raw upstream tool schemas at session startup, it exposes **4 high-scoring meta-tools** (`search_tools`, `get_tool_schema`, `call_tool`, `router_status`) and loads schemas and downstream connections strictly on demand.

Zero external dependencies — runs on pure Python 3.10+ stdlib.

---

## Architecture

```mermaid
flowchart LR
    C["AI Client (Claude / Cursor / Antigravity)"] <-->|"4 Meta-Tools (Stdio JSON-RPC)"| R["mcp-lazy-router"]
    R -->|"Persistent Fingerprint Cache"| K[(".mcp_lazy_cache.json")]
    R -->|"BM25 + Synonym Index + TDQS Audit"| I["Search & Normalizer"]
    R -->|"Pre-flight Schema + Safety Guardrails"| V["Validator & Policy"]
    V -->|"Lazy Stdio Spawn"| S1["Downstream Stdio MCPs"]
    V -->|"Lazy Streamable HTTP + SSE"| S2["Downstream Remote MCPs"]
    V -->|"Oversized Output Spill"| F[(".mcp_lazy_spills/*.txt")]
```

### Core Capabilities
1. **Zero-Tax Progressive Disclosure:** Exposes only 4 meta-tools upfront (`tools/list`), cutting initial prompt token consumption dramatically as upstream servers scale.
2. **True Lazy Connections + Persistent Cache:** Warm cache loads from `.mcp_lazy_cache.json` in milliseconds with **0 downstream processes spawned** until `call_tool` executes.
3. **TDQS Auditor & Auto-Normalizer:** Evaluates every upstream tool on a 0–100 rubric across 6 dimensions (`purpose_clarity`, `usage_guidance`, `boundary_guidance`, `parameter_docs`, `safety_annotations`, `output_contract`) and enriches vague or missing descriptions deterministically without hallucination.
4. **Hybrid BM25 + Synonym Search:** Field-weighted Okapi BM25 over `camelCase`/`snake_case`-split names, servers, tags, descriptions, and parameters, with built-in synonym expansion (`pr`/`pull`, `db`/`sql`, `issue`/`ticket`, etc.).
5. **Pre-Flight Validation & Safety Guardrails:** Pure-Python JSON Schema validation prevents wasted downstream calls; `read_only` mode, allow/block globs, and `confirm_destructive` guardrails protect mutating endpoints.
6. **Oversized Response Spilling & Crash Recovery:** Truncates huge tool outputs cleanly to `max_response_chars`, writes full results to disk under `.mcp_lazy_spills/`, and safely retries crashed read-only/idempotent calls once.

---

## 1-Minute Quick Start (2 Commands)

No manual JSON editing required. `setup` automatically detects **Antigravity**, **Claude Desktop**, or **Cursor**, creates a `.bak` backup of your existing config, moves your current `mcpServers` into `router_config.json`, and wires `mcp-lazy-router` with absolute paths:

```bash
pip install git+https://github.com/RockeTTeee/mcp-lazy-router.git
mcp-lazy-router setup
```

*(Target a specific client with `mcp-lazy-router setup --client claude`, `--client cursor`, or `--client antigravity`, or preview first with `--dry-run`.)*

### Alternative: Zero-Install (`git clone`)

```bash
git clone https://github.com/RockeTTeee/mcp-lazy-router.git
cd mcp-lazy-router
python scripts/run_router.py setup
```

---

## CLI Reference

| Command | Purpose |
| :--- | :--- |
| `setup [--client auto\|antigravity\|claude\|cursor] [--dry-run]` | One-command auto-migration & client setup |
| `serve --config <path> [--eager-index]` | Run the MCP stdio server |
| `index --config <path> [--server <name>]` | Refresh and persist downstream tool indexes |
| `audit --config <path> [--format table\|markdown\|json] [--refresh]` | Run TDQS quality audit across indexed tools |
| `search --config <path> <query> [--server <name>] [--read-only] [--limit 8]` | Test BM25 + synonym search from CLI |
| `inspect --config <path> <server> <tool>` | Inspect normalized schema and TDQS breakdown |
| `import-config <source> [--out <path>]` | Convert existing `mcpServers` config to router format |
| `benchmark --config <path>` | Measure upfront context token savings |
