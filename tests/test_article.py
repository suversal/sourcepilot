"""read_article 测试。重点是 SSRF 防护——那是本平台唯一按调用方给的地址出网的口子。"""

from __future__ import annotations

import socket

import pytest

from sourcepilot import article as article_mod
from sourcepilot.article import ArticleService, assert_public_url
from sourcepilot.contracts import BadRequest, ErrorCode, ReadArticleParams

#: 打桩用的 DNS 表。域名以外的一切（IP 字面量）按原样返回，未登记的域名解析失败。
STUB_DNS = {
    "example.com": ["93.184.216.34"],
    "localhost": ["127.0.0.1"],
    "fake-ip.example": ["198.18.0.111"],          # 代理 fake-ip 模式下的占位地址
    "fake-ip6.example": ["fdfe:dcba:9876::56"],
    "internal.example": ["10.1.2.3"],             # 域名指向内网：DNS rebinding 那一路
}


@pytest.fixture(autouse=True)
def stub_dns(monkeypatch):
    """把 DNS 钉死在表里，让这组用例不依赖跑测试的机器怎么解析域名。

    不只是为了快：本机开着 Clash 这类 fake-ip 代理时，真实解析会把 example.com
    变成 198.18.x.x 占位地址，于是「公网地址应当通过」这条用例会在**代码完全正确**
    的情况下失败。测试该钉的是判定逻辑，不是运行环境的 DNS 配置。
    """

    def fake_getaddrinfo(host, port=None, *args, **kwargs):
        import ipaddress

        try:
            addrs = [str(ipaddress.ip_address(host))]
        except ValueError:
            addrs = STUB_DNS.get(host)
            if addrs is None:
                raise OSError(f"stub DNS 里没有 {host!r}") from None
        out = []
        for addr in addrs:
            v6 = ":" in addr
            family = socket.AF_INET6 if v6 else socket.AF_INET
            sockaddr = (addr, port or 0, 0, 0) if v6 else (addr, port or 0)
            out.append((family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr))
        return out

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


class TestSsrfGuard:
    """不设防的话，外部就能拿这个接口当跳板探测内网。"""

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1/",
            "http://localhost/",
            "http://[::1]/",
            "http://10.0.0.1/",
            "http://192.168.1.1/",
            "http://172.16.0.1/",
            "http://169.254.169.254/latest/meta-data/",  # 云厂商元数据接口
            "http://0.0.0.0/",
        ],
    )
    def test_internal_addresses_rejected(self, url):
        with pytest.raises(BadRequest):
            assert_public_url(url)

    @pytest.mark.parametrize("url", ["ftp://example.com/x", "file:///etc/passwd", "gopher://x/"])
    def test_non_http_schemes_rejected(self, url):
        with pytest.raises(BadRequest, match="只支持 http/https"):
            assert_public_url(url)

    @pytest.mark.parametrize("url", ["http://example.com:6379/", "http://example.com:22/"])
    def test_odd_ports_rejected(self, url):
        """挡住「用 HTTP 请求去戳 redis / ssh」这种玩法。"""
        with pytest.raises(BadRequest, match="端口"):
            assert_public_url(url)

    def test_error_message_does_not_leak_topology(self):
        """报错不能说「这是内网地址」——那本身就是一条内网探测的反馈信号。"""
        with pytest.raises(BadRequest) as exc:
            assert_public_url("http://192.168.1.1/")
        message = exc.value.message
        assert "内网" not in message and "private" not in message.lower()

    def test_public_url_passes(self):
        assert_public_url("https://example.com/a")  # 不抛即通过

    def test_redirect_target_is_revalidated(self, monkeypatch):
        """公网地址 302 到内网就绕过了首次校验，所以跟随重定向后要再验一次。"""
        import httpx

        def fake_get(self, url, **kw):
            return httpx.Response(
                200,
                text="<html><body><p>x</p></body></html>",
                request=httpx.Request("GET", "http://127.0.0.1/secret"),
            )

        monkeypatch.setattr(httpx.Client, "get", fake_get)
        with pytest.raises(BadRequest):
            ArticleService()._fetch("https://example.com/a")


class TestFakeIpRange:
    """代理 fake-ip 模式：域名解析成占位地址，那不是内网地址。

    本项目的部署方式（Mac mini + Clash Verge）就在这种环境里。不认这个例外的话，
    `read_article` 会把**每一个**公网 URL 都判成内网而拒绝，整个工具静默失效。
    """

    @pytest.mark.parametrize("host", ["fake-ip.example", "fake-ip6.example"])
    def test_domain_mapped_to_fake_ip_passes(self, host):
        assert_public_url(f"https://{host}/a")  # 不抛即通过

    @pytest.mark.parametrize("url", ["http://198.18.0.111/", "http://[fdfe:dcba:9876::56]/"])
    def test_literal_fake_ip_still_rejected(self, url):
        """例外只给域名。字面量不经 DNS，放行它等于凭空开一个洞。"""
        with pytest.raises(BadRequest):
            assert_public_url(url)

    def test_domain_pointing_at_lan_still_rejected(self):
        """fake-ip 只作用于要代出去的域名；内网域名走真实解析，照旧要拦（DNS rebinding）。"""
        with pytest.raises(BadRequest):
            assert_public_url("https://internal.example/a")

    def test_strict_mode_when_disabled(self, monkeypatch):
        """SOURCEPILOT_FAKE_IP_CIDRS 设空 = 回到严格模式，不用代理的部署可以这么关。"""
        monkeypatch.setattr(article_mod, "FAKE_IP_NETS", ())
        with pytest.raises(BadRequest):
            assert_public_url("https://fake-ip.example/a")

    def test_bad_cidr_is_ignored_not_fatal(self):
        """配置写错不该让服务起不来——忽略那一条，其余照常。"""
        assert article_mod._parse_cidrs("198.18.0.0/15, 不是CIDR ,") == (
            __import__("ipaddress").ip_network("198.18.0.0/15"),
        )


class TestExtraction:
    def test_missing_page_is_not_found(self, monkeypatch):
        import httpx

        monkeypatch.setattr(
            httpx.Client,
            "get",
            lambda self, url, **kw: httpx.Response(404, request=httpx.Request("GET", url)),
        )
        service = ArticleService()
        with pytest.raises(Exception) as exc:
            service.get(ReadArticleParams(url="https://example.com/gone"))
        assert exc.value.code is ErrorCode.NOT_FOUND

    def test_unextractable_page_reports_clearly(self, monkeypatch):
        """纯前端渲染的页面抽不出正文，要说清楚而不是返回空正文。"""
        import httpx

        monkeypatch.setattr(
            httpx.Client,
            "get",
            lambda self, url, **kw: httpx.Response(
                200, text="<html><body></body></html>", request=httpx.Request("GET", url)
            ),
        )
        with pytest.raises(Exception, match="抽不出正文"):
            ArticleService().get(ReadArticleParams(url="https://example.com/spa"))

    def test_truncation_is_flagged(self, monkeypatch):
        import httpx

        long_text = "<html><body><article>" + ("正文内容。" * 800) + "</article></body></html>"
        monkeypatch.setattr(
            httpx.Client,
            "get",
            lambda self, url, **kw: httpx.Response(
                200, text=long_text, request=httpx.Request("GET", url)
            ),
        )
        env = ArticleService().get(
            ReadArticleParams(url="https://example.com/long", max_chars=1000)
        )
        assert env.data.truncated is True
        assert env.data.char_count == 1000
