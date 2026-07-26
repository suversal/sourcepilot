"""X 账号池与限流状态机。

参考 twscrape 的 `accounts_pool` + `_check_rep`，核心是**两件事必须分开判断**：

    临时限流  x-rate-limit-remaining == 0  → 锁到 reset 时间，换下一个账号
    账号废了  错误码 32/64/88/89/326、HTML+cf-ray → 永久停用，别再用它

搞混的代价是不对称的：把「废了」当「限流」会让你拿一个已封的账号反复去撞，
加速关联封号；把「限流」当「废了」只是白白少一个账号。所以判断从严。

账号凭据存在 gitignore 的本地文件里，本模块只读不写，也从不打印明文。
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ...contracts import AuthExpired, RateLimited
from ...settings import PROJECT_ROOT
from ..cooldown import COOLDOWNS
from .config import FATAL_ERROR_CODES

log = logging.getLogger("sourcepilot.channels.x")

ACCOUNTS_FILE = PROJECT_ROOT / "config" / "x_accounts.yaml"

#: 每个 UA 用账号名做种子固定下来。同一 session 里 UA 跳变本身就是可疑信号。
_UA_POOL = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.1 Safari/605.1.15",
]


@dataclass
class Account:
    name: str
    cookie: str
    #: 从 cookie 里的 ct0 取，GraphQL 要求 x-csrf-token 与之相等。
    csrf: str = ""
    #: 签发这份 cookie 的那个浏览器的 UA。**强烈建议填**——
    #: cookie 是 Chrome 150 签发的、请求却报称 Chrome 131，这个自相矛盾本身
    #: 就是风控信号。不填则按账号名从池里挑一个固定的。
    user_agent_override: str | None = None
    #: 同一浏览器的客户端提示头（sec-ch-ua 那几个）。填了能让指纹更自洽。
    client_hints: dict[str, str] = field(default_factory=dict)
    active: bool = True
    #: 每个 endpoint 单独记限流——搜索被限不代表时间线也被限。
    locked_until: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.csrf:
            self.csrf = _extract_ct0(self.cookie)

    @property
    def user_agent(self) -> str:
        if self.user_agent_override:
            return self.user_agent_override
        seed = int(hashlib.sha256(self.name.encode()).hexdigest()[:8], 16)
        return _UA_POOL[seed % len(_UA_POOL)]

    def headers(self, bearer: str) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {bearer}",
            "Cookie": self.cookie,
            "x-csrf-token": self.csrf,
            "User-Agent": self.user_agent,
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-active-user": "yes",
            "x-twitter-client-language": "en",
            "Referer": "https://x.com/",
            "Accept": "*/*",
        }
        headers.update(self.client_hints)
        return headers

    def __repr__(self) -> str:  # 别让 cookie 从日志或异常栈漏出去
        return f"<Account {self.name} active={self.active} cookie=***>"


def _extract_ct0(cookie: str) -> str:
    for part in cookie.split(";"):
        key, _, value = part.strip().partition("=")
        if key == "ct0":
            return value
    return ""


class AccountPool:
    def __init__(self, accounts: list[Account] | None = None) -> None:
        self.accounts = accounts or []

    @classmethod
    def load(cls, path: Path | None = None) -> AccountPool:
        path = path or ACCOUNTS_FILE
        if not path.exists():
            return cls([])
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        accounts = []
        for entry in data.get("accounts") or []:
            cookie = str(entry.get("cookie", ""))
            name = str(entry.get("name") or f"acct{len(accounts) + 1}")
            if not cookie:
                log.warning("账号 %s 没有 cookie，跳过", name)
                continue
            account = Account(
                name=name,
                cookie=cookie,
                user_agent_override=entry.get("user_agent"),
                client_hints=dict(entry.get("client_hints") or {}),
            )
            if not account.csrf:
                # 没有 ct0 就一定过不了 GraphQL，早点说清楚好过让它反复失败。
                log.warning("账号 %s 的 cookie 里没有 ct0，跳过", name)
                continue
            accounts.append(account)
        return cls(accounts)

    def usable(self, endpoint: str) -> list[Account]:
        now = time.time()
        return [
            a
            for a in self.accounts
            if a.active
            and a.locked_until.get(endpoint, 0) <= now
            and not COOLDOWNS.blocked(f"x:{a.name}")
        ]

    def acquire(self, endpoint: str) -> Account:
        candidates = self.usable(endpoint)
        if not candidates:
            if not self.accounts:
                raise AuthExpired("X 未配置账号")
            if not any(a.active for a in self.accounts):
                raise AuthExpired("X 账号全部失效")
            raise RateLimited("X 账号全部处于限流冷却中")
        # 轮换而不是总用第一个：把请求摊到各账号上，单个号的配额才不会先见底。
        candidates.sort(key=lambda a: a.locked_until.get(endpoint, 0))
        return candidates[0]

    # ---------- 状态机 ----------

    def note_rate_limit(self, account: Account, endpoint: str, reset_at: float | None) -> None:
        """临时限流：锁到 reset 时间，换账号。这是可恢复的。"""
        until = reset_at or (time.time() + 15 * 60)
        account.locked_until[endpoint] = until
        log.warning(
            "账号 %s 在 %s 上被限流，锁 %.0f 秒", account.name, endpoint, until - time.time()
        )

    def note_dead(self, account: Account, reason: str) -> None:
        """账号废了：永久停用。继续用它只会加速关联封号。"""
        account.active = False
        log.error("账号 %s 已停用：%s", account.name, reason)

    def classify(self, account: Account, endpoint: str, response) -> None:
        """把一次响应翻译成账号状态。参考 twscrape 的 `_check_rep`。

        判断顺序有讲究：先看「是不是废了」，再看「是不是限流」。反过来的话，
        一个被封的账号会因为限流头恰好正常而被当成健康账号继续用。
        """
        body = response.text[:2000]

        # 1) Cloudflare 拦截：返回 HTML 而不是 JSON，且带 cf-ray 头。
        if "cf-ray" in response.headers and "<html" in body.lower():
            self.note_dead(account, "被 Cloudflare 拦截")
            raise AuthExpired("X 拒绝了该请求")

        # 2) 业务错误码。
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        for error in payload.get("errors") or []:
            code = error.get("code")
            if code in FATAL_ERROR_CODES:
                self.note_dead(account, f"X 错误码 {code}")
                raise AuthExpired("X 账号不可用")

        # 3) 限流头：这是临时的，锁一会儿换个号即可。
        remaining = response.headers.get("x-rate-limit-remaining")
        if remaining is not None and remaining.isdigit() and int(remaining) == 0:
            reset = response.headers.get("x-rate-limit-reset")
            self.note_rate_limit(
                account, endpoint, float(reset) if reset and reset.isdigit() else None
            )
            raise RateLimited("X 触发限流，已换账号重试")

        if response.status_code == 429:
            self.note_rate_limit(account, endpoint, None)
            raise RateLimited("X 触发限流")
