"""Page fetching and readable-content extraction.

Deliberately dependency-light: httpx for transport, BeautifulSoup for parsing,
plus a small density heuristic to find the main content. Good enough to feed a
local model without dragging in a headless browser.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx

from ...core.logging import get_logger

log = get_logger("jarvis.web.extract")

_DROP_TAGS = ("script", "style", "noscript", "iframe", "svg", "form", "button", "nav",
              "footer", "header", "aside")
_CONTENT_HINTS = ("article", "main", '[role="main"]', "#content", ".content", ".post",
                  ".article-body", "#main")


@dataclass(slots=True)
class Page:
    url: str
    title: str = ""
    text: str = ""
    links: list[dict] = field(default_factory=list)
    status: int = 0
    error: str = ""
    content_type: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.text) and not self.error

    def as_dict(self, max_text: int = 4000) -> dict:
        return {
            "url": self.url,
            "title": self.title,
            "text": self.text[:max_text],
            "status": self.status,
            "error": self.error,
            "domain": urlparse(self.url).netloc.replace("www.", ""),
        }


async def fetch_page(url: str, *, timeout: float = 20.0, user_agent: str = "",
                     max_chars: int = 12000, client: httpx.AsyncClient | None = None) -> Page:
    headers = {
        "User-Agent": user_agent or "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605.1.15",
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
    }
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers)
    try:
        resp = await client.get(url, headers=headers)
        content_type = resp.headers.get("content-type", "")
        page = Page(url=str(resp.url), status=resp.status_code, content_type=content_type)
        if resp.status_code >= 400:
            page.error = f"HTTP {resp.status_code}"
            return page
        if "application/json" in content_type:
            page.title = urlparse(url).netloc
            page.text = resp.text[:max_chars]
            return page
        if "text/" not in content_type and "xml" not in content_type:
            page.error = f"unsupported content type: {content_type or 'unknown'}"
            return page
        title, text, links = extract_readable(resp.text, str(resp.url))
        page.title = title
        page.text = text[:max_chars]
        page.links = links[:40]
        return page
    except httpx.TimeoutException:
        return Page(url=url, error="timed out")
    except httpx.HTTPError as exc:
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
            log.debug("connection problem for %s: %s", url, exc)
        return Page(url=url, error=f"{type(exc).__name__}")
    finally:
        if owns_client:
            await client.aclose()


def extract_readable(html: str, base_url: str = "") -> tuple[str, str, list[dict]]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        title = og_title["content"].strip()

    links: list[dict] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if href.startswith(("#", "javascript:", "mailto:")):
            continue
        text = anchor.get_text(" ", strip=True)
        if not text or len(text) > 120:
            continue
        links.append({"text": text, "url": urljoin(base_url, href)})

    for tag in soup(_DROP_TAGS):
        tag.decompose()

    best_text = ""
    for selector in _CONTENT_HINTS:
        for node in soup.select(selector):
            candidate = _clean(node.get_text("\n", strip=True))
            if len(candidate) > len(best_text):
                best_text = candidate
        if len(best_text) > 800:
            break
    if len(best_text) < 400:
        body = soup.body or soup
        best_text = _clean(body.get_text("\n", strip=True))

    description = soup.find("meta", attrs={"name": "description"})
    if description and description.get("content") and len(best_text) < 200:
        best_text = description["content"].strip() + "\n" + best_text

    return title, best_text, links


def _clean(text: str) -> str:
    lines = []
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if not line or len(line) < 2:
            continue
        lines.append(line)
    deduped: list[str] = []
    seen: set[str] = set()
    for line in lines:
        key = line.lower()
        if key in seen and len(line) < 80:
            continue
        seen.add(key)
        deduped.append(line)
    return "\n".join(deduped)
