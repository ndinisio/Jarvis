"""Web research: planning, fetching, comparison and citation — all mocked."""

from __future__ import annotations

import pytest
from jarvis.capabilities.base import Request
from jarvis.capabilities.research import (
    ResearchCapability,
    _dedupe,
    _relevant_excerpt,
    _wait_for_rendered_text,
)
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


# -- the JS-heavy-page browser fallback (v2.4) ----------------------------------

class _FakeDriver:
    def __init__(self, texts: list[str]):
        self._texts = list(texts)
        self.opened: list[str] = []
        self.open_returns = True

    async def open(self, url: str) -> bool:
        self.opened.append(url)
        return self.open_returns

    async def page_text(self) -> str:
        if len(self._texts) > 1:
            return self._texts.pop(0)
        return self._texts[0] if self._texts else ""


def test_page_is_thin_only_for_a_short_ok_page(app):
    capability = ResearchCapability(app.deps)
    conf = app.config.research
    assert capability._page_is_thin(Page(url="x", text="short", status=200), conf) is True
    assert capability._page_is_thin(Page(url="x", text="x" * 500, status=200), conf) is False
    # An error page is never "thin" in the sense that a browser could help —
    # ok is False, so it's excluded outright, not routed into a fallback.
    assert capability._page_is_thin(Page(url="x", text="", status=0, error="timed out"),
                                    conf) is False


def test_js_fallback_available_requires_the_flag_the_capability_flag_and_macos(app, monkeypatch):
    capability = ResearchCapability(app.deps)
    conf = app.config.research

    monkeypatch.setattr(app.controller, "is_macos", True)
    assert capability._js_fallback_available(conf) is True

    monkeypatch.setattr(app.controller, "is_macos", False)
    assert capability._js_fallback_available(conf) is False


async def test_js_fallback_available_is_false_when_the_browser_capability_is_off(app, monkeypatch):
    monkeypatch.setattr(app.controller, "is_macos", True)
    app.config_store.update({"capabilities": {"browser": False}})
    capability = ResearchCapability(app.deps)  # the config just changed; build fresh
    assert capability._js_fallback_available(app.config.research) is False


async def test_wait_for_rendered_text_returns_as_soon_as_the_threshold_is_met():
    driver = _FakeDriver(["", "short", "this is now long enough to clear the threshold check"])
    text = await _wait_for_rendered_text(driver, threshold=20, max_wait_s=5, poll_s=0.01)
    assert "long enough" in text


async def test_wait_for_rendered_text_gives_up_after_the_timeout():
    driver = _FakeDriver([""])
    text = await _wait_for_rendered_text(driver, threshold=1000, max_wait_s=0.05, poll_s=0.01)
    assert text == ""


async def test_render_with_browser_returns_the_original_page_if_opening_fails(app):
    capability = ResearchCapability(app.deps)
    original = Page(url="https://x.example", text="thin", status=200)
    driver = _FakeDriver(["plenty of rendered content, well past any thin-page threshold"])
    driver.open_returns = False

    import jarvis.tools.browser.tools as browser_tools

    async def fake_detect_browser(deps):
        return "Safari"

    def fake_driver_for(controller, name):
        return driver

    orig_detect, orig_driver_for = browser_tools.detect_browser, browser_tools.driver_for
    browser_tools.detect_browser = fake_detect_browser
    browser_tools.driver_for = fake_driver_for
    try:
        result = await capability._render_with_browser("https://x.example", original,
                                                        app.config.research)
    finally:
        browser_tools.detect_browser, browser_tools.driver_for = orig_detect, orig_driver_for
    assert result is original


async def test_render_with_browser_keeps_the_original_when_the_render_is_not_better(app):
    app.config_store.update({"research": {"js_render_max_wait_s": 0.05, "js_render_poll_s": 0.01}})
    capability = ResearchCapability(app.deps)
    original = Page(url="https://x.example", text="a" * 300, status=200)  # not actually thin
    driver = _FakeDriver(["shorter"])  # worse than the original

    import jarvis.tools.browser.tools as browser_tools

    async def fake_detect_browser(deps):
        return "Safari"

    def fake_driver_for(controller, name):
        return driver

    orig_detect, orig_driver_for = browser_tools.detect_browser, browser_tools.driver_for
    browser_tools.detect_browser = fake_detect_browser
    browser_tools.driver_for = fake_driver_for
    try:
        result = await capability._render_with_browser("https://x.example", original,
                                                        app.config.research)
    finally:
        browser_tools.detect_browser, browser_tools.driver_for = orig_detect, orig_driver_for
    assert result is original


async def test_thin_pages_get_a_browser_fallback_and_substantial_pages_do_not(
    app, fake_provider, monkeypatch
):
    import jarvis.capabilities.research as module
    import jarvis.tools.browser.tools as browser_tools

    async def fake_search(self, query, limit=None):
        return [
            SearchResult("Thin SPA", "https://spa.example.com/", "A snippet"),
            SearchResult("Full article", "https://article.example.com/", "Another snippet"),
        ]

    async def fake_fetch(url, **kwargs):
        if "spa" in url:
            return Page(url=url, title="Thin SPA", text="Loading…", status=200)
        return Page(url=url, title="Full article", text="A" * 500, status=200)

    monkeypatch.setattr(module.WebSearch, "search", fake_search)
    monkeypatch.setattr(module, "fetch_page", fake_fetch)
    monkeypatch.setattr(app.controller, "is_macos", True)
    app.config_store.update({"research": {"js_render_max_wait_s": 0.05, "js_render_poll_s": 0.01}})

    driver = _FakeDriver(["The real rendered content, now well past the thin-page threshold."])

    async def fake_detect_browser(deps):
        return "Safari"

    def fake_driver_for(controller, name):
        return driver

    monkeypatch.setattr(browser_tools, "detect_browser", fake_detect_browser)
    monkeypatch.setattr(browser_tools, "driver_for", fake_driver_for)

    fake_provider.json_responses.append('{"queries": ["test"]}')
    fake_provider.responses.append("A synthesised report [1][2].")

    capability = app.capabilities["research"]
    task = app.tasks.create("research", "test")
    request = Request(text="research something", args={"query": "something"},
                      ctx=app.deps.tool_context(task=task), task=task)
    response = await capability.handle(request)

    assert driver.opened == ["https://spa.example.com/"], (
        "only the thin page should have gotten the browser fallback")
    messages = [step["message"] for step in task.steps]
    assert any("needs a browser" in m for m in messages)
    assert response.error is None


async def test_the_js_fallback_budget_bounds_how_many_tabs_open_per_turn(
    app, fake_provider, monkeypatch
):
    """Every result is thin here — without a budget every one of them would
    open a tab. max_js_fallbacks (default 2) must cap that."""
    import jarvis.capabilities.research as module
    import jarvis.tools.browser.tools as browser_tools

    async def fake_search(self, query, limit=None):
        return [SearchResult(f"Thin {i}", f"https://spa{i}.example.com/", "snippet")
               for i in range(4)]

    async def fake_fetch(url, **kwargs):
        return Page(url=url, title="Thin", text="Loading…", status=200)

    monkeypatch.setattr(module.WebSearch, "search", fake_search)
    monkeypatch.setattr(module, "fetch_page", fake_fetch)
    monkeypatch.setattr(app.controller, "is_macos", True)
    app.config_store.update({"research": {"js_render_max_wait_s": 0.05, "js_render_poll_s": 0.01}})

    opened: list[str] = []

    class _CountingDriver(_FakeDriver):
        async def open(self, url):
            opened.append(url)
            return await super().open(url)

    def fake_driver_for(controller, name):
        return _CountingDriver(["still thin, never clears the threshold"])

    async def fake_detect_browser(deps):
        return "Safari"

    monkeypatch.setattr(browser_tools, "detect_browser", fake_detect_browser)
    monkeypatch.setattr(browser_tools, "driver_for", fake_driver_for)

    fake_provider.json_responses.append('{"queries": ["test"]}')
    fake_provider.responses.append("A report.")

    capability = app.capabilities["research"]
    task = app.tasks.create("research", "test")
    request = Request(text="research something", args={"query": "something"},
                      ctx=app.deps.tool_context(task=task), task=task)
    await capability.handle(request)

    assert len(opened) == app.config.research.max_js_fallbacks == 2


async def test_js_fallback_never_triggers_on_a_non_macos_host_even_with_a_thin_page(
    app, fake_provider, monkeypatch
):
    """The default state of this dev environment: no macOS, so the existing,
    fully-static research behaviour must be completely unchanged."""
    import jarvis.capabilities.research as module
    import jarvis.tools.browser.tools as browser_tools

    async def fake_search(self, query, limit=None):
        return [SearchResult("Thin SPA", "https://spa.example.com/", "A snippet")]

    async def fake_fetch(url, **kwargs):
        return Page(url=url, title="Thin SPA", text="Loading…", status=200)

    monkeypatch.setattr(module.WebSearch, "search", fake_search)
    monkeypatch.setattr(module, "fetch_page", fake_fetch)
    assert app.controller.is_macos is False  # this environment, unmodified

    opened: list[str] = []

    def fake_driver_for(controller, name):
        raise AssertionError("the browser fallback must never be attempted off macOS")

    monkeypatch.setattr(browser_tools, "driver_for", fake_driver_for)

    fake_provider.json_responses.append('{"queries": ["test"]}')
    fake_provider.responses.append("A report.")

    capability = app.capabilities["research"]
    task = app.tasks.create("research", "test")
    request = Request(text="research something", args={"query": "something"},
                      ctx=app.deps.tool_context(task=task), task=task)
    await capability.handle(request)
    assert opened == []
