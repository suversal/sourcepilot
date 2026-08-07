"""从公众号文章链接解析 `__biz`（也就是配置里的 fakeid）。

    python -m sourcepilot.channels.wechat.biz <文章链接> [更多链接...]

**为什么需要这个**：weread 后端靠 `__biz` 算 bookId 才能定位公众号——它搜不了
名字也搜不了微信号（实测多种参数组合全返回图书）。而公众号分享出来的短链
（`/s/xxxxxx`）里根本没有 `__biz`，它只在页面 HTML 里。

带参数的长链（`/s?__biz=...&mid=...`）可以直接读，不必请求页面。

拿到之后写进 config/sources/wechat.yaml：

    - { name: 显示名, fakeid: <这里>, alias: <微信号> }
"""

from __future__ import annotations

import re
import sys
from urllib.parse import parse_qs, urlsplit

import httpx

from ...settings import DEFAULT_UA

#: 页面里 `__biz` 的两种出现形式。`var biz = "..."` 是主要的，
#: 另一个兜住带参数长链被跟随重定向后的情形。
_BIZ_PATTERNS = (
    re.compile(r'var\s+biz\s*=\s*"([A-Za-z0-9+/=]+)"'),
    re.compile(r'__biz=([A-Za-z0-9+/=%]+)'),
)
#: `gh_` 开头的原始 id，与公众号昵称。都是给人核对「解出来的是不是想要的那个号」。
_GH_ID = re.compile(r'var\s+user_name\s*=\s*"(gh_[0-9a-f]+)"')
#: 公众号名。**顺序是踩过坑才定的**：
#:   `#js_name`          页面上公众号名那一栏，正确。
#:   `og:article:author` 是**文章作者署名**，不是公众号——一篇「岩岩」写的
#:                       文章发在「千问AI平台」上，这个字段给的是「岩岩」。
#:                       没有单独署名时它才恰好等于公众号名，所以单个样本
#:                       测不出区别（第一次就是这么漏掉的）。
#:   `og:site_name`      恒为「微信公众平台」，是站点名，绝对不能用。
_NICKNAME_PATTERNS = (
    re.compile(r'id="js_name"[^>]*>\s*([^<]+)'),
    re.compile(r'var\s+nickname\s*=\s*[\'"]([^\'"]+)[\'"]'),
)

#: 文章作者署名。与公众号名分开报，好让人一眼看出解析对没对上。
_AUTHOR = re.compile(r'og:article:author"\s+content="([^"]*)"')


def from_url(url: str) -> dict[str, str | None]:
    """解析一条链接。带参数的长链不出网，短链才请求页面。"""
    query = parse_qs(urlsplit(url).query)
    if query.get("__biz"):
        return {
            "biz": query["__biz"][0], "gh_id": None,
            "nickname": None, "author": None, "fetched": False,
        }

    response = httpx.get(
        url, timeout=30, follow_redirects=True, headers={"User-Agent": DEFAULT_UA}
    )
    html = response.text

    biz = None
    for pattern in _BIZ_PATTERNS:
        match = pattern.search(html)
        if match:
            biz = match.group(1).replace("%3D", "=")
            break

    gh = _GH_ID.search(html)
    author = _AUTHOR.search(html)
    nickname = None
    for pattern in _NICKNAME_PATTERNS:
        match = pattern.search(html)
        if match and match.group(1).strip():
            nickname = match.group(1).strip()
            break

    return {
        "biz": biz,
        "gh_id": gh.group(1) if gh else None,
        "nickname": nickname,
        "author": author.group(1).strip() if author else None,
        "fetched": True,
    }


def main(urls: list[str]) -> int:
    if not urls:
        print(__doc__)
        return 2

    failed = 0
    for url in urls:
        try:
            info = from_url(url)
        except httpx.HTTPError as exc:
            print(f"✗ {url[:56]}  网络错误：{type(exc).__name__}")
            failed += 1
            continue

        if not info["biz"]:
            # 文章被删或被折叠时页面里没有 biz，那不是链接格式的问题。
            print(f"✗ {url[:56]}  页面里没有 __biz（文章可能已删除或需要验证）")
            failed += 1
            continue

        label = info["nickname"] or info["gh_id"] or ""
        # 作者与公众号不同名时一并显示——那正是最容易配错的情形。
        byline = ""
        if info.get("author") and info["author"] != label:
            byline = f"   （本篇作者：{info['author']}）"
        print(f"✓ fakeid: {info['biz']:<26}{label}{byline}")
        print(f"    - {{ name: {label or '？'}, fakeid: {info['biz']} }}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
