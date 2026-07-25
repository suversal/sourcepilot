"""公众平台扫码登录助手。

`python -m sourcepilot.channels.login`

它做三件事：取二维码 → 等你用手机扫 → 把拿到的登录态写进本地文件。
**扫码这一步只能由你本人完成**，程序不接触你的账号密码，也不代你授权。

拿到的凭据只写进 config/wechat_credentials.yaml（已在 .gitignore 里），
不打印到终端、不进日志。
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import httpx
import yaml

from .wechat import CREDENTIALS_FILE

BASE = "https://mp.weixin.qq.com"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def run(qr_path: Path | None = None, poll_seconds: int = 120) -> int:
    qr_path = qr_path or (CREDENTIALS_FILE.parent / "wechat_login_qr.png")
    client = httpx.Client(headers={"User-Agent": UA, "Referer": BASE}, timeout=20)

    try:
        client.post(
            f"{BASE}/cgi-bin/bizlogin",
            params={"action": "startlogin"},
            data={"userlang": "zh_CN", "redirect_url": "", "login_type": "3", "sessionid": ""},
        )

        qr = client.get(f"{BASE}/cgi-bin/scanloginqrcode", params={"action": "getqrcode"})
        if qr.status_code != 200 or not qr.content:
            print("取二维码失败——公众平台可能改版了，或者当前网络到不了 mp.weixin.qq.com")
            return 2
        qr_path.parent.mkdir(parents=True, exist_ok=True)
        qr_path.write_bytes(qr.content)

        print(f"二维码已存到：{qr_path}")
        print("请用**专用微信小号**扫码并确认登录（别用主号——这条线风险最高）。")
        print(f"最多等待 {poll_seconds} 秒…\n")

        deadline = time.time() + poll_seconds
        while time.time() < deadline:
            ask = client.get(f"{BASE}/cgi-bin/scanloginqrcode", params={"action": "ask"})
            status = ask.json().get("status") if ask.headers.get(
                "content-type", ""
            ).startswith("application/json") else None
            if status == 1:
                print("已扫码，等待手机端确认…")
            elif status == 0:
                pass
            elif status == 4:
                print("你在手机上取消了登录。")
                return 1
            elif status == 2:
                break
            time.sleep(2)
        else:
            print("等待超时，没有完成扫码。")
            return 1

        done = client.post(
            f"{BASE}/cgi-bin/bizlogin",
            params={"action": "login"},
            data={"userlang": "zh_CN", "redirect_url": "", "login_type": "3", "sessionid": ""},
        )
        match = re.search(r"token=(\d+)", done.text)
        if not match:
            print("登录流程走完了但没拿到 token——公众平台可能改了返回格式。")
            return 2

        token = match.group(1)
        cookie = "; ".join(f"{k}={v}" for k, v in client.cookies.items())
        CREDENTIALS_FILE.write_text(
            yaml.safe_dump({"token": token, "cookie": cookie}, allow_unicode=True),
            encoding="utf-8",
        )
        qr_path.unlink(missing_ok=True)

        print(f"凭据已写入 {CREDENTIALS_FILE}（该文件在 .gitignore 里，不会进仓库）")
        print("接下来把 config/sources/wechat.yaml 的 enabled 改成 true，并填上要订阅的公众号名。")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(run())
