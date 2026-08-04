"""公众号凭据自检。

    python -m sourcepilot.channels.wechat.check

**为什么不能用搜索接口来验证**：公众平台按接口分别限流，`searchbiz`（搜公众号）
与 `appmsg`（拉文章列表）是两套额度。实测出现过 searchbiz 返回 `ret: 0`、
appmsg 同时返回 `200013 freq control` 的情况——用前者验证会得出「凭据没问题」，
而采集实际走的是后者，照样失败。

所以这里**打的就是采集真正用的那个接口**，两个都测，分别报告。

只打一次。凭据出问题时反复探测正是把额度耗光的原因。
"""

from __future__ import annotations

import sys

import httpx
import yaml

from ...settings import DEFAULT_UA
from .mp import APPMSG_LIST, CREDENTIALS_FILE, SEARCH_BIZ

#: 探测用的公众号。量子位是长期活跃的大号，不会因为「这个号没文章」而误判。
PROBE_FAKEID = "MzIzNjc1NzUzMw=="

#: 公众平台的业务码 → 人话。
RET_MEANING = {
    0: ("✓", "正常"),
    200003: ("✗", "登录态失效——token 与 cookie 必须来自同一次登录，两个都要换"),
    200013: ("⚠", "频率限制（freq control）。凭据本身可能没问题，等额度恢复即可"),
}


def _call(url: str, params: dict, creds: dict) -> dict:
    response = httpx.get(
        url,
        params={**params, "token": creds["token"], "lang": "zh_CN", "f": "json", "ajax": 1},
        headers={
            "Cookie": creds["cookie"],
            "Referer": "https://mp.weixin.qq.com/",
            "User-Agent": DEFAULT_UA,
        },
        timeout=20,
    )
    return response.json().get("base_resp") or {}


def check() -> int:
    if not CREDENTIALS_FILE.exists():
        print(f"没有凭据文件：{CREDENTIALS_FILE}")
        print("按 config/sources/wechat.yaml 文件头的说明配置。")
        return 1

    creds = yaml.safe_load(CREDENTIALS_FILE.read_text(encoding="utf-8")) or {}
    if not creds.get("token") or not creds.get("cookie"):
        print("凭据文件缺 token 或 cookie。")
        return 1

    probes = (
        ("searchbiz  搜公众号", SEARCH_BIZ,
         {"action": "search_biz", "begin": 0, "count": 1, "query": "量子位"}),
        ("appmsg     拉文章列表", APPMSG_LIST,
         {"action": "list_ex", "begin": 0, "count": 1, "fakeid": PROBE_FAKEID, "type": "9"}),
    )

    worst = 0
    for label, url, params in probes:
        try:
            resp = _call(url, params, creds)
        except httpx.HTTPError as exc:
            print(f"  ✗ {label}  网络错误：{type(exc).__name__}")
            worst = max(worst, 2)
            continue
        ret = resp.get("ret")
        mark, meaning = RET_MEANING.get(ret, ("✗", resp.get("err_msg") or "未知错误"))
        print(f"  {mark} {label}  ret={ret}  {meaning}")
        if ret != 0:
            worst = max(worst, 1 if ret == 200013 else 2)

    print()
    if worst == 0:
        print("凭据可用，采集正常。")
    elif worst == 1:
        print("凭据有效但被限流。等额度恢复即可，**不要反复重试**——那正是耗光额度的原因。")
    else:
        print("需要更新凭据。token 与 cookie 要从同一次登录里一起复制。")
    return worst


if __name__ == "__main__":
    sys.exit(check())
