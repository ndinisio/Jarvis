"""Web research.

A genuine multi-step investigation rather than "ask the chat model and hope":

    plan queries → search → choose sources → read pages in parallel →
    extract the relevant passages → synthesise with citations

The conversational model is never used as the browser. It plans the queries and
writes the final synthesis; the fetching, extraction and comparison are done by
code, which is what keeps the result grounded in sources the user can check.

The whole thing runs as a background task: the user gets an acknowledgement in
well under a second and watches the steps in the activity panel.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from ..core.errors import Cancelled, NetworkUnavailable
from ..core.logging import get_logger
from ..models.base import ChatMessage
from ..models.registry import Slot
from ..tools.web.extract import Page, fetch_page
from ..tools.web.search import SearchResult, WebSearch
from .base import Capability, Request, Response

log = get_logger("jarvis.capabilities.research")

_STOP = {"the", "a", "an", "of", "and", "or", "for", "to", "in", "on", "is", "are", "what",
         "which", "best", "current", "please", "me", "my", "find", "research"}


@dataclass
class Source:
    index: int
    title: str
    url: str
    domain: str
    excerpt: str

    def as_dict(self) -> dict[str, Any]:
        return {"index": self.index, "title": self.title, "url": self.url,
                "domain": self.domain, "snippet": self.excerpt[:300]}


class ResearchCapability(Capability):
    name = "research"
    description = "Investigate a topic on the web and report back with sources."
    long_running = True

    def __init__(self, deps):
        super().__init__(deps)
        self._search = WebSearch(lambda: deps.config)

    async def handle(self, request: Request) -> Response:
        query = (request.args.get("query") or request.text).strip()
        conf = self.deps.config.research
        task = request.task

        def step(message: str, progress: float | None = None, **meta):
            # Inside a task the step *is* the progress report; ctx.report would
            # duplicate it, since the context's progress callback feeds the same
            # task.
            if task is not None:
                self.deps.tasks.step(task, message, progress, **meta)
            else:
                request.ctx.report(message, **meta)

        def check_cancel():
            if request.ctx.cancelled():
                raise Cancelled()

        # 1. Plan ---------------------------------------------------------
        step("Planning the search…", 0.05, phase="plan")
        queries = await self._plan_queries(query)
        check_cancel()

        # 2. Search -------------------------------------------------------
        step(f"Searching: {queries[0]}", 0.15, phase="search")
        results: list[SearchResult] = []
        try:
            batches = await asyncio.gather(
                *(self._search.search(q, conf.max_results) for q in queries),
                return_exceptions=True,
            )
        except Exception as exc:  # pragma: no cover - gather with return_exceptions
            raise NetworkUnavailable(detail=str(exc)) from exc
        for batch in batches:
            if isinstance(batch, BaseException):
                log.debug("search leg failed: %s", batch)
                continue
            results.extend(batch)
        results = _dedupe(results)
        if not results:
            return Response(
                text="I couldn't reach a search service, so I can't research that at the moment. "
                     "Check the network connection and I'll try again.",
                error="no search results",
            )
        check_cancel()

        # 3. Read ---------------------------------------------------------
        chosen = results[: conf.max_pages]
        step(f"Opening {len(chosen)} results…", 0.3, phase="read",
             sources=[r.url for r in chosen])

        pages: list[Page] = []
        semaphore = asyncio.Semaphore(4)
        # Shared, mutable, and safe under asyncio's cooperative scheduling:
        # every read() checks-then-decrements this in one synchronous block
        # with no `await` in between, so concurrent reads under the
        # semaphore above can't race past the budget.
        js_budget = [conf.max_js_fallbacks if self._js_fallback_available(conf) else 0]

        async def read(result: SearchResult) -> Page | None:
            async with semaphore:
                if request.ctx.cancelled():
                    return None
                step(f"Reading {_domain(result.url)}…", phase="read")
                page = await fetch_page(
                    result.url, timeout=conf.per_request_timeout_s,
                    user_agent=conf.user_agent, max_chars=conf.max_page_chars,
                )
                if self._page_is_thin(page, conf) and js_budget[0] > 0:
                    js_budget[0] -= 1
                    step(f"{_domain(result.url)} needs a browser to render — opening it…",
                        phase="read")
                    page = await self._render_with_browser(result.url, page, conf)
                return page

        fetched = await asyncio.gather(*(read(r) for r in chosen), return_exceptions=True)
        for item in fetched:
            if isinstance(item, Page) and item.ok:
                pages.append(item)
        check_cancel()

        if not pages:
            # Still useful: search snippets alone often answer the question.
            step("Pages wouldn't load; working from search results.", 0.6, phase="compare")

        # 4. Compare ------------------------------------------------------
        step("Comparing sources…", 0.65, phase="compare")
        sources = _build_sources(query, results, pages)

        # 5. Synthesise ---------------------------------------------------
        step("Preparing the summary…", 0.85, phase="synthesise")
        report = await self._synthesise(query, sources, request)
        check_cancel()

        spoken = await self._spoken_summary(report, request)
        saved = self._save_report(query, report, sources)

        return Response(
            text=report,
            spoken=spoken,
            display={
                "kind": "research",
                "title": query[:90],
                "markdown": report,
                "sources": [s.as_dict() for s in sources],
                "file": str(saved) if saved else "",
            },
            data={"sources": [s.as_dict() for s in sources]},
        )

    # -- JS-heavy pages: a real browser as a second attempt, not the default -
    def _js_fallback_available(self, conf) -> bool:
        return (conf.js_fallback_enabled and self.deps.config.capabilities.browser
                and self.deps.controller.is_macos)

    @staticmethod
    def _page_is_thin(page: Page, conf) -> bool:
        return page.ok and len(page.text.strip()) < conf.thin_page_chars

    async def _render_with_browser(self, url: str, original: Page, conf) -> Page:
        """The static fetch came back too thin to be useful — often a sign
        the page needs JavaScript to render its real content. There's no
        way to run that JS invisibly through the existing AppleScript
        bridge (see tools/browser/tools.py), so this opens a real, visible
        tab, same trade-off CurrentPageTool already accepts for the
        opposite fallback direction (browser JS unavailable -> static
        fetch). Never worse than the static result: anything short of a
        clear improvement, or any failure along the way, returns *original*
        unchanged."""
        from ..tools.browser.tools import detect_browser, driver_for

        try:
            name = await detect_browser(self.deps)
            driver = driver_for(self.deps.controller, name)
            if not await driver.open(url):
                return original
            text = await _wait_for_rendered_text(driver, conf.thin_page_chars,
                                                 max_wait_s=conf.js_render_max_wait_s,
                                                 poll_s=conf.js_render_poll_s)
        except Exception as exc:
            log.debug("browser JS fallback failed for %s: %s", url, exc)
            return original
        if len(text.strip()) <= len(original.text.strip()):
            return original
        return Page(url=url, title=original.title, text=text[:conf.max_page_chars],
                   status=original.status, content_type=original.content_type)

    # -- steps -------------------------------------------------------------
    async def _plan_queries(self, query: str) -> list[str]:
        """Two or three complementary search queries beat one long sentence."""
        fallback = [query]
        keywords = [w for w in re.findall(r"[\w'-]+", query.lower()) if w not in _STOP]
        if len(keywords) > 3:
            fallback.append(" ".join(keywords[:6]))
        try:
            data = await self.models.complete_json(
                Slot.FAST,
                [
                    ChatMessage("system", "You write web search queries. JSON only."),
                    ChatMessage(
                        "user",
                        f'Write 2-3 short web search queries that would answer: "{query}"\n'
                        'Reply with JSON only: {"queries": ["…", "…"]}',
                    ),
                ],
                max_tokens=120,
                timeout_s=10.0,
            )
        except Exception as exc:
            log.debug("query planning unavailable: %s", exc)
            return fallback[:2]
        queries = data.get("queries") if isinstance(data, dict) else None
        if isinstance(queries, list):
            cleaned = [str(q).strip() for q in queries if str(q).strip()][:3]
            if cleaned:
                return cleaned
        return fallback[:2]

    async def _synthesise(self, query: str, sources: list[Source], request: Request) -> str:
        if not sources:
            return "I couldn't gather any usable sources on that."
        corpus = "\n\n".join(
            f"[{s.index}] {s.title} — {s.domain}\n{s.excerpt}" for s in sources
        )[:18000]
        today = dt.datetime.now().strftime("%d %B %Y")
        prompt = (
            f"Today is {today}. Answer the user's request using only the sources below.\n\n"
            f"Request: {query}\n\nSources:\n{corpus}\n\n"
            "Write a concise report:\n"
            "- Lead with the direct answer in one or two sentences.\n"
            "- Then the key points, comparing sources where they differ.\n"
            "- Cite sources inline as [1], [2].\n"
            "- Mark anything you infer rather than read as 'Inference:'.\n"
            "- If the sources don't answer the question, say so plainly.\n"
            "Keep it under 300 words. No preamble."
        )
        parts: list[str] = []
        try:
            async for delta in self.models.stream(
                Slot.GENERAL,
                [ChatMessage("system", "You are a precise research analyst."),
                 ChatMessage("user", prompt)],
                max_tokens=700,
                temperature=0.2,
            ):
                if request.ctx.cancelled():
                    break
                parts.append(delta)
        except Exception as exc:
            log.warning("synthesis failed: %s", exc)
            return _fallback_report(query, sources)
        text = "".join(parts).strip()
        return text or _fallback_report(query, sources)

    async def _spoken_summary(self, report: str, request: Request) -> str:
        from ..core.personality import speakable

        if len(report) < 420:
            return speakable(report)
        try:
            completion = await self.models.complete(
                Slot.FAST,
                [
                    ChatMessage("system", "You compress findings into two spoken sentences. "
                                          "British, composed, no preamble."),
                    ChatMessage("user", f"Summarise aloud in two sentences:\n\n{report[:3000]}"),
                ],
                max_tokens=120,
                temperature=0.3,
            )
            if completion.text.strip():
                return speakable(completion.text)
        except Exception:
            pass
        return speakable(report, max_chars=340)

    def _save_report(self, query: str, report: str, sources: list[Source]):
        try:
            directory = self.deps.config.tasks_dir
            directory.mkdir(parents=True, exist_ok=True)
            slug = re.sub(r"[^\w]+", "-", query.lower())[:48].strip("-") or "research"
            path = directory / f"{dt.datetime.now():%Y%m%d-%H%M}-{slug}.md"
            body = [f"# {query}", "", report, "", "## Sources", ""]
            body += [f"{s.index}. [{s.title or s.domain}]({s.url})" for s in sources]
            path.write_text("\n".join(body), encoding="utf-8")
            return path
        except OSError as exc:
            log.debug("could not save research report: %s", exc)
            return None


async def _wait_for_rendered_text(driver, threshold: int, max_wait_s: float = 6.0,
                                  poll_s: float = 0.6) -> str:
    """A freshly opened tab hasn't necessarily finished running its own JS
    yet — reading immediately would risk the same "loading…" shell the
    static fetch already produced, defeating the point. Polls rather than
    a fixed sleep, the same "wait until ready, don't guess a delay"
    approach WaitForElementTool already uses elsewhere in this package."""
    deadline = time.monotonic() + max_wait_s
    text = ""
    while True:
        text = await driver.page_text()
        if len(text.strip()) >= threshold or time.monotonic() >= deadline:
            return text
        await asyncio.sleep(poll_s)


def _dedupe(results: list[SearchResult]) -> list[SearchResult]:
    seen_urls: set[str] = set()
    per_domain: dict[str, int] = {}
    out: list[SearchResult] = []
    for result in results:
        if not result.url or result.url in seen_urls:
            continue
        domain = _domain(result.url)
        if per_domain.get(domain, 0) >= 2:
            continue
        seen_urls.add(result.url)
        per_domain[domain] = per_domain.get(domain, 0) + 1
        out.append(result)
    return out


def _build_sources(query: str, results: list[SearchResult], pages: list[Page]) -> list[Source]:
    by_url = {p.url: p for p in pages}
    sources: list[Source] = []
    index = 1
    for result in results:
        page = by_url.get(result.url)
        if page is None:
            # Match on the resolved URL too (redirects).
            page = next((p for p in pages if _domain(p.url) == _domain(result.url)), None)
        excerpt = _relevant_excerpt(page.text, query) if page else result.snippet
        if not excerpt:
            continue
        sources.append(
            Source(index, (page.title if page else result.title) or result.title,
                   page.url if page else result.url, _domain(result.url), excerpt)
        )
        index += 1
        if index > 8:
            break
    return sources


def _relevant_excerpt(text: str, query: str, budget: int = 2200) -> str:
    """Keyword-scored paragraph selection — keeps the prompt small and on topic."""
    if not text:
        return ""
    terms = [w for w in re.findall(r"[\w'-]+", query.lower()) if w not in _STOP and len(w) > 2]
    paragraphs = [p.strip() for p in text.split("\n") if len(p.strip()) > 60]
    if not paragraphs:
        return text[:budget]
    scored = []
    for position, paragraph in enumerate(paragraphs):
        lowered = paragraph.lower()
        score = sum(lowered.count(term) for term in terms)
        score += max(0, 3 - position * 0.2) * 0.3  # lead paragraphs matter
        scored.append((score, position, paragraph))
    scored.sort(key=lambda item: (-item[0], item[1]))
    chosen: list[tuple[int, str]] = []
    used = 0
    for score, position, paragraph in scored:
        if used + len(paragraph) > budget:
            continue
        chosen.append((position, paragraph))
        used += len(paragraph)
        if used > budget * 0.9:
            break
    chosen.sort(key=lambda item: item[0])
    return "\n".join(p for _, p in chosen) or text[:budget]


def _fallback_report(query: str, sources: list[Source]) -> str:
    lines = [f"I gathered {len(sources)} sources on “{query}” but couldn't summarise them "
             "(the language model wasn't available). The key passages:", ""]
    for source in sources[:5]:
        lines.append(f"[{source.index}] {source.title or source.domain} — {source.url}")
        lines.append(source.excerpt[:400].replace("\n", " "))
        lines.append("")
    return "\n".join(lines)


def _domain(url: str) -> str:
    return urlparse(url).netloc.replace("www.", "")
