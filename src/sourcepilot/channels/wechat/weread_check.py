"""微信读书凭据自检 + 书架核对。

    python -m sourcepilot.channels.wechat.weread_check

做三件事，每件都对应一种真实的失败方式：

1. **书架接口通不通** —— 只验这个不够，但它能区分「cookie 没了」和「被风控」。
2. **换不换得到阅读器页通行证** —— `/web/mp/articles` 要带阅读器页 Referer，
   书架里得有**任意一个**公众号来换这张证。**不是每个订阅的号都要加书架**
   （那道校验不比对 bookId，实测见 weread.py 模块文档）。
3. **真拉一次文章** —— 拿配置里第一个号打真接口。前两步都过、这步挂，
   才说明是上下文或风控的问题。

只打必要的请求。微信读书对这条路有反爬，反复探测正是触发它的原因。
"""

from __future__ import annotations

import sys

from ...contracts import SourcePilotError
from ...sources.config import load_sources
from .weread import WereadClient, WereadCredentials, book_id_for


def check() -> int:
    credentials = WereadCredentials.load()
    if credentials is None:
        print("✗ 没有凭据。请在 config/weread_credentials.yaml 里写：")
        print('    cookie: "wr_vid=...; wr_skey=..."')
        print("  取法：浏览器登录 weread.qq.com → 开发者工具 → Network →")
        print("        任一请求的 Request Headers → 复制整条 Cookie")
        return 1

    missing = credentials.missing_keys()
    if missing:
        # 先判形状再发请求：缺 wr_skey 时接口回的是 -2010/-2041，看着像风控或
        # 地址失效，会把人带偏（2026-08-07 为此排查了半天）。
        print(f"✗ cookie 里缺少必需项：{', '.join(missing)}")
        print("  只有 _qimei_*/_clck 这些埋点键是不够的——那说明复制时没登录，")
        print("  或者是在别的腾讯域名下复制的。")
        print("  正确做法：确认 weread.qq.com 左上角显示你的头像后，")
        print("  F12 → Application → Cookies → https://weread.qq.com，")
        print("  确认列表里能看到 wr_vid 和 wr_skey，再整串复制。")
        return 1

    client = WereadClient(credentials)

    try:
        shelf = client.shelf()
    except SourcePilotError as exc:
        print(f"✗ 书架接口失败：{exc.code.value} {exc.message}")
        if exc.code.value == "AUTH_EXPIRED":
            print("  登录态失效，重新登录 weread.qq.com 后再复制一次 Cookie。")
        elif exc.code.value == "RATE_LIMITED":
            print("  触发风控了。停手，隔几个小时再试——别反复重试。")
        return 1
    print(f"✓ 书架接口正常，里面有 {len(shelf)} 个公众号")

    try:
        reader_url = client.reader_ticket()
    except SourcePilotError as exc:
        print(f"✗ {exc.message}")
        return 1
    print("✓ 已换到阅读器页通行证（订阅的号不必逐个加书架，有一个能换证就行）")

    accounts = load_sources()["wechat"].accounts
    usable = [(a.name, book_id_for(a.fakeid)) for a in accounts if a.fakeid]
    usable = [(n, b) for n, b in usable if b]
    skipped = len(accounts) - len(usable)
    print(f"✓ 配置里 {len(accounts)} 个号，{len(usable)} 个能定位到 bookId")
    if skipped:
        print(f"⚠ {skipped} 个号没配 fakeid 或解不出 bookId，会被跳过")

    if not usable:
        print("\n一个号都定位不了，先把 fakeid 补进 config/sources/wechat.yaml。")
        return 1

    name, book_id = usable[0]
    try:
        articles = client.articles(book_id, reader_url)
    except SourcePilotError as exc:
        print(f"\n✗ 拉「{name}」的文章失败：{exc.code.value} {exc.message}")
        return 1

    print(f"\n✓ 真实拉取「{name}」成功，{len(articles)} 篇")
    for article in articles[:3]:
        print(f"    - {article['title'][:40]}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(check())
