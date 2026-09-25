#!/usr/bin/env python3
"""Launch the read-only NIOME operations dashboard."""

from __future__ import annotations

import os
from pathlib import Path
import sys

import uvicorn


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


if __name__ == "__main__":
    uvicorn.run(
        "niome_subnet.dashboard.server:app",
        host=os.getenv("NIOME_DASH_HOST", "127.0.0.1"),
        port=int(os.getenv("NIOME_DASH_PORT", "8111")),
        log_level=os.getenv("NIOME_DASH_LOG_LEVEL", "info"),
        access_log=False,
    )
