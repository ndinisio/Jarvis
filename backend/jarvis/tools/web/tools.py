"""Web tools: search and page retrieval.

These are the primitives. The *research capability* composes them into a
multi-step investigation with progress reporting and source comparison.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ...security.permissions import RiskLevel
from ..base import Tool, ToolContext, ToolResult, ToolSpec
from .extract import fetch_page
from .search import WebSearch


class SearchWebTool(Tool):
    spec = ToolSpec(
        name="search_web",
        description="Search the web and return ranked results with snippets",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 6},
            },
            "required": ["query"],
        },
        risk=RiskLevel.LOW,
        category="research",
        requires_network=True,
        expected_ms=1500,
    )

    def __init__(self, deps):
        self._deps = deps
        self._search = WebSearch(lambda: deps.config)

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        query = args["query"]
        ctx.report(f"Searching for “{query}”…", tool="search_web")
        results = await self._search.search(query, int(args.get("limit", 6)))
        if not results:
            return ToolResult(
                ok=False, data={"results": []},
                summary="The search returned nothing useful.",
            )
        return ToolResult(
            data={"results": [r.as_dict() for r in results], "query": query},
            summary=f"{len(results)} results for “{query}”.",
            display={
                "kind": "sources",
                "title": f"Search: {query}",
                "sources": [r.as_dict() for r in results],
            },
        )


class FetchPageTool(Tool):
    spec = ToolSpec(
        name="fetch_page",
        description="Fetch a web page and extract its readable text",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "max_chars": {"type": "integer", "default": 8000},
            },
            "required": ["url"],
        },
        risk=RiskLevel.LOW,
        category="research",
        requires_network=True,
        expected_ms=2500,
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        conf = self._deps.config.research
        url = args["url"]
        ctx.report(f"Reading {_short(url)}…", tool="fetch_page")
        page = await fetch_page(
            url,
            timeout=conf.per_request_timeout_s,
            user_agent=conf.user_agent,
            max_chars=int(args.get("max_chars", 8000)),
        )
        if not page.ok:
            return ToolResult.failure(
                f"I couldn't read {_short(url)}.", detail=page.error or "no readable content"
            )
        return ToolResult(
            data=page.as_dict(max_text=int(args.get("max_chars", 8000))),
            summary=f"Read {page.title or _short(url)} — {len(page.text)} characters.",
            display={"kind": "page", "title": page.title or _short(url), "url": page.url,
                     "text": page.text[:3000]},
        )


class FetchManyTool(Tool):
    spec = ToolSpec(
        name="fetch_pages",
        description="Fetch several pages concurrently and extract their text",
        parameters={
            "type": "object",
            "properties": {
                "urls": {"type": "array"},
                "max_chars": {"type": "integer", "default": 6000},
            },
            "required": ["urls"],
        },
        risk=RiskLevel.LOW,
        category="research",
        requires_network=True,
        expected_ms=4000,
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        conf = self._deps.config.research
        urls = [u for u in (args.get("urls") or []) if isinstance(u, str)][: conf.max_pages]
        if not urls:
            return ToolResult.failure("No URLs were given.")
        ctx.report(f"Opening {len(urls)} pages…", tool="fetch_pages")

        async def one(url: str):
            ctx.raise_if_cancelled()
            return await fetch_page(
                url, timeout=conf.per_request_timeout_s, user_agent=conf.user_agent,
                max_chars=int(args.get("max_chars", 6000)),
            )

        pages = await asyncio.gather(*(one(u) for u in urls), return_exceptions=True)
        good = [p for p in pages if not isinstance(p, BaseException) and p.ok]
        return ToolResult(
            ok=bool(good),
            data={"pages": [p.as_dict() for p in good]},
            summary=f"Read {len(good)} of {len(urls)} pages.",
            display={"kind": "sources", "title": "Pages read",
                     "sources": [{"title": p.title, "url": p.url, "snippet": p.text[:160]}
                                 for p in good]},
        )


class ResearchTopicTool(Tool):
    """The whole investigation as one tool call.

    ``search_web`` and ``fetch_pages`` are primitives; composing them into a
    grounded answer takes several steps of judgement that a small local model
    plans badly. Exposing the research capability as a tool lets the agent ask
    for the *outcome* — "find out X" — and get sources back, while still being
    able to drop down to the primitives when it only needs one page.
    """

    spec = ToolSpec(
        name="research_topic",
        description=(
            "Investigate a topic on the web: plan queries, read several sources "
            "and return a synthesised answer with citations"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "the question to investigate, as a full sentence"},
            },
            "required": ["query"],
        },
        risk=RiskLevel.LOW,
        category="research",
        requires_network=True,
        expected_ms=12000,
        returns="a written answer with numbered sources and their URLs",
        mutates=False,
        retryable=True,
        examples=["research the best espresso machines under 500",
                  "find out what changed in the new EU AI rules",
                  "look into why my laptop battery drains overnight"],
    )

    def __init__(self, deps):
        self._deps = deps
        self._capability = None

    def _cap(self):
        # Built on first use: importing the capability pulls in the model
        # registry, and the registry is constructed during start-up.
        if self._capability is None:
            from ...capabilities.research import ResearchCapability

            self._capability = ResearchCapability(self._deps)
        return self._capability

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        from ...capabilities.base import Request

        query = str(args["query"]).strip()
        response = await self._cap().handle(Request(text=query, args={"query": query}, ctx=ctx))
        sources = (response.data or {}).get("sources", []) if isinstance(response.data, dict) else []
        if response.error:
            return ToolResult.failure(response.text or "The research didn't get anywhere.",
                                      detail=response.error)
        return ToolResult(
            # The report itself is the finding; the agent reads it to decide
            # whether the question is actually answered.
            data={"query": query, "report": response.text, "sources": sources},
            summary=response.speech or f"Researched “{query}”.",
            display=response.display,
        )


def _short(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url).netloc.replace("www.", "") or url[:40]


def web_tools(deps) -> list[Tool]:
    return [SearchWebTool(deps), FetchPageTool(deps), FetchManyTool(deps),
            ResearchTopicTool(deps)]
