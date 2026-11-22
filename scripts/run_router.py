#!/usr/bin/env python3
"""Zero-install launcher for mcp-lazy-router."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from mcp_lazy_router.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
