"""crawler アダプタのテスト / crawler adapter tests."""
from __future__ import annotations

import pytest

from aipmo.adapters.base import AdapterError
from aipmo.adapters.crawler import CrawlerAdapter
from aipmo.cli import build_engine

PAGE_HTML = """
<html>
<head>
  <title>Example News</title>
  <meta name="description" content="the daily news">
  <meta property="og:image" content="https://example.com/img.png">
  <meta property="og:type" content="article">
</head>
<body>
  <article>
    <h1>Headline One</h1>
    <p>Body text.</p>
  </article>
  <a href="/story/1">Story 1</a>
  <a href="https://other.example.com/story/2">Story 2</a>
</body>
</html>
"""


class FakeTransport:
    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.requests: list[tuple[str, str, dict]] = []

    def request(self, method, url, headers, body=None, timeout=60.0):
        self.requests.append((method, url, headers))
        matches = [(url.rfind(f), f) for f in self.routes if f in url]
        if matches:
            response = self.routes[max(matches)[1]]
            return response() if callable(response) else response
        return 404, {}, b"not found"


def html_response(html: str = PAGE_HTML, status: int = 200):
    return status, {}, html.encode("utf-8")


# ===== fetch_page ============================================================

def test_fetch_page_returns_status_and_content():
    transport = FakeTransport({"example.com": html_response()})
    adapter = CrawlerAdapter(transport=transport)

    result = adapter.invoke("fetch_page", {"url": "https://example.com/news"})

    assert result["status"] == 200
    assert "Headline One" in result["content"]


def test_fetch_page_returns_http_errors_as_status_not_exception():
    """404 はテンプレート側で when により許容したい場合があるため例外にしない。"""
    transport = FakeTransport({"example.com": (404, {}, b"gone")})
    adapter = CrawlerAdapter(transport=transport)

    result = adapter.invoke("fetch_page", {"url": "https://example.com/missing"})

    assert result["status"] == 404


def test_fetch_page_sends_a_user_agent():
    transport = FakeTransport({"example.com": html_response()})
    adapter = CrawlerAdapter(transport=transport, user_agent="test-agent/1.0")

    adapter.invoke("fetch_page", {"url": "https://example.com"})

    _, _, headers = transport.requests[0]
    assert headers["User-Agent"] == "test-agent/1.0"


# ===== extract_text ==========================================================

def test_extract_text_from_tag():
    adapter = CrawlerAdapter(transport=FakeTransport({}))

    result = adapter.invoke("extract_text", {"html": PAGE_HTML, "tag": "h1"})

    assert result["count"] == 1
    assert result["texts"] == ["Headline One"]


def test_extract_text_defaults_to_body():
    adapter = CrawlerAdapter(transport=FakeTransport({}))

    result = adapter.invoke("extract_text", {"html": PAGE_HTML})

    assert result["count"] == 1
    assert "Headline One" in result["texts"][0]


# ===== extract_links =========================================================

def test_extract_links_resolves_relative_urls_against_base_url():
    adapter = CrawlerAdapter(transport=FakeTransport({}))

    result = adapter.invoke("extract_links", {
        "html": PAGE_HTML, "base_url": "https://example.com/news",
    })

    assert result["count"] == 2
    urls = [link["url"] for link in result["links"]]
    assert "https://example.com/story/1" in urls
    assert "https://other.example.com/story/2" in urls


def test_extract_links_caps_at_max_links():
    html = "<html><body>" + "".join(
        f'<a href="/p{i}">p{i}</a>' for i in range(10)
    ) + "</body></html>"
    adapter = CrawlerAdapter(transport=FakeTransport({}), max_links=3)

    result = adapter.invoke("extract_links", {"html": html, "base_url": ""})

    assert result["count"] == 3


# ===== extract_metadata ======================================================

def test_extract_metadata_reads_title_and_meta_tags():
    adapter = CrawlerAdapter(transport=FakeTransport({}))

    result = adapter.invoke("extract_metadata", {"html": PAGE_HTML})

    assert result["title"] == "Example News"
    assert result["description"] == "the daily news"
    assert result["og_image"] == "https://example.com/img.png"
    assert result["og_type"] == "article"


def test_extract_metadata_handles_missing_title_and_meta():
    adapter = CrawlerAdapter(transport=FakeTransport({}))

    result = adapter.invoke("extract_metadata", {"html": "<html><body></body></html>"})

    assert result == {
        "title": None, "description": None, "og_image": None, "og_type": None,
    }


# ===== connectivity failure ===================================================

def test_connection_failure_raises_adapter_error(monkeypatch):
    import urllib.error

    def raise_url_error(*args, **kwargs):
        raise urllib.error.URLError("name or service not known")

    monkeypatch.setattr("urllib.request.urlopen", raise_url_error)
    adapter = CrawlerAdapter()

    with pytest.raises(AdapterError, match="接続できません"):
        adapter.invoke("fetch_page", {"url": "https://unreachable.example"})


# ===== no writes ==============================================================

def test_no_action_writes():
    """crawler は認証情報を持たない読み取り専用アダプタ。"""
    adapter = CrawlerAdapter(transport=FakeTransport({}))
    for name in adapter.actions():
        assert adapter.writes(name) is False


# ===== config.yaml wiring (build_engine) =====================================

def test_build_engine_registers_crawler_when_configured():
    """risk_forecast と同じく opt-in — config に無ければ登録されない。"""
    config = {"adapters": {"mode": "real", "crawler": {"max_links": 5}}}

    engine = build_engine(config)

    assert engine.adapters.has("crawler")
    crawler = engine.adapters.get("crawler")
    assert isinstance(crawler, CrawlerAdapter)
    assert crawler.max_links == 5


def test_build_engine_does_not_register_crawler_when_not_configured():
    config = {"adapters": {"mode": "real"}}

    engine = build_engine(config)

    assert not engine.adapters.has("crawler")
