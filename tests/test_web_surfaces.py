"""v3.0 Phase 4: the two browsers, genuine input, and the user's part.

Which browser a web action reaches (the hub's policy), that password and
card fields are never typed into by either browser, that a sign-in or
CAPTCHA is handed to the user and waited for, and — against a real
Chromium — shadow DOM, iframes, settling on network requests, scrolling,
keys and going back.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from conftest import collect
from jarvis.router.quick import QuickCommands
from jarvis.security import consequence
from jarvis.surfaces.web import hub as hub_module
from jarvis.surfaces.web.hub import BrowserHub, hub_of
from jarvis.tools.browser import sensitive
from jarvis.tools.browser.observe import PageMemory, render_manifest
from jarvis.tools.browser.page_tools import PressPageKeyTool, TakeOverTool
from jarvis.tools.browser.tools import normalise_key


class _Driver:
    def __init__(self, app_name: str, owned: bool = False):
        self.app_name = app_name
        self.owned = owned
        self.opened: list[str] = []
        self.fronted = 0

    async def open(self, url):
        self.opened.append(url)
        return True

    def at(self, url):
        return bool(self.opened) and self.opened[-1] == url

    async def current_page(self):
        return {"url": self.opened[-1] if self.opened else "https://x.example/", "title": "X"}

    async def run_js(self, script, *, timeout=20.0):
        return "complete"

    async def bring_to_front(self):
        self.fronted += 1


@pytest.fixture
def hub(app, monkeypatch):
    """A hub on macOS whose two browsers are recognisable fakes."""
    monkeypatch.setattr(hub_module, "IS_MACOS", True)
    hub = BrowserHub(app.deps)
    everyday, jarvis = _Driver("Safari"), _Driver("JARVIS Chrome", owned=True)
    hub.everyday_calls = []

    async def fake_everyday(name=""):
        hub.everyday_calls.append(name)
        return everyday

    async def fake_jarvis():
        return jarvis if app.config.browser.jarvis_browser else None

    monkeypatch.setattr(hub, "everyday", fake_everyday)
    monkeypatch.setattr(hub, "jarvis", fake_jarvis)
    hub.fakes = (everyday, jarvis)
    app.config_store.update({"browser": {"jarvis_browser": True}})
    return hub


class _Ctx:
    def __init__(self, task_id=None):
        self.task_id = task_id


# -- the policy -------------------------------------------------------------------

async def test_an_errand_that_navigates_runs_in_jarvis_chrome_and_stays_there(hub):
    everyday, jarvis = hub.fakes
    task = _Ctx("t1")
    assert await hub.for_action(task, navigating=True, url="https://www.amazon.co.uk/") is jarvis
    # Every later step of the same task — reads included — goes to the same window.
    assert await hub.for_action(task) is jarvis
    assert await hub.for_action(task, navigating=True, url="https://other.example/") is jarvis


async def test_a_task_about_the_page_you_are_on_uses_your_browser(hub):
    everyday, jarvis = hub.fakes
    task = _Ctx("t2")
    assert await hub.for_action(task) is everyday, "first step reads the current page"
    assert await hub.for_action(task, navigating=True, url="https://x.example/") is everyday


async def test_a_quick_command_outside_any_task_uses_your_browser(hub):
    everyday, _ = hub.fakes
    assert await hub.for_action(_Ctx(None), navigating=True, url="https://youtube.com/") is everyday


async def test_site_overrides_and_the_off_switch_send_errands_to_your_browser(hub, app):
    everyday, jarvis = hub.fakes
    app.config_store.update({"browser": {"site_overrides": {"mail.google.com": "everyday"}}})
    assert await hub.for_action(_Ctx("a"), navigating=True, url="https://mail.google.com/x") is everyday
    assert await hub.for_action(_Ctx("b"), navigating=True, url="https://www.amazon.co.uk/") is jarvis
    app.config_store.update({"browser": {"jarvis_browser": False}})
    assert await hub.for_action(_Ctx("c"), navigating=True, url="https://www.amazon.co.uk/") is everyday


async def test_a_named_browser_wins(hub):
    everyday, jarvis = hub.fakes
    assert await hub.for_action(_Ctx("n1"), navigating=True, browser="Safari") is everyday
    assert hub.everyday_calls[-1] == "Safari"
    assert await hub.for_action(_Ctx("n2"), browser="the JARVIS browser") is jarvis


async def test_off_macos_jarvis_chrome_is_the_only_browser(app, monkeypatch):
    monkeypatch.setattr(hub_module, "IS_MACOS", False)
    hub = BrowserHub(app.deps)
    jarvis = _Driver("JARVIS Chrome", owned=True)

    async def fake_jarvis():
        return jarvis

    monkeypatch.setattr(hub, "jarvis", fake_jarvis)
    assert await hub.everyday() is None
    assert await hub.for_action(_Ctx(None)) is jarvis


async def test_jarvis_chrome_reports_why_it_is_unavailable(app, monkeypatch):
    from jarvis.surfaces.web import cdp

    app.config_store.update({"browser": {"jarvis_browser": True}})
    monkeypatch.setattr(cdp.PlaywrightBrowser, "installed", staticmethod(lambda: False))
    hub = BrowserHub(app.deps)
    assert await hub.jarvis() is None
    assert "Playwright" in hub.jarvis_unavailable


async def test_without_chrome_installed_it_falls_back_to_bundled_chromium(app, monkeypatch):
    started = []

    class FakeBrowser:
        def __init__(self, *, user_data_dir, channel, headless):
            self.channel = channel

        async def start(self):
            started.append(self.channel)
            if self.channel == "chrome":
                raise RuntimeError("Chromium distribution 'chrome' is not found")
            return self

        async def close(self):
            pass

    hub = BrowserHub(app.deps)
    browser = await hub._launch(FakeBrowser, app.config.browser.model_copy(update={"channel": "chrome"}))
    assert started == ["chrome", None]
    assert browser is not None and browser.channel is None


def test_pin_sends_everything_to_one_driver_and_restores(app):
    hub = hub_of(app.deps)
    pinned = _Driver("Eval")
    with hub.pin(pinned):
        assert asyncio.run(hub.for_action(_Ctx("x"), navigating=True)) is pinned
    assert hub._pinned is None


async def test_browse_to_in_an_errand_opens_jarvis_chrome_not_the_system_browser(app, hub, monkeypatch):
    _, jarvis = hub.fakes
    app.deps.browsers = hub
    system_opens = []

    async def open_url(url, browser=None):
        system_opens.append(url)
        raise AssertionError("the everyday browser should not be used")

    monkeypatch.setattr(app.controller, "open_url", open_url)
    ctx = app.deps.tool_context(task_id="errand")
    result = await app.deps.registry.call("browse_to", {"url": "amazon.co.uk"}, ctx)
    assert result.ok, result.summary
    assert jarvis.opened == ["https://amazon.co.uk"]
    assert system_opens == []
    assert "Opened" in result.observation


# -- never a password -------------------------------------------------------------

@pytest.mark.parametrize("info", [
    {"type": "password"},
    {"type": "text", "autocomplete": "current-password"},
    {"autocomplete": "cc-number"},
    {"name": "cardNumber"},
    {"id": "cvc"},
    {"name": "card_expiry"},
])
def test_password_and_card_fields_are_refused(info):
    assert sensitive.refusal(info)


@pytest.mark.parametrize("info", [
    {"type": "email", "name": "email"},
    {"type": "search", "name": "field-keywords"},
    {"type": "text", "name": "postcode", "autocomplete": "postal-code"},
])
def test_ordinary_fields_are_not(info):
    assert sensitive.refusal(info) == ""


# -- the permission gate for keys and submits -------------------------------------

def test_enter_in_a_field_is_judged_by_where_its_form_goes():
    spec = PressPageKeyTool.spec
    checkout = {"role": "field", "text": "Postcode", "action": "https://shop.example/checkout/place"}
    assert consequence.classify("press_page_key", {"key": "enter"}, spec, checkout) is True
    assert consequence.classify("press_page_key", {"key": "escape"}, spec, checkout) is False
    search = {"role": "field", "text": "Search", "action": "https://shop.example/s"}
    assert consequence.classify("press_page_key", {"key": "enter"}, spec, search) is False
    # The same rule for typing with submit=True.
    assert consequence.classify("fill_page_field", {"text": "x", "submit": True}, spec, checkout) is True
    assert consequence.classify("fill_page_field", {"text": "x"}, spec, checkout) is False


def test_key_names_are_normalised():
    assert normalise_key("Page Down") == "PageDown"
    assert normalise_key("esc") == "Escape"
    assert normalise_key("return") == "Enter"
    assert normalise_key("F13") == ""


# -- handing over to the user -----------------------------------------------------

async def test_a_handoff_is_never_pre_approved_and_waits_its_own_time(app):
    app.config_store.update({"security": {"auto_approve": ["low", "medium", "high"],
                                          "confirmation_timeout_s": 0.05}})
    app.permissions.grant_task("t")

    async def answer_later():
        await asyncio.sleep(0.2)  # longer than the ordinary confirmation window
        pending = app.permissions.pending()
        assert pending and pending[0]["details"]["handoff"] is True
        assert pending[0]["details"]["offer_remember"] is False
        app.permissions.resolve(pending[0]["id"], True, remember=True)

    waiter = asyncio.create_task(answer_later())
    assert await app.permissions.require("ask_user_to_take_over", "low", "Please sign in",
                                         handoff=True, timeout_s=2.0, task_id="t")
    await waiter
    # "remember" never turns a handoff into a standing grant.
    assert "ask_user_to_take_over" not in app.permissions._session_grants


async def test_take_over_shows_the_window_and_carries_on_when_the_user_is_done(app, monkeypatch):
    driver = _Driver("JARVIS Chrome", owned=True)
    monkeypatch.setattr(hub_of(app.deps), "_pinned", driver)
    app.config_store.update({"browser": {"handoff_timeout_s": 5}})
    ctx = app.deps.tool_context()
    tool = TakeOverTool(app.deps)

    async def user_signs_in():
        while not app.permissions.pending():
            await asyncio.sleep(0.01)
        pending = app.permissions.pending()[0]
        assert "Sign in to Amazon".lower() in pending["summary"].lower()
        assert "JARVIS Chrome" in pending["summary"]
        app.permissions.resolve(pending["id"], True)

    helper = asyncio.create_task(user_signs_in())
    result = await tool.run({"reason": "Sign in to Amazon"}, ctx)
    await helper
    assert result.ok
    assert driver.fronted == 1
    assert "Look at the page again" in result.observation


async def test_take_over_declined_stops_the_task_there(app, monkeypatch):
    driver = _Driver("Safari")
    monkeypatch.setattr(hub_of(app.deps), "_pinned", driver)
    ctx = app.deps.tool_context()

    async def user_declines():
        while not app.permissions.pending():
            await asyncio.sleep(0.01)
        app.permissions.resolve(app.permissions.pending()[0]["id"], False)

    helper = asyncio.create_task(user_declines())
    result = await TakeOverTool(app.deps).run({"reason": "solve the CAPTCHA"}, ctx)
    await helper
    assert not result.ok
    assert "can't go past this point" in result.summary


async def test_saying_done_finishes_a_handoff(app, monkeypatch):
    driver = _Driver("JARVIS Chrome", owned=True)
    monkeypatch.setattr(hub_of(app.deps), "_pinned", driver)
    ctx = app.deps.tool_context()
    running = asyncio.create_task(TakeOverTool(app.deps).run({"reason": "sign in"}, ctx))
    while not app.permissions.pending():
        await asyncio.sleep(0.01)
    turn = await app.ask("I'm done")
    assert turn.decision.name == "affirm"
    assert (await running).ok
    assert any(e.payload.get("approved") for e in collect(app.bus, {"confirm.resolved"}))


@pytest.mark.parametrize("text", ["done", "I'm done", "ok done", "all done", "finished",
                                  "I'm signed in", "logged in", "I've finished"])
def test_done_words_affirm(text):
    decision = QuickCommands().match(text)
    assert decision is not None and decision.name == "affirm", text


@pytest.mark.parametrize("text", ["done with the email yet?", "is it finished downloading"])
def test_done_inside_a_sentence_is_not_an_affirmation(text):
    decision = QuickCommands().match(text)
    assert decision is None or decision.name != "affirm"


# -- what the model is told -------------------------------------------------------

def test_sign_in_and_captcha_pages_tell_the_model_to_hand_over():
    base = {"url": "https://x.example/login", "title": "Sign in", "elements": []}
    text = render_manifest({**base, "signals": {"password_field": True, "captcha": True}})
    assert "never type passwords" in text and "CAPTCHA" in text
    assert text.count("ask_user_to_take_over") == 2
    assert "Attention" not in render_manifest({**base, "signals": {}})


def test_the_next_look_says_what_changed():
    memory = PageMemory()
    driver = object()
    page = {"url": "https://shop.example/dp/1", "elements": [
        {"handle": "jv1", "role": "button", "text": "Add to Basket"}], "dialogs": []}
    assert memory.changes(driver, page) == ""
    same = memory.changes(driver, page)
    assert same == "No change since the last look."
    after = {**page, "elements": page["elements"] + [
        {"handle": "jv9", "role": "button", "text": "Proceed to checkout"}],
        "dialogs": ["Added to Basket"]}
    changed = memory.changes(driver, after)
    assert "New since the last look" in changed
    assert "Added to Basket" in changed and '[jv9] button "Proceed to checkout"' in changed
    assert memory.changes(driver, {**after, "url": "https://shop.example/cart"}) == "", \
        "a different page is a new page, not a change"
    assert memory.changes(("another task", driver), page) == "", "another task's look is its own"


# -- against a real browser -------------------------------------------------------

def _browser_available() -> bool:
    try:
        import playwright.async_api  # noqa: F401
    except ImportError:
        return False
    from evals.browser import _bundled_chromium

    return _bundled_chromium() is not None


live = pytest.mark.skipif(not _browser_available(), reason="Playwright/Chromium not installed")

_PAGE = """<!doctype html><html><head><title>Lab</title></head><body>
<main>
  <form action="/signin" method="post">
    <input type="email" name="email" aria-label="Email">
    <input type="password" name="password" aria-label="Password">
    <input name="cardnumber" aria-label="Card number">
  </form>
  <x-widget></x-widget>
  <iframe srcdoc="<textarea aria-label='Notes'></textarea>"></iframe>
  <button id="slow" type="button">Save</button><p id="status"></p>
  <div style="height:3000px"></div><p>Bottom</p>
</main>
<script>
customElements.define('x-widget', class extends HTMLElement {
  connectedCallback() {
    this.attachShadow({mode: 'open'}).innerHTML = '<button type="button">Inside shadow</button>';
  }
});
document.getElementById('slow').addEventListener('click', function() {
  fetch('/slow').then(function() { document.getElementById('status').textContent = 'saved'; });
});
</script></body></html>"""


@pytest.fixture
async def lab():
    from jarvis.surfaces.web.cdp import PlaywrightBrowser, PlaywrightDriver

    from evals.browser import _bundled_chromium

    browser = PlaywrightBrowser(headless=True, executable_path=_bundled_chromium())
    await browser.start()

    async def serve(route):
        if route.request.url.endswith("/slow"):
            await asyncio.sleep(0.8)
            await route.fulfill(status=200, body="ok")
        elif route.request.url.endswith("/two"):
            await route.fulfill(status=200, content_type="text/html", body="<title>Two</title>two")
        else:
            await route.fulfill(status=200, content_type="text/html", body=_PAGE)

    await browser.context.route("https://lab.example/**", serve)
    driver = PlaywrightDriver(browser)
    assert await driver.open("https://lab.example/")
    yield driver
    await browser.close()


async def _handle(driver, text):
    manifest = await driver.page_manifest(limit=80)
    return next(e["handle"] for e in manifest["elements"] if e["text"] == text), manifest


@live
async def test_live_the_listing_reaches_into_shadow_roots_and_frames(lab):
    manifest = await lab.page_manifest(limit=80)
    texts = {e["text"] for e in manifest["elements"]}
    assert {"Inside shadow", "Notes", "Email"} <= texts
    assert manifest["signals"]["password_field"] is True
    notes, _ = await _handle(lab, "Notes")
    result = await lab.fill_handle(notes, "hello from JARVIS")
    assert result["ok"] and result["input"] == "genuine"
    frame = lab.browser.page.frames[1]
    assert await frame.evaluate("document.querySelector('textarea').value") == "hello from JARVIS"
    shadow, _ = await _handle(lab, "Inside shadow")
    assert (await lab.click_handle(shadow))["ok"]


@live
async def test_live_neither_path_types_a_password_or_card_number(lab):
    from jarvis.tools.browser import manifest_js
    from jarvis.tools.browser.tools import _parse_js_json

    for label in ("Password", "Card number"):
        handle, _ = await _handle(lab, label)
        genuine = await lab.fill_handle(handle, "hunter2")
        assert genuine["ok"] is False and genuine.get("refused"), label
        scripted = _parse_js_json(await lab.run_js(manifest_js.build_fill_script(handle, "hunter2", False)))
        assert scripted["ok"] is False and scripted.get("refused"), label
    values = await lab.run_js("JSON.stringify([...document.querySelectorAll('input')].map(i => i.value))")
    assert json.loads(values) == ["", "", ""]


@live
async def test_live_the_look_after_a_click_waits_for_the_request_it_started(lab):
    from jarvis.tools.browser.observe import acted, settle

    handle, _ = await _handle(lab, "Save")
    assert (await lab.click_handle(handle))["ok"]
    acted(lab)
    # The page's own fetch takes 0.8s; the look after the click (every
    # look settles first) waits for it and for the page to update.
    await settle(lab)
    assert await lab.has_text("saved")


def test_same_url_ignores_a_trailing_slash_but_not_query_or_fragment():
    from jarvis.surfaces.web.cdp import _same_url

    assert _same_url("https://x.example/s?k=a", "https://x.example/s?k=a")
    assert _same_url("https://x.example", "https://x.example/"), "a bare domain is its root"
    assert _same_url("https://x.example/s/", "https://x.example/s"), "a trailing slash is nothing"
    assert _same_url("https://X.Example/s", "https://x.example/s"), "host is case-insensitive"
    assert not _same_url("https://x.example/s?k=a", "https://x.example/s?k=b"), "the query is the destination"
    assert not _same_url("https://x.example/s#a", "https://x.example/s#b"), "so is the fragment"
    assert not _same_url("https://x.example/s", "https://x.example/t")


@live
async def test_live_a_second_open_to_the_same_url_is_a_no_op(lab):
    """The AirPods bug's other half: repeating browse_to must not cost a
    real reload when nothing about the destination changed."""
    requests: list[str] = []
    lab.browser.context.on("request", lambda req: requests.append(req.url))
    assert await lab.open("https://lab.example/")
    assert not any("lab.example" in url for url in requests), \
        "already at that URL: open() must return without navigating"
    assert await lab.open("https://lab.example/two")
    assert any("lab.example/two" in url for url in requests), \
        "a genuinely different URL must still navigate"


@live
async def test_live_scroll_keys_and_back(lab):
    result = await lab.scroll("bottom")
    assert result["ok"] and result["y"] > 0
    handle, _ = await _handle(lab, "Email")
    assert (await lab.press_key("tab", handle))["ok"]
    assert (await lab.focused_handle()) != ""
    assert (await lab.press_key("F13"))["ok"] is False
    assert await lab.open("https://lab.example/two")
    assert (await lab.go_back())["ok"]
    assert (await lab.current_page())["title"] == "Lab"


@live
@pytest.mark.parametrize(("markup", "expected"), [
    # The invisible-reCAPTCHA badge that ordinary pages carry: nothing to do.
    ('<div class="grecaptcha-badge" style="width:256px;height:60px;position:fixed;bottom:0;right:0">'
     '<iframe src="https://www.google.com/recaptcha/api2/anchor?k=x&size=invisible" '
     'width="256" height="60"></iframe></div>', False),
    # A checkbox CAPTCHA the user has to tick.
    ('<div class="g-recaptcha"><iframe src="https://www.google.com/recaptcha/api2/anchor?k=x&size=normal" '
     'title="reCAPTCHA" width="304" height="78"></iframe></div>', True),
    ('<iframe src="https://challenges.cloudflare.com/turnstile/v0/x" width="300" height="65"></iframe>', True),
])
async def test_live_only_a_real_challenge_counts_as_a_captcha(markup, expected):
    from jarvis.surfaces.web.cdp import PlaywrightBrowser, PlaywrightDriver

    from evals.browser import _bundled_chromium

    browser = PlaywrightBrowser(headless=True, executable_path=_bundled_chromium())
    await browser.start()
    try:
        async def serve(route):
            body = f"<title>Shop</title><main><button>Buy</button>{markup}</main>"
            if "lab.example" not in route.request.url:
                body = "<html><body>widget</body></html>"
            await route.fulfill(status=200, content_type="text/html", body=body)

        await browser.context.route("**/*", serve)
        driver = PlaywrightDriver(browser)
        assert await driver.open("https://lab.example/")
        manifest = await driver.page_manifest()
        assert manifest["signals"]["captcha"] is expected
    finally:
        await browser.close()
