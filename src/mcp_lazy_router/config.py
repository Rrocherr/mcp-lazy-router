"""Configuration loading, validation, env expansion, and client config importing."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(ValueError):
    """Raised when router configuration is missing or invalid."""


@dataclass(slots=True)
class PolicyConfig:
    """Global routing, safety, and output truncation policies."""

    read_only: bool = False
    require_confirmation_for_destructive: bool = True
    strict_destructive_heuristics: bool = True
    allowed_tools: list[str] = field(default_factory=list)
    blocked_tools: list[str] = field(default_factory=list)
    max_response_chars: int = 24000
    spill_oversized_responses: bool = True


@dataclass(slots=True)
class ServerConfig:
    """Configuration for a single downstream MCP server."""

    name: str
    transport: str
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    timeout_seconds: float = 30.0
    idle_timeout_seconds: float = 300.0
    read_only: bool | None = None
    allowed_tools: list[str] = field(default_factory=list)
    blocked_tools: list[str] = field(default_factory=list)
    description: str = ""
    tags: list[str] = field(default_factory=list)

    def fingerprint(self) -> str:
        """Return a stable SHA-256 hash of server identity fields for cache invalidation."""
        identity = {
            "transport": self.transport,
            "command": self.command,
            "args": self.args,
            "url": self.url,
        }
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(slots=True)
class RouterConfig:
    """Complete parsed router configuration."""

    servers: dict[str, ServerConfig]
    cache_path: Path
    spill_dir: Path
    cache_ttl_seconds: float = 86400.0
    policy: PolicyConfig = field(default_factory=PolicyConfig)


def expand_env_vars(
    value: str,
    environ: Mapping[str, str] | None = None,
    strict: bool = False,
) -> tuple[str, list[str]]:
    """Expand ${VAR} and ${VAR:-default} placeholders in a string.

    Returns (expanded_string, missing_variable_names).
    Raises ConfigError if strict=True and any variable without a default is missing.
    """
    env_map = os.environ if environ is None else environ
    missing: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        var_name = match.group(1)
        default_val = match.group(2)
        if var_name in env_map:
            raw_val = env_map[var_name]
            if raw_val == "" and default_val is not None:
                return default_val
            return raw_val
        if default_val is not None:
            return default_val
        if var_name not in missing:
            missing.append(var_name)
        return match.group(0)

    expanded = _ENV_PATTERN.sub(_replace, value)
    if strict and missing:
        raise ConfigError(f"Missing required environment variable(s): {', '.join(missing)}")
    return expanded, missing


def _ensure_string_list(raw: Any, field_name: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ConfigError(f"'{field_name}' must be a list of strings")
    return list(raw)


def _ensure_string_dict(raw: Any, field_name: str) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"'{field_name}' must be an object mapping strings to strings")
    result: dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not isinstance(v, (str, int, float, bool)):
            raise ConfigError(f"'{field_name}' values must be scalar strings")
        result[k] = str(v)
    return result


def _parse_policy(raw_policy: Any) -> PolicyConfig:
    if raw_policy is None:
        return PolicyConfig()
    if not isinstance(raw_policy, dict):
        raise ConfigError("'policy' must be a JSON object")

    max_chars = raw_policy.get("max_response_chars", 24000)
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars <= 0:
        raise ConfigError("'policy.max_response_chars' must be a positive integer")

    return PolicyConfig(
        read_only=bool(raw_policy.get("read_only", False)),
        require_confirmation_for_destructive=bool(
            raw_policy.get("require_confirmation_for_destructive", True)
        ),
        strict_destructive_heuristics=bool(
            raw_policy.get("strict_destructive_heuristics", True)
        ),
        allowed_tools=_ensure_string_list(raw_policy.get("allowed_tools"), "policy.allowed_tools"),
        blocked_tools=_ensure_string_list(raw_policy.get("blocked_tools"), "policy.blocked_tools"),
        max_response_chars=max_chars,
        spill_oversized_responses=bool(raw_policy.get("spill_oversized_responses", True)),
    )


def _parse_server(name: str, raw: Any, base_dir: Path) -> ServerConfig:
    if not isinstance(name, str) or not name.strip():
        raise ConfigError("Server name must be a non-empty string")
    if not isinstance(raw, dict):
        raise ConfigError(f"Server '{name}' configuration must be a JSON object")

    raw_url = raw.get("url") or raw.get("serverUrl")
    raw_command = raw.get("command")
    raw_transport = raw.get("transport")

    if raw_transport is None:
        if raw_url is not None:
            transport = "http"
        elif raw_command is not None:
            transport = "stdio"
        else:
            raise ConfigError(
                f"Server '{name}' must specify 'transport' or one of 'command' / 'url'"
            )
    else:
        if not isinstance(raw_transport, str):
            raise ConfigError(f"Server '{name}.transport' must be a string")
        normalized = raw_transport.strip().lower()
        if normalized in ("streamable_http", "streamable-http"):
            normalized = "http"
        if normalized not in ("stdio", "http"):
            raise ConfigError(
                f"Server '{name}' has unsupported transport '{raw_transport}'; use 'stdio' or 'http'"
            )
        transport = normalized

    args = _ensure_string_list(raw.get("args"), f"servers.{name}.args")
    env = _ensure_string_dict(raw.get("env"), f"servers.{name}.env")
    headers = _ensure_string_dict(raw.get("headers"), f"servers.{name}.headers")

    cwd_val = raw.get("cwd")
    resolved_cwd: str | None = None
    if cwd_val is not None:
        if not isinstance(cwd_val, str) or not cwd_val.strip():
            raise ConfigError(f"Server '{name}.cwd' must be a non-empty string")
        cwd_path = Path(cwd_val)
        if not cwd_path.is_absolute():
            cwd_path = (base_dir / cwd_path).resolve()
        resolved_cwd = str(cwd_path)

    if transport == "stdio":
        if not isinstance(raw_command, str) or not raw_command.strip():
            raise ConfigError(f"Stdio server '{name}' requires a non-empty 'command'")
        command = raw_command
        url = None
    else:
        if not isinstance(raw_url, str) or not raw_url.strip():
            raise ConfigError(f"HTTP server '{name}' requires a non-empty 'url'")
        url = raw_url
        command = None

    timeout = raw.get("timeout_seconds", 30.0)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ConfigError(f"Server '{name}.timeout_seconds' must be a positive number")

    idle_timeout = raw.get("idle_timeout_seconds", 300.0)
    if (
        isinstance(idle_timeout, bool)
        or not isinstance(idle_timeout, (int, float))
        or idle_timeout < 0
    ):
        raise ConfigError(f"Server '{name}.idle_timeout_seconds' must be non-negative")

    read_only = raw.get("read_only")
    if read_only is not None and not isinstance(read_only, bool):
        raise ConfigError(f"Server '{name}.read_only' must be a boolean if provided")

    description = raw.get("description", "")
    if not isinstance(description, str):
        raise ConfigError(f"Server '{name}.description' must be a string")

    return ServerConfig(
        name=name,
        transport=transport,
        command=command,
        args=args,
        env=env,
        cwd=resolved_cwd,
        url=url,
        headers=headers,
        enabled=bool(raw.get("enabled", True)),
        timeout_seconds=float(timeout),
        idle_timeout_seconds=float(idle_timeout),
        read_only=read_only,
        allowed_tools=_ensure_string_list(
            raw.get("allowed_tools"), f"servers.{name}.allowed_tools"
        ),
        blocked_tools=_ensure_string_list(
            raw.get("blocked_tools"), f"servers.{name}.blocked_tools"
        ),
        description=description,
        tags=_ensure_string_list(raw.get("tags"), f"servers.{name}.tags"),
    )


def load_config(path: str | Path) -> RouterConfig:
    """Load and validate a router configuration file (native or mcpServers format)."""
    config_path = Path(path).resolve()
    if not config_path.is_file():
        raise ConfigError(f"Configuration file not found: {config_path}")

    try:
        raw_text = config_path.read_text(encoding="utf-8-sig")
        data = json.loads(raw_text)
    except OSError as exc:
        raise ConfigError(f"Unable to read configuration file {config_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {config_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError("Top-level configuration must be a JSON object")

    raw_servers = data.get("servers")
    if raw_servers is None:
        raw_servers = data.get("mcpServers")
    if not isinstance(raw_servers, dict):
        raise ConfigError("Configuration must define a 'servers' or 'mcpServers' object")

    base_dir = config_path.parent
    servers: dict[str, ServerConfig] = {}
    for srv_name, srv_raw in raw_servers.items():
        servers[srv_name] = _parse_server(srv_name, srv_raw, base_dir)

    raw_cache = data.get("cache_path", ".mcp_lazy_cache.json")
    if not isinstance(raw_cache, str) or not raw_cache.strip():
        raise ConfigError("'cache_path' must be a non-empty string")
    cache_path = Path(raw_cache)
    if not cache_path.is_absolute():
        cache_path = (base_dir / cache_path).resolve()

    raw_spill = data.get("spill_dir", ".mcp_lazy_spills")
    if not isinstance(raw_spill, str) or not raw_spill.strip():
        raise ConfigError("'spill_dir' must be a non-empty string")
    spill_dir = Path(raw_spill)
    if not spill_dir.is_absolute():
        spill_dir = (base_dir / spill_dir).resolve()

    cache_ttl = data.get("cache_ttl_seconds", 86400.0)
    if isinstance(cache_ttl, bool) or not isinstance(cache_ttl, (int, float)) or cache_ttl < 0:
        raise ConfigError("'cache_ttl_seconds' must be a non-negative number")

    policy = _parse_policy(data.get("policy"))

    return RouterConfig(
        servers=servers,
        cache_path=cache_path,
        spill_dir=spill_dir,
        cache_ttl_seconds=float(cache_ttl),
        policy=policy,
    )


def _is_self_referential_server(name: str, entry: dict[str, Any]) -> bool:
    """Detect whether an MCP server entry points back to mcp-lazy-router itself."""
    markers = ("mcp-lazy-router", "mcp_lazy_router")
    if any(m in name.lower() for m in markers):
        return True
    command = str(entry.get("command", "")).lower()
    if any(m in command for m in markers):
        return True
    args = entry.get("args")
    if isinstance(args, list):
        for arg in args:
            if isinstance(arg, str) and any(m in arg.lower() for m in markers):
                return True
    return False


def import_client_config(source_path: str | Path) -> dict[str, Any]:
    """Import an existing Claude/Cursor/Antigravity MCP config into router config format.

    Automatically filters out self-referential mcp-lazy-router entries to avoid recursion.
    """
    src = Path(source_path).resolve()
    if not src.is_file():
        raise ConfigError(f"Source config file not found: {src}")

    try:
        data = json.loads(src.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Failed to parse client config {src}: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError("Client config root must be a JSON object")

    raw_servers = data.get("mcpServers")
    if raw_servers is None:
        raw_servers = data.get("servers")
    if not isinstance(raw_servers, dict):
        raise ConfigError("Client config must contain 'mcpServers' (or 'servers')")

    imported_servers: dict[str, Any] = {}
    for name, entry in raw_servers.items():
        if not isinstance(entry, dict):
            continue
        if _is_self_referential_server(name, entry):
            continue

        item: dict[str, Any] = {}
        url = entry.get("url") or entry.get("serverUrl")
        if url:
            item["transport"] = "http"
            item["url"] = url
            if isinstance(entry.get("headers"), dict):
                item["headers"] = dict(entry["headers"])
        elif entry.get("command"):
            item["transport"] = "stdio"
            item["command"] = entry["command"]
            if isinstance(entry.get("args"), list):
                item["args"] = list(entry["args"])
            if isinstance(entry.get("env"), dict):
                item["env"] = dict(entry["env"])
            if isinstance(entry.get("cwd"), str):
                item["cwd"] = entry["cwd"]
        else:
            continue

        if "disabled" in entry:
            item["enabled"] = not bool(entry["disabled"])
        elif "enabled" in entry:
            item["enabled"] = bool(entry["enabled"])
        else:
            item["enabled"] = True

        imported_servers[name] = item

    return {
        "cache_path": ".mcp_lazy_cache.json",
        "spill_dir": ".mcp_lazy_spills",
        "cache_ttl_seconds": 86400,
        "policy": {
            "read_only": False,
            "require_confirmation_for_destructive": True,
            "strict_destructive_heuristics": True,
            "allowed_tools": [],
            "blocked_tools": [],
            "max_response_chars": 24000,
            "spill_oversized_responses": True,
        },
        "servers": imported_servers,
    }


def _candidate_client_paths() -> dict[str, Path]:
    home = Path.home()
    if sys.platform == "win32":
        appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        claude_path = appdata / "Claude" / "claude_desktop_config.json"
    elif sys.platform == "darwin":
        claude_path = (
            home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
        )
    else:
        xdg = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
        claude_path = xdg / "Claude" / "claude_desktop_config.json"

    return {
        "antigravity": home / ".gemini" / "config" / "mcp_config.json",
        "claude": claude_path,
        "cursor": home / ".cursor" / "mcp.json",
    }


def detect_client_config_path(
    client: str = "auto",
    custom_path: str | Path | None = None,
) -> tuple[str, Path]:
    """Resolve the target client name and MCP configuration file path."""
    if custom_path is not None:
        resolved_client = client if client != "auto" else "custom"
        return resolved_client, Path(custom_path).resolve()

    candidates = _candidate_client_paths()
    if client != "auto":
        if client not in candidates:
            raise ConfigError(
                f"Unknown client '{client}'; choose from auto, antigravity, claude, cursor"
            )
        return client, candidates[client].resolve()

    for name in ("antigravity", "claude", "cursor"):
        p = candidates[name]
        if p.is_file():
            return name, p.resolve()
    for name in ("antigravity", "claude", "cursor"):
        p = candidates[name]
        if p.parent.is_dir():
            return name, p.resolve()
    return "antigravity", candidates["antigravity"].resolve()


def _extract_non_self_servers(raw_servers: Any) -> dict[str, Any]:
    if not isinstance(raw_servers, dict):
        return {}
    imported_servers: dict[str, Any] = {}
    for name, entry in raw_servers.items():
        if not isinstance(entry, dict) or _is_self_referential_server(name, entry):
            continue
        item: dict[str, Any] = {}
        url = entry.get("url") or entry.get("serverUrl")
        if url:
            item["transport"] = "http"
            item["url"] = url
            if isinstance(entry.get("headers"), dict):
                item["headers"] = dict(entry["headers"])
        elif entry.get("command"):
            item["transport"] = "stdio"
            item["command"] = entry["command"]
            if isinstance(entry.get("args"), list):
                item["args"] = list(entry["args"])
            if isinstance(entry.get("env"), dict):
                item["env"] = dict(entry["env"])
            if isinstance(entry.get("cwd"), str):
                item["cwd"] = entry["cwd"]
        else:
            continue

        if "disabled" in entry:
            item["enabled"] = not bool(entry["disabled"])
        elif "enabled" in entry:
            item["enabled"] = bool(entry["enabled"])
        else:
            item["enabled"] = True

        imported_servers[name] = item
    return imported_servers


def setup_client_integration(
    client: str = "auto",
    client_config_path: str | Path | None = None,
    router_config_path: str | Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """One-command idempotent setup migrating existing MCP servers into router_config.json."""
    resolved_client, target_client_cfg = detect_client_config_path(client, client_config_path)
    if router_config_path is not None:
        target_router_cfg = Path(router_config_path).resolve()
    else:
        target_router_cfg = (target_client_cfg.parent / "router_config.json").resolve()

    client_data: dict[str, Any] = {}
    has_existing_client_file = target_client_cfg.is_file() and target_client_cfg.stat().st_size > 0
    if has_existing_client_file:
        try:
            parsed = json.loads(target_client_cfg.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(
                f"Failed to parse existing client config {target_client_cfg}: {exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise ConfigError(f"Client config {target_client_cfg} root must be a JSON object")
        client_data = dict(parsed)

    migrated_from_client = _extract_non_self_servers(
        client_data.get("mcpServers") or client_data.get("servers")
    )

    router_data: dict[str, Any] = {
        "cache_path": ".mcp_lazy_cache.json",
        "spill_dir": ".mcp_lazy_spills",
        "cache_ttl_seconds": 86400,
        "policy": {
            "read_only": False,
            "require_confirmation_for_destructive": True,
            "strict_destructive_heuristics": True,
            "allowed_tools": [],
            "blocked_tools": [],
            "max_response_chars": 24000,
            "spill_oversized_responses": True,
        },
        "servers": {},
    }
    if target_router_cfg.is_file() and target_router_cfg.stat().st_size > 0:
        try:
            existing_router = json.loads(target_router_cfg.read_text(encoding="utf-8-sig"))
            if isinstance(existing_router, dict):
                for k, v in existing_router.items():
                    if k != "servers":
                        router_data[k] = v
                existing_servers = _extract_non_self_servers(existing_router.get("servers"))
                router_data["servers"].update(existing_servers)
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(
                f"Failed to parse existing router config {target_router_cfg}: {exc}"
            ) from exc

    router_data["servers"].update(migrated_from_client)

    repo_launcher = Path(__file__).resolve().parent.parent.parent / "scripts" / "run_router.py"
    if repo_launcher.is_file():
        mcp_entry = {
            "command": sys.executable,
            "args": [
                str(repo_launcher.resolve()),
                "serve",
                "--config",
                str(target_router_cfg),
            ],
        }
    else:
        mcp_entry = {
            "command": sys.executable,
            "args": [
                "-m",
                "mcp_lazy_router",
                "serve",
                "--config",
                str(target_router_cfg),
            ],
        }

    backup_path: Path | None = None
    if not dry_run:
        target_router_cfg.parent.mkdir(parents=True, exist_ok=True)
        target_client_cfg.parent.mkdir(parents=True, exist_ok=True)
        if has_existing_client_file:
            backup_path = target_client_cfg.with_name(target_client_cfg.name + ".bak")
            shutil.copy2(target_client_cfg, backup_path)

        target_router_cfg.write_text(
            json.dumps(router_data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        updated_client = {k: v for k, v in client_data.items() if k != "servers"}
        updated_client["mcpServers"] = {"mcp-lazy-router": mcp_entry}
        target_client_cfg.write_text(
            json.dumps(updated_client, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    routed_list = list(router_data["servers"].keys())
    return {
        "client": resolved_client,
        "client_config_path": str(target_client_cfg),
        "backup_path": str(backup_path) if backup_path else None,
        "router_config_path": str(target_router_cfg),
        "total_routed_servers": len(routed_list),
        "routed_servers": routed_list,
        "dry_run": dry_run,
        "mcp_entry": mcp_entry,
    }
