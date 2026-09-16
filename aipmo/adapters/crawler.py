"""Web クローラアダプタ / Web crawler adapter.

外部サイトの1ページを取得し、その HTML からテキスト・リンク・
メタデータを抜き出す。認証情報を持たない、読み取り専用のアダプタ
（`writes=True` のアクションは無い）。

HTML の解析には beautifulsoup4（`html.parser` バックエンド、追加の
コンパイル済み依存なし）を使う。`pip install "aipmo[crawler]"` で入る。

Fetches a single page from an external site and pulls text, links, and
metadata out of its HTML. A read-only adapter with no credentials and no
`writes=True` action.

HTML parsing uses beautifulsoup4 (the `html.parser` backend — no compiled
dependency beyond it). Installed via `pip install "aipmo[crawler]"`.
"""
from __future__ import annotations

import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urljoin

from .base import Adapter, AdapterError, action


class CrawlerAdapter(Adapter):
    name = "crawler"

    def __init__(self, timeout: float = 30.0, max_links: int = 100,
                 user_agent: str = "AI-PMO-Crawler/1.0",
                 transport: Any = None, **config: Any) -> None:
        super().__init__(**config)
        self.timeout = timeout
        self.max_links = max_links
        self.user_agent = user_agent
        self._transport = transport

    def _get(self, url: str,
             headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
        merged = {"User-Agent": self.user_agent, **(headers or {})}
        if self._transport is not None:
            return self._transport.request("GET", url, merged, None, self.timeout)

        request = urllib.request.Request(url, headers=merged, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers or {}), exc.read()
        except urllib.error.URLError as exc:
            raise AdapterError(f"crawler: 接続できません / cannot reach {url}: {exc}")

    @staticmethod
    def _soup(html: str) -> Any:
        try:
            from bs4 import BeautifulSoup  # 遅延 import / lazy import
        except ImportError:
            raise AdapterError(
                'crawler: HTML 解析には追加の導入が必要です / HTML parsing needs '
                'an extra package:\n  pip install "aipmo[crawler]"'
            )
        return BeautifulSoup(html, "html.parser")

    # -- アクション / actions -------------------------------------------------

    @action()
    def fetch_page(self, url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        """URL を取得する / fetch a URL.

        404 のような HTTP エラーは例外にせず status に入れて返す——
        テンプレート側で when によりリンク切れを許容したい場合があるため。
        接続自体ができない場合（DNS 失敗など）だけ AdapterError にする。

        HTTP errors (404, etc.) are returned in `status` rather than raised, so
        a template can tolerate a dead link via `when`. Only a connection
        failure (DNS, refused, etc.) raises AdapterError.
        """
        status, _, raw = self._get(url, headers)
        return {
            "url": url,
            "status": status,
            "content": raw.decode("utf-8", "replace"),
        }

    @action()
    def extract_text(self, html: str, tag: str = "body") -> dict[str, Any]:
        """指定タグの中のテキストをすべて抜き出す / extract the text inside a tag."""
        soup = self._soup(html)
        elements = soup.find_all(tag)
        texts = [element.get_text(strip=True) for element in elements]
        return {"tag": tag, "count": len(texts), "texts": texts}

    @action()
    def extract_links(self, html: str, base_url: str = "") -> dict[str, Any]:
        """`<a href>` を集める。base_url を渡すと相対 URL を絶対 URL に直す。

        Collects `<a href>` links; passing base_url resolves relative ones.
        """
        soup = self._soup(html)
        links = []
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            links.append({
                "url": urljoin(base_url, href) if base_url else href,
                "text": anchor.get_text(strip=True),
            })
        links = links[: self.max_links]
        return {"count": len(links), "links": links}

    @action()
    def extract_metadata(self, html: str) -> dict[str, Any]:
        """`<title>` と代表的な `<meta>` タグを抜き出す。

        Extracts `<title>` and the common `<meta>` tags.
        """
        soup = self._soup(html)
        title = soup.title.get_text(strip=True) if soup.title else None

        description = None
        og_image = None
        og_type = None
        for meta in soup.find_all("meta"):
            name = (meta.get("name") or "").lower()
            prop = (meta.get("property") or "").lower()
            content = meta.get("content")
            if name == "description":
                description = content
            elif prop == "og:image":
                og_image = content
            elif prop == "og:type":
                og_type = content

        return {
            "title": title,
            "description": description,
            "og_image": og_image,
            "og_type": og_type,
        }
