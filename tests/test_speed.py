"""v3.0 Phase 8: where the time goes, and not wasting it.

Request timelines (core/latency.py) — time to first action, the model /
acting / looking / waiting split, background work and spoken answers landing
on the request that caused them. Page settling (tools/browser/observe.py) —
a settle right after another is free when nothing changed, anything that
acts makes the next one wait in full, and a page that shows it has nothing
queued needs only a short beat of stillness. Preloading the model when the
user starts talking, and the settings migration that brings the 12k context.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from jarvis.core import latency
from jarvis.core.events import EventBus, EventType
from jarvis.core.telemetry import Telemetry
from jarvis.tools.browser import observe


def _timings(bus: EventBus) -> list[dict]:
    return [e.payload for e in bus.history if e.type == EventType.REQUEST_TIMING]


# -- request timelines ----------------------------------------------------------

async def test_a_request_splits_its_time_between_model_acting_looking_and_waiting():
    bus = EventBus()
    telemetry = Telemetry(bus)
    timeline, token = telemetry.begin_request("r1", "add batteries to my basket")
    started = time.time()
    telemetry.record("model.chat", 400.0, started=started, slot="operator",
                     prompt_tokens=1200, completion_tokens=40)
    latency.current().note_wait(150.0)          # the page settling inside the click
    telemetry.record("tool.click_page_element", 250.0, started=started + 0.4)
    telemetry.record("tool.read_page_manifest", 60.0, started=started + 0.65)
    telemetry.record("voice.tts", 900.0)        # not part of the request's own work
    telemetry.end_turn(timeline, token, route="agent:operate")

    assert latency.current() is None, "the request isn't current once the turn returns"
    timing = telemetry.requests()[-1]
    assert timing["finished"] is True
    assert timing["model_calls"] == 1 and timing["tool_calls"] == 2
    assert timing["model_ms"] == 400
    assert timing["act_ms"] == 100, "waiting inside the click isn't acting"
    assert timing["wait_ms"] == 150 and timing["look_ms"] == 60
    assert timing["prompt_tokens"] == 1200 and timing["completion_tokens"] == 40
    assert 380 <= timing["first_action_ms"] <= 450
    assert [s["kind"] for s in timing["steps"]] == ["model", "act", "look"]
    assert _timings(bus)[-1]["id"] == "r1"
    assert telemetry.summary()["request.total"]["count"] == 1


async def test_background_work_lands_on_the_request_that_started_it():
    bus = EventBus()
    telemetry = Telemetry(bus)
    timeline, token = telemetry.begin_request("r2", "find me a kettle on amazon")

    async def errand():
        await asyncio.sleep(0.05)
        telemetry.record("tool.browse_to", 30.0, started=time.time())
        telemetry.finish_request()

    task = asyncio.create_task(errand())       # copies the context, like a real task
    telemetry.end_turn(timeline, token, route="capability:automation", task_id="t1")
    replied = telemetry.request("r2")
    assert replied["finished"] is False and replied["background"] is True

    await task
    done = telemetry.request("r2")
    assert done["finished"] is True and done["tool_calls"] == 1
    assert done["answered_ms"] >= done["replied_ms"], "the answer came after the acknowledgement"
    assert [t["finished"] for t in _timings(bus) if t["id"] == "r2"] == [False, True]


async def test_only_speech_of_the_result_counts_as_spoken(app, config):
    from jarvis.voice.manager import VoiceManager

    config.voice.enabled = True
    manager = VoiceManager(config, app.bus, app.telemetry)

    class Quiet:
        name = "quiet"

        async def speak(self, text):
            return True

        async def stop(self):
            return False

    manager.tts = Quiet()
    timeline, token = app.telemetry.begin_request("r3", "what's the weather", source="voice",
                                                  stt_ms=300.0)
    manager.enqueue("On it.")                  # an acknowledgement: not the answer
    await asyncio.sleep(0.02)
    assert timeline.spoken_ms is None
    latency.answering()
    manager.enqueue("It's sunny.")
    await asyncio.sleep(0.02)
    latency.deactivate(token)
    assert timeline.spoken_ms is not None
    assert timeline.as_dict()["heard_to_spoken_ms"] >= 300, "recognition is part of the wait"


async def test_a_turn_through_the_orchestrator_is_timed_end_to_end(app):
    result = await app.ask("what time is it")
    timing = app.telemetry.requests()[-1]
    assert timing["text"] == "what time is it" and timing["finished"]
    assert timing["route"] == f"{result.decision.kind}:{result.decision.name}"
    assert timing["first_action_ms"] is not None and timing["tool_calls"] >= 1


# -- page settling ------------------------------------------------------------------

class _Page:
    """A page that reports a DOM signature, work it has queued, and — like
    JARVIS Chrome — how long the network has been idle."""

    def __init__(self, *, queued: str = "0", idle: float = 10.0):
        self.queued = queued
        self.idle = idle
        self.mutations = 0
        self.calls = 0

    async def run_js(self, script, *, timeout=20.0):
        self.calls += 1
        if "readyState" in script and "__jarvisMutations" not in script:
            return "complete"
        return f"complete:https://shop.example/:{self.mutations}:40|{self.queued}"

    def network_idle_s(self):
        return self.idle


async def _timed_settle(page) -> float:
    started = time.monotonic()
    await observe.settle(page)
    return time.monotonic() - started


async def test_a_settle_right_after_another_is_free_when_nothing_changed():
    page = _Page()
    assert await _timed_settle(page) >= observe.IDLE_QUIET_S
    calls = page.calls
    assert await _timed_settle(page) < 0.05
    assert page.calls == calls + 1, "one look at the page, no waiting"


async def test_anything_that_acts_makes_the_next_settle_wait_in_full():
    page = _Page()
    await observe.settle(page)
    observe.acted(page)
    assert await _timed_settle(page) >= observe.IDLE_QUIET_S
    observe.acted()                              # a system-level action: every page
    assert await _timed_settle(page) >= observe.IDLE_QUIET_S


async def test_a_change_or_a_request_since_the_last_settle_means_waiting_again():
    page = _Page()
    await observe.settle(page)
    page.mutations += 1                         # the page changed by itself
    assert await _timed_settle(page) >= observe.IDLE_QUIET_S
    page.idle = 0.0                             # a request went out…
    waiting = asyncio.create_task(_timed_settle(page))
    await asyncio.sleep(0.3)
    page.idle = 5.0                             # …and came back
    assert await waiting >= 0.3


async def test_a_tool_outside_the_browser_invalidates_every_page(app, ctx):
    page = _Page()
    await observe.settle(page)
    assert await _timed_settle(page) < 0.05
    await app.deps.registry.call("set_volume", {"level": 30}, ctx)
    assert await _timed_settle(page) >= observe.IDLE_QUIET_S


async def test_a_page_with_work_queued_is_watched_for_the_full_half_second():
    idle = await _timed_settle(_Page(queued="0"))
    busy = await _timed_settle(_Page(queued="2"))          # a timer about to render
    unknown = await _timed_settle(_Page(queued="-1"))      # a browser that can't tell
    assert idle < 0.45
    assert busy >= 0.5 and unknown >= 0.5


async def test_after_a_request_the_page_must_stay_still_a_beat_longer():
    page = _Page(queued="-1", idle=0.0)
    waiting = asyncio.create_task(_timed_settle(page))
    await asyncio.sleep(0.1)
    page.idle = 0.0001
    started = time.monotonic()

    def idle():                                   # idle since the request finished
        return time.monotonic() - started

    page.network_idle_s = idle
    elapsed = await waiting
    assert elapsed >= 0.1 + observe.NETWORK_QUIET_S + 0.5 - 0.06


# -- against a real browser ---------------------------------------------------------

def _browser_available() -> bool:
    try:
        import playwright.async_api  # noqa: F401
    except ImportError:
        return False
    from evals.browser import _bundled_chromium

    return _bundled_chromium() is not None


live = pytest.mark.skipif(not _browser_available(), reason="Playwright/Chromium not installed")

_LAB = """<!doctype html><html><head><title>Timers</title></head><body>
<button id="later" type="button">Show later</button><p id="out"></p>
<script>
document.getElementById('later').addEventListener('click', function() {
  setTimeout(function() { document.getElementById('out').textContent = 'rendered late'; }, 450);
});
</script></body></html>"""


@pytest.fixture
async def timers():
    from jarvis.surfaces.web.cdp import PlaywrightBrowser, PlaywrightDriver

    from evals.browser import _bundled_chromium

    browser = PlaywrightBrowser(headless=True, executable_path=_bundled_chromium())
    await browser.start()

    async def serve(route):
        await route.fulfill(status=200, content_type="text/html", body=_LAB)

    await browser.context.route("https://timers.example/**", serve)
    driver = PlaywrightDriver(browser)
    assert await driver.open("https://timers.example/")
    yield driver
    await browser.close()


@live
async def test_live_a_render_queued_on_a_timer_is_waited_for(timers):
    manifest = await timers.page_manifest(limit=20)
    handle = next(e["handle"] for e in manifest["elements"] if e["text"] == "Show later")
    await observe.settle(timers)
    assert (await timers.click_handle(handle))["ok"]
    observe.acted(timers)
    await observe.settle(timers)
    assert await timers.has_text("rendered late")


@live
async def test_live_reading_the_page_is_not_mistaken_for_it_changing(timers):
    await observe.settle(timers)
    await timers.page_manifest(limit=20)       # tags every element with data-jarvis-id
    assert await observe._still_settled(timers)


@live
async def test_live_a_finished_page_settles_quickly(timers):
    await asyncio.sleep(0.4)
    assert await _timed_settle(timers) < 0.45


# -- the model, ready when it's needed ---------------------------------------------

async def test_the_model_is_loaded_when_the_user_starts_talking(app, monkeypatch):
    from jarvis.models.registry import Slot

    loads: list[tuple[str, dict]] = []

    class Provider:
        name = "ollama"
        local = True
        accepts_runtime_options = True

        async def preload(self, model, **runtime):
            loads.append((model, runtime))
            return True

    class Resolution:
        provider = Provider()
        model = "qwen3:8b"

    async def resolve(slot, max_age=60.0):
        return Resolution()

    monkeypatch.setattr(app.models, "resolve", resolve)
    app.bus.add_hook(app._preload_when_spoken_to)
    app.bus.publish(EventType.WAKE, word="jarvis", score=0.9)
    app.bus.publish(EventType.WAKE, word="jarvis", score=0.9)   # moments later
    await asyncio.sleep(0.02)
    assert len(loads) == 1, "at most once a minute"
    conf = app.models.slot_config(Slot.OPERATOR)
    assert loads[0][1]["num_ctx"] == conf.num_ctx, "the same context size as the calls, or it reloads"


async def test_the_resident_model_gets_a_12k_context_and_old_files_move_to_it(tmp_path):
    from jarvis.core.config import CONFIG_VERSION, Config, load_config

    assert Config().models.general.num_ctx == 12288
    old = Config().model_dump()
    old["config_version"] = 3
    old["models"]["general"]["num_ctx"] = 8192
    old["models"]["general"]["model"] = "llama3.1:8b"   # chosen after the v3 upgrade
    path = tmp_path / "config.json"
    path.write_text(json.dumps(old), encoding="utf-8")
    config = load_config(path)
    assert config.models.general.num_ctx == 12288
    assert config.models.general.model == "llama3.1:8b", \
        "an upgrade only applies the changes made since the file's own version"
    assert config.config_version == CONFIG_VERSION


# -- the paths that use them ----------------------------------------------------------

async def test_clicking_on_a_page_makes_the_look_after_it_wait_in_full(app, ctx):
    from jarvis.tools.browser.page_tools import ClickPageElementTool

    class Shop(_Page):
        app_name = "JARVIS Chrome"
        owned = True

        async def current_page(self):
            return {"url": "https://shop.example/", "title": "Shop"}

        async def click_handle(self, handle):
            return {"ok": True, "text": "Add to Basket", "url": "https://shop.example/",
                    "title": "Shop"}

    page = Shop()
    with app.deps.browsers.pin(page):
        await observe.settle(page)
        result = await ClickPageElementTool(app.deps).run({"handle": "jv1"}, ctx)
        assert result.ok
        assert await _timed_settle(page) >= observe.IDLE_QUIET_S, \
            "the page may be about to change: the look after a click can't reuse the one before"


async def test_the_acknowledgement_is_not_the_answer_being_spoken(app):
    from jarvis.router.schema import RouteDecision, RouteKind

    class Voice:
        def __init__(self):
            self.queued: list[tuple[str, bool]] = []

        def enqueue(self, text):
            self.queued.append((text, latency.speech_marker() is not None))

    voice = Voice()
    app.orchestrator.voice = voice
    decision = RouteDecision(RouteKind.CAPABILITY, "automation", {})

    async def errand(task):
        return "Added the batteries to your basket."

    timeline, token = app.telemetry.begin_request("r9", "add batteries to my basket")
    turn = await app.orchestrator._run_in_background("add batteries", decision, "Errand", errand)
    app.telemetry.end_turn(timeline, token, route="capability:automation", task_id=turn.task_id)
    await app.tasks.get(turn.task_id)._runner
    assert voice.queued[0][1] is False, "“On it” isn't the answer"
    assert voice.queued[-1] == ("Added the batteries to your basket.", True)
    assert app.telemetry.request("r9")["finished"] is True
