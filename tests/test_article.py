"""read_article 测试。重点是 SSRF 防护——那是本平台唯一按调用方给的地址出网的口子。"""

from __future__ import annotations

import pytest

from sourcepilot.article import ArticleService, assert_public_url
from sourcepilot.contracts import BadRequest, ErrorCode, ReadArticleParams


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
