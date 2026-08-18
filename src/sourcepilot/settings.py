"""运行时路径与全局默认值。"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_dotenv(path: Path) -> dict[str, str]:
    """把 `.env` 里的键值读进环境变量，**已存在的不覆盖**，返回实际注入的那些。

    为什么进程自己读、而不是交给启动方式：凭据不能进仓库（`.idea/` 是跟着仓库
    走的，写进运行配置的 XML 等于直接推上 GitHub），而 IDEA 启动、命令行 uvicorn、
    cron 三条起法各有各的环境。自己读一份 `.env` 是唯一让三者拿到同一份配置的
    办法，也不必为此加 python-dotenv 依赖——需要的就是这十几行。

    真实环境变量优先（用 `setdefault`）：部署时用系统环境覆盖文件里的值是常规做法，
    反过来会让人怎么 export 都不生效。
    """
    injected: dict[str, str] = {}
    if not path.exists():
        return injected
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:  # 读不到就当没有，配置文件坏了不该让服务起不来
        return injected
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        # 去掉成对的引号；值里面的引号原样保留（token 里不会有，但别乱改人家的值）
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            injected[key] = value
    return injected


#: 项目根的 .env。已在 .gitignore 里，模板见 .env.example。
DOTENV_FILE = Path(os.getenv("SOURCEPILOT_DOTENV", PROJECT_ROOT / ".env"))
load_dotenv(DOTENV_FILE)

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

#: 采集中断告警的 Telegram 通道。**与 AIRADAR 的 telegram_notifier 同名**，
#: 同一个机器人可以直接复用（两边发的东西不同，共用通道没问题）。
#: 两个都不填 = 不推送，其余功能不受影响。见 alert.py。
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
