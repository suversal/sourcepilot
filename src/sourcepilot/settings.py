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

#: fake-ip 段：Clash/TUN 这类代理在 fake-ip 模式下不做真实 DNS，而是把每个域名
#: 映射到这个段里的一个占位地址（mihomo 默认 198.18.0.0/16，IPv6 侧
#: fdfe:dcba:9876::/48）。`read_article` 的 SSRF 校验看的是解析结果，于是在这种
#: 环境下**所有公网域名都会被判成私网而拒绝**——而本项目的部署方式（Mac mini +
#: Clash Verge）正好如此。所以这些段要按「域名的占位地址」而不是「内网地址」处理，
#: 理由与安全边界见 `article.py` 的 `_is_public_ip`。
#: 逗号分隔的 CIDR；设成空串即关闭这项放行（不用代理的部署可以关掉）。
FAKE_IP_CIDRS = os.getenv(
    "SOURCEPILOT_FAKE_IP_CIDRS", "198.18.0.0/15,fdfe:dcba:9876::/48"
)

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
