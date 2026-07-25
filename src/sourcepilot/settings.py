"""运行时路径与全局默认值。"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

SOURCES_DIR = Path(os.getenv("SOURCEPILOT_SOURCES_DIR", PROJECT_ROOT / "config" / "sources"))
CATEGORIES_FILE = Path(
    os.getenv("SOURCEPILOT_CATEGORIES_FILE", PROJECT_ROOT / "config" / "categories.yaml")
)
DB_PATH = Path(os.getenv("SOURCEPILOT_DB", PROJECT_ROOT / "data" / "sourcepilot.db"))

#: 现查默认超时（秒）。契约 §3 的降级链以此为界。
LIVE_TIMEOUT = float(os.getenv("SOURCEPILOT_LIVE_TIMEOUT", "8"))

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
