"""x-client-transaction-id 生成器。

**为什么非做不可**：实测（2026-07-26）X 只对 `SearchTimeline` 强制这个头，而且
它带时间戳、**一次性**——在真实浏览器里截获一个原样重放，依然 404。所以「现场搜 X」
这个差异点绕不开它。时间线类 operation 不需要，那条路已经能跑。

**它是什么**：X 把签名密钥藏在首页的「加载动画」里。页面上那个 X 形状的 loading
动画是一组 SVG 贝塞尔路径，动态 chunk 里另存着一组索引。把两者按 verification key
的字节做索引、在某个时间点上求值，就得到 anim_key。再和方法、路径、时间戳一起做
SHA256，拼上混淆字节，base64 —— 就是那个头。

绕这么大圈的用意是：**光有 cookie 不够，你还得真的解析过它的前端**。

算法参考 twscrape 的 `xclid.py`（MIT）。这里是自己的实现，但每一步都必须与 X 前端
逐位对齐——差一个字节签名就废，所以下面的常量和取整方式都不能"优化"。

**已端到端验证**（2026-07-26）：用本模块生成的签名打真实 `SearchTimeline`，
返回 200 / 133KB / 20 条推文——而同一端点不带签名是 404。

**它会随 X 前端改版而失效**，表现为签名被拒（404）。届时重新构造一个签名器即可；
若页面结构本身变了，要改的是本文件的选择器与正则。

一个容易踩的坑：**verification key 每次请求都不同**，所以取 key、算 anim_key、
发请求必须在一次会话里连贯做完，不能缓存 key 跨请求复用。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import math
import random
import re
import time
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from ...contracts import UpstreamDown
from ...settings import DEFAULT_UA

log = logging.getLogger("sourcepilot.channels.x")

HOME_URL = "https://x.com/tesla"  # 随便一个稳定存在的公开主页即可

#: 匿名访问拿到的入口 bundle。**它里面没有签名脚本**——X 给登录态和匿名态发的是
#: 两套不同的 web build。实测：匿名 35KB、只有 1 个 chunk；带 cookie 271KB、3 个 chunk。
#: 所以解析密钥必须带账号 cookie，这不是可选项。
LOGGED_OUT_ENTRY_RE = re.compile(r"entry-client-logged-out[-.][^/?#]*\.js")

#: 新版 x-web 构建：chunk 直接以完整 URL 写在页面里。
#: **只认 x-web**——老版 responsive-web 的 vendor/en/main 也是完整 URL，但那三个
#: 里没有签名脚本；把它们算进来会让代码误以为「已经是新版」而跳过下面的重建分支。
#: 这个坑我踩过：正则写宽一格，整条路就断了。
ASSET_URL_RE = re.compile(r"https://[\w.-]+/x-web/[\w./-]+\.js")
#: 老版 webpack 构建：页面里嵌两张映射表，chunk 地址得自己拼。
CHUNK_BASE = "https://abs.twimg.com/responsive-web/client-web"
HASH_MAP_RE = re.compile(r'(\d+):"([0-9a-f]{7})"')
NAME_MAP_RE = re.compile(r'(\d+):"([^"]+)"')
HEX7_RE = re.compile(r"[0-9a-f]{7}")
#: 存放动画索引的文件：老版本直接叫 ondemand.s.*.js，新版本是 chunk 里动态 import 的
#: sign.o-*.js。`\b` 是为了别把 design.o-*.js 这种误当成它。
INDICES_FILE_RE = re.compile(r"(?:\.{0,2}/)?[\w./-]*?\b(?:ondemand\.s|sign\.o)[\w.-]*\.js")
#: 索引以 `(x[12], 16)` 这种形式散在 JS 里。
INDICES_RE = re.compile(r"\(\w\[(\d{1,2})\],\s*16\)")

#: X 自己的纪元起点（2023-05-01），时间戳要相对它算。
EPOCH = 1_682_924_400
#: 混淆用的固定词与固定尾字节。这是 X 前端里的魔法值，不是我们能选的。
KEYWORD = "obfiowerehiring"
TAIL_BYTE = 3


class SignatureUnavailable(UpstreamDown):
    """页面结构变了，解析不出签名原料。"""


class CubicCurve:
    """三次贝塞尔求值。照搬浏览器动画的缓动实现，包括那个二分收敛。"""

    def __init__(self, curves: list[float]) -> None:
        self.curves = curves

    @staticmethod
    def _calc(a: float, b: float, m: float) -> float:
        return 3.0 * a * (1 - m) ** 2 * m + 3.0 * b * (1 - m) * m * m + m**3

    def value_at(self, t: float) -> float:
        c = self.curves
        if t <= 0.0:
            gradient = 0.0
            if c[0] > 0.0:
                gradient = c[1] / c[0]
            elif c[1] == 0.0 and c[2] > 0.0:
                gradient = c[3] / c[2]
            return gradient * t
        if t >= 1.0:
            gradient = 0.0
            if c[2] < 1.0:
                gradient = (c[3] - 1.0) / (c[2] - 1.0)
            elif c[2] == 1.0 and c[0] < 1.0:
                gradient = (c[1] - 1.0) / (c[0] - 1.0)
            return 1.0 + gradient * (t - 1.0)

        start, end, mid = 0.0, 1.0, 0.0
        while start < end:
            mid = (start + end) / 2
            x_est = self._calc(c[0], c[2], mid)
            if abs(t - x_est) < 1e-5:
                return self._calc(c[1], c[3], mid)
            if x_est < t:
                start = mid
            else:
                end = mid
        return self._calc(c[1], c[3], mid)


def _interpolate(a: list[float], b: list[float], f: float) -> list[float]:
    return [x * (1 - f) + y * f for x, y in zip(a, b, strict=True)]


def _rotation_matrix(degrees: float) -> list[float]:
    rad = math.radians(degrees)
    return [math.cos(rad), -math.sin(rad), math.sin(rad), math.cos(rad)]


def _solve(value: float, lo: float, hi: float, rounding: bool) -> float:
    result = value * (hi - lo) / 255 + lo
    return math.floor(result) if rounding else round(result, 2)


def _float_to_hex(x: float) -> str:
    """模拟 JS 的 `Number.prototype.toString(16)`。

    Python 的 `float.hex()` 格式完全不同（`0x1.8p+1`），不能用。这段照着 JS 的
    行为写：整数部分逐位取余，小数部分乘 16 逐位取整。
    """
    result: list[str] = []
    quotient = int(x)
    fraction = x - quotient

    while quotient > 0:
        quotient = int(x / 16)
        remainder = int(x - (float(quotient) * 16))
        result.insert(0, chr(remainder + 55) if remainder > 9 else str(remainder))
        x = float(quotient)

    if fraction == 0:
        return "".join(result)

    result.append(".")
    while fraction > 0:
        fraction *= 16
        integer = int(fraction)
        fraction -= float(integer)
        result.append(chr(integer + 55) if integer > 9 else str(integer))
    return "".join(result)


def calc_anim_key(frame: list[float], target_time: float) -> str:
    """把一帧动画参数在某时间点上求值，得到 anim_key。

    前 3 个数是起始颜色、接着 3 个是终止颜色、第 7 个是旋转角，剩下的是缓动曲线。
    取值后把颜色和旋转矩阵拼成十六进制串——这就是 X 前端里那段动画的"指纹"。
    """
    from_color = [*frame[:3], 1.0]
    to_color = [*frame[3:6], 1.0]
    to_rotation = [_solve(frame[6], 60.0, 360.0, True)]

    rest = frame[7:]
    curves = [_solve(x, -1.0 if i % 2 else 0.0, 1.0, False) for i, x in enumerate(rest)]
    progress = CubicCurve(curves).value_at(target_time)

    color = [max(0, min(255, v)) for v in _interpolate(from_color, to_color, progress)]
    rotation = _interpolate([0.0], to_rotation, progress)

    parts = [format(round(v), "x") for v in color[:-1]]
    for value in _rotation_matrix(rotation[0]):
        rounded = abs(round(value, 2))
        hex_value = _float_to_hex(rounded)
        if hex_value.startswith("."):
            parts.append(f"0{hex_value}".lower())
        else:
            parts.append(hex_value or "0")
    parts.extend(["0", "0"])
    return re.sub(r"[.-]", "", "".join(parts))


# ---------- 从页面里抽原料 ----------


def parse_verification_key(soup: BeautifulSoup) -> list[int]:
    el = soup.find("meta", attrs={"name": "twitter-site-verification", "content": True})
    if el is None:
        raise SignatureUnavailable("页面里没有 twitter-site-verification——X 改版了")
    try:
        return list(base64.b64decode(str(el.get("content")), validate=True))
    except ValueError as exc:
        raise SignatureUnavailable("verification key 不是合法 base64") from exc


def parse_anim_frames(soup: BeautifulSoup, vk_bytes: list[int]) -> list[list[float]]:
    """从加载动画的 SVG 路径里取动画帧。选哪一条由 verification key 决定。"""
    paths = soup.select("svg[id^='loading-x-anim'] g:first-child path:nth-child(2)")
    values = [str(p.get("d") or "").strip() for p in paths]
    if not values:
        raise SignatureUnavailable("页面里没有 loading-x-anim 动画——X 改版了")

    chosen = values[vk_bytes[5] % len(values)]
    # 路径串前 9 个字符是 "M...C" 那段起手，后面按 C 分段就是各组控制点。
    segments = chosen[9:].split("C")
    try:
        return [[float(n) for n in re.sub(r"[^\d]+", " ", s).split()] for s in segments]
    except ValueError as exc:
        raise SignatureUnavailable("动画路径解析失败") from exc


def _find_indices_script(
    html: str, client: httpx.Client, headers: dict[str, str] | None = None
) -> str:
    """找到存放动画索引的那个 js。

    老版本页面里直接就有链接；新的 x-web 版本把它藏在某个 chunk 的动态 import 里，
    只能逐个 chunk 翻。翻到就停——不必扫完。
    """
    scripts = list(dict.fromkeys(ASSET_URL_RE.findall(html)))
    if not scripts:
        # 老版 webpack 构建：没有现成的 chunk URL，从页面里的两张映射表重建。
        #   哈希表  {chunk_id: "7位十六进制"}
        #   名称表  {chunk_id: "可读名"}     —— 值不是 7 位十六进制的那些
        #   地址     {base}/{名称或id}.{哈希}a.js      注意末尾那个 a
        hash_map = dict(HASH_MAP_RE.findall(html))
        if not hash_map:
            raise SignatureUnavailable("页面里既没有 chunk 链接也没有 webpack 映射表")
        name_map = {
            cid: name for cid, name in NAME_MAP_RE.findall(html) if not HEX7_RE.fullmatch(name)
        }
        scripts = [
            f"{CHUNK_BASE}/{name_map.get(cid, cid)}.{digest}a.js"
            for cid, digest in hash_map.items()
        ]

    direct = [s for s in scripts if INDICES_FILE_RE.search(s)]
    if direct:
        return direct[0]

    for url in scripts:
        try:
            body = client.get(url, headers=headers or {"User-Agent": DEFAULT_UA}).text
        except httpx.HTTPError:
            continue
        match = INDICES_FILE_RE.search(body)
        if match:
            return urljoin(url, match.group(0))
    raise SignatureUnavailable(f"在 {len(scripts)} 个 chunk 里都没找到签名脚本")


def parse_anim_indices(
    html: str, client: httpx.Client, headers: dict[str, str] | None = None
) -> list[int]:
    url = _find_indices_script(html, client, headers)
    body = client.get(url, headers=headers or {"User-Agent": DEFAULT_UA}).text
    indices = [int(m.group(1)) for m in INDICES_RE.finditer(body)]
    if not indices:
        raise SignatureUnavailable("签名脚本里没有索引——格式变了")
    return indices


class XTransactionSigner:
    """持有解析出来的密钥，按需生成签名。

    密钥来自页面，X 不重新发版就一直有效，所以解析一次缓存起来；被拒时调
    `refresh()` 重来。
    """

    def __init__(self, vk_bytes: list[int], anim_key: str) -> None:
        self.vk_bytes = vk_bytes
        self.anim_key = anim_key

    @classmethod
    def load(
        cls,
        cookie: str,
        user_agent: str | None = None,
        client: httpx.Client | None = None,
    ) -> XTransactionSigner:
        """解析签名密钥。**必须带账号 cookie**——匿名态的 bundle 里没有签名脚本。"""
        if not cookie:
            raise SignatureUnavailable("解析 X 签名密钥需要账号 cookie（匿名态拿不到签名脚本）")

        owns = client is None
        client = client or httpx.Client(follow_redirects=True, timeout=20.0)
        headers = {"User-Agent": user_agent or DEFAULT_UA, "Cookie": cookie}
        try:
            html = client.get(HOME_URL, headers=headers).text
            if LOGGED_OUT_ENTRY_RE.search(html):
                # 拿到匿名 build 说明 cookie 没生效（过期或被拒），继续解析只会失败在更深处。
                raise SignatureUnavailable(
                    "X 返回的是匿名版页面——cookie 未生效，签名密钥无法解析"
                )
            soup = BeautifulSoup(html, "html.parser")

            vk_bytes = parse_verification_key(soup)
            frames = parse_anim_frames(soup, vk_bytes)
            indices = parse_anim_indices(html, client, headers)

            # 帧时长由 key 的若干字节连乘决定，再按 JS 的 Math.round 取到十位。
            frame_time = 1
            for i in indices[1:]:
                frame_time *= vk_bytes[i] % 16
            frame_time = math.floor(frame_time / 10 + 0.5) * 10

            frame = frames[vk_bytes[indices[0]] % 16]
            anim_key = calc_anim_key(frame, frame_time / 4096)
            log.info(
                "X 签名密钥已就绪（vk %d 字节，anim_key %d 字符）",
                len(vk_bytes),
                len(anim_key),
            )
            return cls(vk_bytes, anim_key)
        finally:
            if owns:
                client.close()

    def sign(self, method: str, path: str) -> str:
        """生成一个签名。带时间戳与随机字节，所以**每次都不同、且只能用一次**。"""
        ts = math.floor((time.time() * 1000 - EPOCH * 1000) / 1000)
        ts_bytes = [(ts >> (i * 8)) & 0xFF for i in range(4)]

        payload = f"{method.upper()}!{path}!{ts}{KEYWORD}{self.anim_key}"
        digest = list(hashlib.sha256(payload.encode()).digest())
        body = [*self.vk_bytes, *ts_bytes, *digest[:16], TAIL_BYTE]

        # 随机字节 XOR 混淆：同样的输入每次产出不同的串，防重放也防特征匹配。
        noise = random.randint(0, 255)
        obfuscated = bytearray([noise, *[b ^ noise for b in body]])
        return base64.b64encode(obfuscated).decode().rstrip("=")
