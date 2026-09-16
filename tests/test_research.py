"""Web research: planning, fetching, comparison and citation — all mocked."""

from __future__ import annotations

import pytest
from jarvis.capabilities.base import Request
from jarvis.capabilities.research import _dedupe, _relevant_excerpt
from jarvis.tools.web.extract import Page, extract_readable
from jarvis.tools.web.search import SearchResult, _parse_duckduckgo, search_url

HTML = """
<html><head><title>MacBook Air M3 review</title>
<meta name="description" content="A thorough review"></head>
<body>
<nav>Home About</nav>
<script>var tracking = 1;</script>
<article>
<h1>MacBook Air M3 review</h1>
<p>The MacBook Air with the M3 chip starts at 1099 pounds and offers 18 hours of battery life.</p>
<p>Compared with the M2 model, single-core performance is roughly 17 percent higher.</p>
</article>
<footer>Copyright</footer>
</body></html>
"""

DDG_HTML = """
<div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa">
First result</a><div class="result__snippet">Snippet one</div></div>
<div class="result"><a class="result__a" href="https://example.org/b">Second result</a>
<div class="result__snippet">Snippet two</div></div>
"""


def test_readable_extraction_drops_chrome():
    title, text, links = extract_readable(HTML, "https://example.com/review")
    assert title == "MacBook Air M3 review"
    assert "1099 pounds" in text
    assert "tracking" not in text
    assert "Copyright" not in text
    assert isinstance(links, list)


def test_duckduckgo_parsing_unwraps_redirects():
    results = _parse_duckduckgo(DDG_HTML, limit=5)
    assert [r.url for r in results] == ["https://example.com/a", "https://example.org/b"]
    assert results[0].title == "First result"


def test_search_url_builder():
    assert search_url("tide times").startswith("https://duckduckgo.com/?q=tide")


def test_dedupe_limits_results_per_domain():
    results = [
        SearchResult("a", "https://x.com/1"), SearchResult("b", "https://x.com/2"),
        SearchResult("c", "https://x.com/3"), SearchResult("d", "https://y.com/1"),
        SearchResult("e", "https://x.com/1"),
    ]
    deduped = _dedupe(results)
    assert len(deduped) == 3
    assert sum(1 for r in deduped if "x.com" in r.url) == 2


def test_excerpt_selection_prefers_relevant_paragraphs():
    text = "\n".join([
        "An unrelated paragraph about gardening that goes on for quite a while indeed truly.",
        "The battery life of the MacBook Air is eighteen hours under light use conditions.",
        "Another unrelated paragraph concerning the weather in a distant coastal town today.",
    ])
    excerpt = _relevant_excerpt(text, "macbook air battery life", budget=200)
    assert "battery life" in excerpt


@pytest.fixture
def research(app, fake_provider, monkeypatch):
    """Research capability with a scripted search engine and page fetcher."""
    import jarvis.capabilities.research as module

    async def fake_search(self, query, limit=None):
        return [
            SearchResult("MacBook Air M3 review", "https://example.com/review", "A review"),
            SearchResult("MacBook deals", "https://shop.example.org/deals", "Deals today"),
        ]

    async def fake_fetch(url, **kwargs):
        if "review" in url:
            return Page(url=url, title="MacBook Air M3 review",
                        text="The MacBook Air M3 costs 1099 pounds with 18 hours of battery.",
                        status=200)
        return Page(url=url, title="Deals", text="Currently 949 pounds at several retailers.",
                    status=200)

    monkeypatch.setattr(module.WebSearch, "search", fake_search)
    monkeypatch.setattr(module, "fetch_page", fake_fetch)
    return app.capabilities["research"]


async def test_research_produces_a_cited_report(app, research, fake_provider):
    fake_provider.json_responses.append('{"queries": ["macbook air m3 price"]}')
    fake_provider.responses.append(
        "The M3 MacBook Air is 1099 pounds at list, and 949 from discounters [1][2]."
    )
    task = app.tasks.create("research", "test")
    request = Request(text="research macbook air prices", args={"query": "macbook air prices"},
                      ctx=app.deps.tool_context(task=task), task=task)
    response = await research.handle(request)

    assert "1099" in response.text
    assert response.display["kind"] == "research"
    assert len(response.display["sources"]) == 2
    assert response.spoken and len(response.spoken) < len(response.text) + 200
    # The report is saved for later reference.
    assert response.display["file"]


async def test_research_reports_progress_steps(app, research, fake_provider):
    task = app.tasks.create("research", "test")
    request = Request(text="research something", args={"query": "something"},
                      ctx=app.deps.tool_context(task=task), task=task)
    await research.handle(request)
    messages = [step["message"] for step in task.steps]
    assert any("Searching" in m for m in messages)
    assert any("Reading" in m or "Opening" in m for m in messages)
    assert any("summary" in m.lower() for m in messages)


async def test_research_is_cancellable(app, research):
    task = app.tasks.create("research", "test")
    task.cancel_event.set()
    request = Request(text="research something", args={"query": "something"},
                      ctx=app.deps.tool_context(task=task), task=task)
    from jarvis.core.errors import Cancelled

    with pytest.raises(Cancelled):
        await research.handle(request)


async def test_research_without_search_results(app, fake_provider, monkeypatch):
    import jarvis.capabilities.research as module

    async def empty_search(self, query, limit=None):
        return []

    monkeypatch.setattr(module.WebSearch, "search", empty_search)
    capability = app.capabilities["research"]
    task = app.tasks.create("research", "test")
    request = Request(text="research nothing", args={"query": "nothing"},
                      ctx=app.deps.tool_context(task=task), task=task)
    response = await capability.handle(request)
    assert response.error
    assert "search" in response.text.lower()


async def test_research_survives_a_dead_model(app, research, fake_provider):
    """With no model available the sources are still gathered and reported."""
    fake_provider.fail = True
    task = app.tasks.create("research", "test")
    request = Request(text="research macbook", args={"query": "macbook"},
                      ctx=app.deps.tool_context(task=task), task=task)
    response = await research.handle(request)
    assert "1099" in response.text or "sources" in response.text.lower()


async def test_fetch_page_handles_network_failure(app, ctx, monkeypatch):
    import httpx

    async def broken_get(self, url, **kwargs):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(httpx.AsyncClient, "get", broken_get)
    result = await app.deps.registry.call("fetch_page", {"url": "https://example.com"}, ctx)
    assert result.ok is False
    assert "couldn't read" in result.summary
