"""Web search.

DuckDuckGo's HTML endpoint is the default because it needs no API key, which
keeps the "works out of the box, no paid services" promise. Brave and a
self-hosted SearXNG are supported for users who want them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, quote_plus, urlparse

import httpx

from ...core.errors import NetworkUnavailable
from ...core.logging import get_logger

log = get_logger("jarvis.web.search")


@dataclass(slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    source: str = ""

    def as_dict(self) -> dict:
        return {"title": self.title, "url": self.url, "snippet": self.snippet,
                "source": self.source or _domain(self.url)}


class WebSearch:
    def __init__(self, config_provider):
        self._config_provider = config_provider

    @property
    def _conf(self):
        return self._config_provider().research

    async def search(self, query: str, limit: int | None = None) -> list[SearchResult]:
        conf = self._conf
        limit = limit or conf.max_results
        provider = conf.search_provider
        try:
            if provider == "brave" and conf.brave_api_key:
                return await self._brave(query, limit)
            if provider == "searxng" and conf.searxng_url:
                return await self._searxng(query, limit)
            return await self._duckduckgo(query, limit)
        except NetworkUnavailable:
            raise
        except httpx.HTTPError as exc:
            raise NetworkUnavailable(
                "I couldn't reach the search service.", detail=f"{type(exc).__name__}: {exc}"
            ) from exc

    # -- providers ---------------------------------------------------------
    async def _duckduckgo(self, query: str, limit: int) -> list[SearchResult]:
        conf = self._conf
        headers = {"User-Agent": conf.user_agent, "Accept-Language": "en-GB,en;q=0.9"}
        async with httpx.AsyncClient(timeout=conf.per_request_timeout_s, headers=headers,
                                     follow_redirects=True) as client:
            for endpoint in ("https://html.duckduckgo.com/html/", "https://lite.duckduckgo.com/lite/"):
                try:
                    resp = await client.post(endpoint, data={"q": query, "kl": "uk-en"})
                    resp.raise_for_status()
                except httpx.HTTPError as exc:
                    log.debug("duckduckgo %s failed: %s", endpoint, exc)
                    continue
                results = _parse_duckduckgo(resp.text, limit)
                if results:
                    return results
        return []

    async def _brave(self, query: str, limit: int) -> list[SearchResult]:
        conf = self._conf
        async with httpx.AsyncClient(timeout=conf.per_request_timeout_s) as client:
            resp = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": limit},
                headers={"X-Subscription-Token": conf.brave_api_key,
                         "Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
        return [
            SearchResult(item.get("title", ""), item.get("url", ""),
                         _strip_html(item.get("description", "")))
            for item in (data.get("web", {}).get("results") or [])[:limit]
        ]

    async def _searxng(self, query: str, limit: int) -> list[SearchResult]:
        conf = self._conf
        async with httpx.AsyncClient(timeout=conf.per_request_timeout_s) as client:
            resp = await client.get(
                conf.searxng_url.rstrip("/") + "/search",
                params={"q": query, "format": "json"},
            )
            resp.raise_for_status()
            data = resp.json()
        return [
            SearchResult(item.get("title", ""), item.get("url", ""), item.get("content", ""))
            for item in (data.get("results") or [])[:limit]
        ]


def _parse_duckduckgo(html: str, limit: int) -> list[SearchResult]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    results: list[SearchResult] = []
    seen: set[str] = set()

    anchors = soup.select("a.result__a") or soup.select("a.result-link") or soup.select("h2 a")
    for anchor in anchors:
        url = _clean_ddg_url(anchor.get("href", ""))
        if not url or url in seen:
            continue
        snippet = ""
        container = anchor.find_parent(class_=re.compile("result")) or anchor.parent
        if container:
            node = container.find(class_=re.compile("snippet")) or container.find("td", class_=None)
            if node:
                snippet = node.get_text(" ", strip=True)
        seen.add(url)
        results.append(SearchResult(anchor.get_text(" ", strip=True), url, snippet[:400]))
        if len(results) >= limit:
            break
    return results


def _clean_ddg_url(href: str) -> str:
    if not href:
        return ""
    if href.startswith("//duckduckgo.com/l/") or "/l/?uddg=" in href:
        query = parse_qs(urlparse("https:" + href if href.startswith("//") else href).query)
        target = query.get("uddg", [""])[0]
        return target
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("http"):
        return href
    return ""


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def _domain(url: str) -> str:
    return urlparse(url).netloc.replace("www.", "")


def search_url(query: str, engine: str = "duckduckgo") -> str:
    """Build a human-facing search URL (used when opening a browser)."""
    engines = {
        "duckduckgo": "https://duckduckgo.com/?q={}",
        "google": "https://www.google.com/search?q={}",
        "bing": "https://www.bing.com/search?q={}",
    }
    return engines.get(engine, engines["duckduckgo"]).format(quote_plus(query))
