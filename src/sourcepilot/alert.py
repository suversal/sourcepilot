"""采集中断告警：把「看得见地坏掉」变成「主动来找你」。

Canary（`canary.py`）已经能正确判定每个源的健康度，`/health` 也一直答得出来
——**但没人会去看**。2026-08-08 公众号线因为出口 IP 被风控而停掉，到 08-17
才被发现，中间 9 天里 `/health` 每一次都如实报着 `down`。一个源坏掉是必然的，
9 天发现不了才是真问题。

所以这一层只做一件事：**状态发生变化时推一条消息出去**。

三条设计约定：

1. **只在转换时发**，不是每轮都发。`ok/degraded → down` 发一条故障，
   `down → 非 down` 发一条恢复。一个源一直 down 不会每分钟吵一次。
   `degraded` 本身不发——落后几分钟、条目数掉一半这类波动太频繁，
   告警一吵人就不看了，那等于回到没有告警。
2. **已推送状态存库**（`alert_state` 表），不存内存。否则每次重启进程都会
   把同一批陈年故障重推一遍。
3. **推送失败不更新状态**，下一轮自然重试。反过来做（先记已通知、再发送）
   会让一次网络抖动永久吞掉一条告警——而告警恰恰是「出问题时」才用的东西，
   那时候网络本来就更可能不好。

发送本身是 best-effort：**绝不抛异常、绝不阻塞采集**。告警挂掉是小事，
把调度线程带崩是大事。

用法（复用 AIRADAR 那个机器人即可，两边同一套环境变量）：

    export TELEGRAM_BOT_TOKEN=...
    export TELEGRAM_CHAT_ID=...
    python -m sourcepilot.alert --test    # 先验一条测试消息通不通
    python -m sourcepilot.alert           # 检查一次并按需推送（也可挂 cron 兜底）

配了这两个变量，API 进程里的调度器每轮采集后会自动检查；没配就整个跳过，
不影响任何其它功能。
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime

from .canary import Canary, Health, SourceHealthReport
from .settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from .store import Store

log = logging.getLogger("sourcepilot.alert")

TELEGRAM_API = "https://api.telegram.org"
#: Telegram 单条消息上限 4096 字符，留点余量给截断提示。
MAX_MESSAGE_LENGTH = 4000
SEND_TIMEOUT = 10.0

#: 发送函数的签名：收一段文本，回「发出去了没有」。测试替换它。
Sender = Callable[[str], bool]


def send_telegram(
    text: str,
    *,
    bot_token: str | None = None,
    chat_id: str | None = None,
    timeout: float = SEND_TIMEOUT,
) -> bool:
    """推一条 Telegram 消息。**任何失败都只返回 False，不抛。**

    与 AIRADAR 的 `telegram_notifier` 用同一对环境变量，同一个机器人可以直接复用
    ——两边发的是不同的东西（那边是同步报告，这边是采集故障），共用通道没问题。
    """
    token = bot_token or TELEGRAM_BOT_TOKEN
    chat = chat_id or TELEGRAM_CHAT_ID
    if not token or not chat:
        log.debug("没配 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，跳过推送")
        return False

    if len(text) > MAX_MESSAGE_LENGTH:
        text = text[:MAX_MESSAGE_LENGTH] + "\n…（消息过长，已截断）"

    payload = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode("utf-8")
    request = urllib.request.Request(f"{TELEGRAM_API}/bot{token}/sendMessage", data=payload)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
            ok = bool(body.get("ok"))
            if not ok:
                # 401/400 这类是配置问题（token 错、chat_id 错、没先跟机器人说过话），
                # 重试一万次也不会好，所以把对方的说法原样记下来。
                log.warning("Telegram 拒绝了这条消息：%s", body.get("description"))
            return ok
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        log.warning("Telegram 推送失败（下一轮会重试）：%s", exc)
        return False
    except Exception:  # pragma: no cover - 兜底：告警绝不能把调用方带崩
        log.exception("Telegram 推送出现意外错误")
        return False


def configured() -> bool:
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def _age(report: SourceHealthReport, now: datetime) -> str:
    if report.last_success_at is None:
        return "从未成功过"
    hours = (now - report.last_success_at).total_seconds() / 3600
    when = report.last_success_at.strftime("%m-%d %H:%M")
    if hours >= 48:
        return f"上次成功 {when}Z（{hours / 24:.1f} 天前）"
    return f"上次成功 {when}Z（{hours:.1f} 小时前）"


def format_alert(
    newly_down: list[SourceHealthReport],
    recovered: list[str],
    counts: dict[str, int],
    now: datetime,
) -> str:
    """纯格式化，不出网——所以能在测试里逐字钉住。"""
    lines = ["🛰 SourcePilot 采集告警"]

    for report in newly_down:
        lines.append("")
        lines.append(f"❌ {report.name}：{report.reason}")
        lines.append(f"   {_age(report, now)}")

    if recovered:
        lines.append("")
        lines.append(f"✅ 已恢复：{'、'.join(recovered)}")

    total = sum(counts.values())
    detail = " · ".join(f"{k} {v}" for k, v in sorted(counts.items()))
    lines.append("")
    lines.append(f"{total} 个源：{detail}")
    return "\n".join(lines)


class Alerter:
    """比较「Canary 现在的判定」与「上次推送过的状态」，只推差异。"""

    def __init__(self, canary: Canary, store: Store, sender: Sender | None = None) -> None:
        self.canary = canary
        self.store = store
        self.sender = sender or send_telegram

    def poll(self, now: datetime | None = None) -> str | None:
        """检查一次。有变化就推，返回推出去的消息；无变化或推送失败返回 None。"""
        now = now or datetime.now(UTC)
        reports = self.canary.check_all(now)
        by_name = {r.name: r for r in reports}
        down_now = {r.name for r in reports if r.status is Health.DOWN}
        already = self.store.alert_states()

        newly_down = [by_name[n] for n in sorted(down_now - set(already))]
        # 之前报过、现在不 down 了。也包括源被禁用/删掉的情况——那同样是
        # 「不用再盯着了」，把标记清掉，别让它永远挂在已通知列表里。
        recovered = sorted(n for n in already if n not in down_now)

        if not newly_down and not recovered:
            return None

        counts: dict[str, int] = {}
        for r in reports:
            counts[r.status.value] = counts.get(r.status.value, 0) + 1
        message = format_alert(newly_down, recovered, counts, now)

        if not self.sender(message):
            # 没发出去就**不动状态**，下一轮重试。先记后发会永久吞掉这条告警。
            return None

        for report in newly_down:
            self.store.set_alert_state(report.name, report.status.value, now)
        for name in recovered:
            self.store.clear_alert_state(name)
        log.info(
            "已推送采集告警：%d 个新故障、%d 个恢复", len(newly_down), len(recovered)
        )
        return message


def main(argv: list[str] | None = None) -> int:
    import sys

    from .sources.config import load_sources

    argv = sys.argv[1:] if argv is None else argv

    if not configured():
        print("✗ 没配 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，告警不会推送。")
        print("  两个变量与 AIRADAR 的 telegram_notifier 同名，同一个机器人可直接复用。")
        return 1

    if "--test" in argv:
        ok = send_telegram("🛰 SourcePilot 告警通道自检：这条能看到就说明配置正确。")
        print("✓ 测试消息已发出" if ok else "✗ 发送失败，看上面的日志")
        return 0 if ok else 1

    store = Store()
    canary = Canary(store, load_sources())
    message = Alerter(canary, store).poll()
    if message is None:
        summary = canary.summary()
        print(f"没有状态变化，不推送。当前：{summary['counts']}")
    else:
        print("已推送：")
        print(message)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
