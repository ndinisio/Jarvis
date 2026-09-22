"""scroll_quartz: real scroll-wheel event synthesis.

There is no macOS host here to run PyObjC/Quartz on, so what's actually
verifiable is that the right calls happen, in the right order, with the
right argument shapes — proven against a fake Quartz module injected via
sys.modules, the same technique used elsewhere in this suite for a
dependency this environment can't provide for real (see test_imap_email.py
for imaplib/smtplib). The module's own docstring states plainly that the
on-screen effect, including the direction sign, has never been confirmed
against a real scroll gesture.
"""

from __future__ import annotations

import sys

import pytest
from jarvis.tools.interaction import scroll_quartz


class _FakeQuartz:
    kCGScrollEventUnitLine = "line"
    kCGHIDEventTap = "hid"

    def __init__(self, *, fail: bool = False, fail_after: int | None = None):
        self.calls: list[tuple] = []
        self.posted: list[object] = []
        self._fail = fail
        self._fail_after = fail_after

    def CGEventCreateScrollWheelEvent(self, source, units, wheel_count, wheel1):
        self.calls.append((source, units, wheel_count, wheel1))
        return ("event", wheel1)

    def CGEventPost(self, tap, event):
        if self._fail_after is not None and len(self.posted) >= self._fail_after:
            raise RuntimeError("posting failed partway through")
        if self._fail:
            raise RuntimeError("posting failed")
        self.posted.append((tap, event))


@pytest.fixture(autouse=True)
def _clean_quartz_module():
    sys.modules.pop("Quartz", None)
    yield
    sys.modules.pop("Quartz", None)


def test_available_is_false_when_quartz_cannot_be_imported():
    sys.modules.pop("Quartz", None)
    assert scroll_quartz.available() is False


def test_available_is_true_when_quartz_is_importable():
    sys.modules["Quartz"] = _FakeQuartz()
    assert scroll_quartz.available() is True


def test_scroll_posts_one_event_per_unit_of_amount():
    fake = _FakeQuartz()
    sys.modules["Quartz"] = fake
    assert scroll_quartz.scroll("down", 3) is True
    assert len(fake.calls) == 3
    assert len(fake.posted) == 3


def test_scroll_up_and_down_use_opposite_signs():
    fake = _FakeQuartz()
    sys.modules["Quartz"] = fake
    scroll_quartz.scroll("up", 1)
    scroll_quartz.scroll("down", 1)
    up_wheel1 = fake.calls[0][3]
    down_wheel1 = fake.calls[1][3]
    assert up_wheel1 > 0 and down_wheel1 < 0
    assert up_wheel1 == -down_wheel1


def test_scroll_uses_the_line_unit_and_the_hid_event_tap():
    fake = _FakeQuartz()
    sys.modules["Quartz"] = fake
    scroll_quartz.scroll("down", 1)
    _source, units, wheel_count, _wheel1 = fake.calls[0]
    assert units == fake.kCGScrollEventUnitLine
    assert wheel_count == 1
    tap, _event = fake.posted[0]
    assert tap == fake.kCGHIDEventTap


def test_scroll_rejects_an_unrecognised_direction_without_touching_quartz():
    fake = _FakeQuartz()
    sys.modules["Quartz"] = fake
    assert scroll_quartz.scroll("sideways", 1) is False
    assert fake.calls == []


def test_scroll_returns_false_without_raising_when_quartz_is_missing():
    sys.modules.pop("Quartz", None)
    assert scroll_quartz.scroll("down", 1) is False


def test_scroll_returns_false_without_raising_when_posting_fails():
    sys.modules["Quartz"] = _FakeQuartz(fail=True)
    assert scroll_quartz.scroll("down", 1) is False


def test_scroll_treats_a_non_positive_amount_as_one_event():
    fake = _FakeQuartz()
    sys.modules["Quartz"] = fake
    scroll_quartz.scroll("down", 0)
    assert len(fake.calls) == 1


def test_scroll_returns_true_if_even_one_event_posted_before_a_later_failure():
    """A real bug this closes: the first implementation returned False for
    *any* failure in the loop, even one that happened after several events
    had already posted successfully. ScrollTool falls back to the
    key-based path whenever scroll() returns False — so that would have
    meant some scroll-wheel events already fired, and then the key-based
    fallback scrolled again on top of them: a double action, the same
    failure shape as the double-TTS bug this session already fixed once
    in orchestrator.py's _respond. Posting some events and then hitting a
    real failure must still report success, so the caller does not retry
    with a different mechanism."""
    fake = _FakeQuartz(fail_after=2)
    sys.modules["Quartz"] = fake
    result = scroll_quartz.scroll("down", 5)
    assert result is True
    assert len(fake.posted) == 2  # exactly the ones that succeeded before the failure


def test_scroll_returns_false_only_when_nothing_posted_at_all():
    fake = _FakeQuartz(fail_after=0)  # fails on the very first attempt
    sys.modules["Quartz"] = fake
    assert scroll_quartz.scroll("down", 5) is False
    assert fake.posted == []


def test_scroll_stops_trying_after_the_first_failure_rather_than_continuing():
    """Once a post fails, retrying the remaining repeats risks the same
    failure mode again for no benefit — one partial scroll is a better
    outcome than repeatedly hammering a call that just failed."""
    fake = _FakeQuartz(fail_after=1)
    sys.modules["Quartz"] = fake
    scroll_quartz.scroll("down", 5)
    assert len(fake.calls) == 2  # the one that succeeded, and the one that failed
    assert len(fake.posted) == 1
