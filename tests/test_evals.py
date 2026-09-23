"""The evaluation harness itself: suites, checks, mock sites, oracle, and a live run.

The harness is what every v3.0 claim is measured with, so it gets tested like
production code: a broken check or a mock site that doesn't record state
would make a failing JARVIS look like a passing one.
"""

from __future__ import annotations

import httpx
import pytest

from evals import checks
from evals.mock_sites.hosts import mock_url
from evals.mock_sites.server import MockServer
from evals.oracle import OracleBrain, find_element
from evals.suites import load_mac_tasks, load_utterances, load_web_tasks


# ---------------------------------------------------------------------------- suites
def test_web_suite_is_well_formed():
    tasks = load_web_tasks()
    assert len(tasks) >= 40
    for task in tasks:
        assert task.phrasings, task.id
        assert task.checks, task.id
        for check in task.checks:
            (name, _), = check.items()
            assert name in checks.CHECK_NAMES, f"{task.id}: unknown check {name}"
        if "real-only" not in task.tags:
            assert task.oracle, f"{task.id} needs an oracle recipe"


def test_every_safety_task_proves_something_was_not_done():
    negative = {"no_orders", "no_checkout", "no_mail_sent", "no_password_typed"}
    for task in load_web_tasks():
        if task.category == "safety":
            names = {name for check in task.checks for name in check}
            assert names & negative, f"{task.id} must assert a consequential action did not happen"


def test_understanding_corpus_is_large_and_labelled():
    utterances = load_utterances()
    assert len(utterances) >= 300
    assert {u.mode for u in utterances} <= {"act", "chat"}
    assert len({u.text for u in utterances}) == len(utterances), "duplicate utterances"
    tags = {tag for u in utterances for tag in u.tags}
    assert {"compound", "hijack", "colloquial", "chat-domain", "asr", "polite"} <= tags


def test_mac_suite_loads():
    tasks = load_mac_tasks()
    assert len(tasks) >= 20
    assert all(task.check for task in tasks)


# ---------------------------------------------------------------------------- checks
def _state(**amazon):
    base = {"cart": [], "orders": [], "checkout_reached": False, "signin_attempts": [],
            "cookies": None, "visits": []}
    base.update(amazon)
    return {"amazon": base, "mail": {"sent": []}, "events": {"registrations": []},
            "tasks": {"lists": {}, "shared": [], "feedback": []}}


def test_checks_pass_and_fail_on_ground_truth():
    cart = [{"asin": "B0MOUSEM185", "title": "Logitech M185 Wireless Mouse", "qty": 2,
             "variant": "Red", "price": 12.99}]
    state = _state(cart=cart)
    assert checks.evaluate([{"basket_has": {"asin": "B0MOUSEM185", "variant": "Red", "qty": 2}}],
                           state, {}) == []
    assert checks.evaluate([{"basket_has": {"asin": "B0MOUSEM185", "variant": "Blue"}}], state, {})
    assert checks.evaluate([{"no_orders": True}], _state(orders=[{"asin": "x"}]), {})
    assert checks.evaluate([{"confirmation_requested": "Buy Now"}], state,
                           {"confirmations": [{"summary": 'Click "Buy Now" on the page?'}]}) == []
    assert checks.evaluate([{"no_such_check": 1}], state, {}) == ["unknown check 'no_such_check'"]


def test_basket_has_any_accepts_alternatives():
    state = _state(cart=[{"asin": "B0CABLE1M2", "title": "cable", "qty": 1, "variant": "", "price": 1}])
    assert checks.evaluate([{"basket_has_any": [{"asin": "B0CABLE2M1"}, {"asin": "B0CABLE1M2"}]}],
                           state, {}) == []


# ---------------------------------------------------------------------------- mock sites
def test_hostnames_map_onto_the_mock_and_everything_else_is_blocked():
    assert mock_url("https://www.amazon.co.uk/s?k=usb", "http://127.0.0.1:9") == \
        "http://127.0.0.1:9/amazon/s?k=usb"
    assert mock_url("https://duckduckgo.com/?q=x", "http://h") == "http://h/search/?q=x"
    assert mock_url("https://example.org/", "http://h") is None


def test_mock_shop_records_the_basket_and_resets():
    with MockServer() as server:
        response = httpx.post(f"{server.url}/amazon/api/cart/add",
                              json={"asin": "B0MOUSEM185", "quantity": 1, "variant": ""})
        assert response.status_code == 400, "a missing required colour must be refused"
        httpx.post(f"{server.url}/amazon/api/cart/add",
                   json={"asin": "B0MOUSEM185", "quantity": 2, "variant": "Blue"}).raise_for_status()
        assert server.state()["amazon"]["cart"][0]["qty"] == 2
        server.reset({"amazon": {"cart": [{"asin": "B0HDMI21X3", "qty": 1}]}})
        assert [i["asin"] for i in server.state()["amazon"]["cart"]] == ["B0HDMI21X3"]


def test_mock_forms_validate_like_a_real_site():
    with MockServer() as server:
        page = httpx.post(f"{server.url}/events/register",
                          data={"name": "A", "email": "a@example.com", "city": "Atlantis", "ticket": "vip"})
        assert "choose your city" in page.text
        assert server.state()["events"]["registrations"] == []
        httpx.post(f"{server.url}/events/register",
                   data={"name": "A", "email": "a@example.com", "city": "leeds", "ticket": "vip"})
        assert server.state()["events"]["registrations"][0]["city"] == "Leeds"


# ---------------------------------------------------------------------------- oracle
def test_oracle_grounds_only_on_elements_it_was_shown():
    prompt = ('[jv3] field "Search Amazon.co.uk" name="k"\n'
              '[jv9] button "Add to Basket"\n[jv10] link "Basket 0"')
    assert find_element(prompt, "Add to Basket").handle == "jv9"
    assert find_element(prompt, "Search Amazon.co.uk", fillable=True).handle == "jv3"
    assert find_element("60 elements found on the page.", "Add to Basket") is None


def test_oracle_looks_again_then_gives_up_when_nothing_is_shown():
    brain = OracleBrain()
    brain.begin([{"click": "Add to Basket"}])
    first = brain.next_action("nothing useful")
    assert first["tool"] == "read_page_manifest"
    brain.on_tool_result("read_page_manifest", True)
    assert brain.next_action("still nothing")["tool"] == "read_page_manifest"
    assert brain.next_action("still nothing")["action"] == "give_up"


def test_oracle_advances_only_on_a_successful_result():
    brain = OracleBrain()
    brain.begin([{"go": "https://www.amazon.co.uk/"}, {"click": "Add to Basket"}])
    assert brain.next_action("")["tool"] == "browse_to"
    brain.on_tool_result("browse_to", False)
    assert brain.next_action("")["tool"] == "browse_to", "a failed step is retried, not skipped"
    brain.on_tool_result("browse_to", True)
    assert brain.next_action('[jv2] button "Add to Basket"')["arguments"]["handle"] == "jv2"


# ---------------------------------------------------------------------------- live run
def _browser_available() -> bool:
    try:
        import playwright.async_api  # noqa: F401
    except ImportError:
        return False
    from evals.browser import _bundled_chromium

    return _bundled_chromium() is not None


live = pytest.mark.skipif(not _browser_available(), reason="Playwright/Chromium not installed")


@live
async def test_harness_runs_a_real_jarvis_turn_against_the_mock_site():
    from evals.harness import Harness
    from evals.run_web import select

    task = select(load_web_tasks(), ids="shop-deals")[0]
    async with Harness(model="oracle", task_timeout_s=60) as harness:
        result = await harness.run_task(task, task.phrasings[0])
    assert result.ok, result.failures
    assert "browse_to" in result.tools
    assert result.model_calls > 0


@live
async def test_harness_blocks_the_real_internet():
    from evals.harness import Harness

    async with Harness(model="oracle") as harness:
        await harness.driver.open("https://example.org/")
        assert (await harness.driver.current_page())["title"] == "Blocked"
        assert await harness.driver.open("https://www.amazon.co.uk/")
        assert "amazon.co.uk" in (await harness.driver.current_page())["url"]
