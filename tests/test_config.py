"""Unit tests for configuration loading, env expansion, and client config import."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from mcp_lazy_router.config import (
    ConfigError,
    expand_env_vars,
    import_client_config,
    load_config,
)


class ConfigTests(unittest.TestCase):
    def test_env_expansion_strict_and_defaults(self) -> None:
        env = {"TOKEN": "abc123", "EMPTY": ""}
        val, missing = expand_env_vars("Bearer ${TOKEN}/${EMPTY:-fallback}/${MISSING}", environ=env)
        self.assertEqual(val, "Bearer abc123/fallback/${MISSING}")
        self.assertEqual(missing, ["MISSING"])

        with self.assertRaises(ConfigError):
            expand_env_vars("${MISSING}", environ=env, strict=True)

    def test_load_native_and_mcp_servers_formats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            native_file = tmp_dir / "router.json"
            native_file.write_text(
                json.dumps({
                    "cache_path": "cache/idx.json",
                    "spill_dir": "spills",
                    "policy": {"read_only": True, "max_response_chars": 5000},
                    "servers": {
                        "local": {"command": "python", "args": ["-V"]},
                        "remote": {"url": "https://example.com/mcp"},
                    },
                }),
                encoding="utf-8",
            )
            cfg = load_config(native_file)
            self.assertEqual(cfg.cache_path, (tmp_dir / "cache/idx.json").resolve())
            self.assertEqual(cfg.spill_dir, (tmp_dir / "spills").resolve())
            self.assertTrue(cfg.policy.read_only)
            self.assertEqual(cfg.servers["local"].transport, "stdio")
            self.assertEqual(cfg.servers["remote"].transport, "http")

            client_file = tmp_dir / "claude.json"
            client_file.write_text(
                json.dumps({
                    "mcpServers": {
                        "gh": {"command": "npx", "args": ["-y", "gh-mcp"]},
                        "docs": {"serverUrl": "https://docs.example.com/mcp"},
                    }
                }),
                encoding="utf-8",
            )
            cfg2 = load_config(client_file)
            self.assertEqual(cfg2.servers["gh"].transport, "stdio")
            self.assertEqual(cfg2.servers["docs"].transport, "http")

    def test_invalid_configs_raise_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text('{"foo": 1}', encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(bad)

            bad.write_text('{"servers": {"s1": {"transport": "sse", "url": "http://x"}}}', encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(bad)

    def test_import_client_config_strips_self_references(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "mcp_config.json"
            src.write_text(
                json.dumps({
                    "mcpServers": {
                        "mcp-lazy-router": {
                            "command": "python",
                            "args": ["scripts/run_router.py", "serve"],
                        },
                        "self-by-args": {
                            "command": "python",
                            "args": ["-m", "mcp_lazy_router", "serve"],
                        },
                        "sqlite": {
                            "command": "sqlite-mcp",
                            "args": ["app.db"],
                            "disabled": True,
                        },
                        "remote": {
                            "url": "https://api.example.com/mcp",
                            "headers": {"Authorization": "Bearer ${API_KEY}"},
                        },
                    }
                }),
                encoding="utf-8",
            )
            imported = import_client_config(src)
            servers = imported["servers"]
            self.assertNotIn("mcp-lazy-router", servers)
            self.assertNotIn("self-by-args", servers)
            self.assertIn("sqlite", servers)
            self.assertFalse(servers["sqlite"]["enabled"])
            self.assertEqual(servers["remote"]["transport"], "http")


if __name__ == "__main__":
    unittest.main()
