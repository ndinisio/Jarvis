"""JARVIS' voice.

The persona is a *constraint system*, not a costume: British, composed,
precise, quietly witty, never theatrical. Short answers are the default; "sir"
appears occasionally rather than in every sentence; and the most common
utterances (greetings, acknowledgements, confirmations) are answered from a
phrasebook with zero model involvement, which is both faster and more
consistent than asking a 1B model to be charming.
"""

from __future__ import annotations

import datetime as dt
import random
import re
from collections.abc import Iterable

from .config import Config

SYSTEM_IDENTITY = """You are JARVIS, a private computer intelligence running locally on the user's Mac.

Voice and manner:
- British, composed, precise. Quietly witty at most; never theatrical or effusive.
- Brief by default. One or two sentences unless detail is genuinely required.
- Address the user as "{honorific}" occasionally — not in every reply.
- State what you did or found. Do not narrate your reasoning or your own capabilities.
- Never use filler such as "Certainly!", "Great question", "As an AI", or emoji.
- If you don't know, say so in one sentence and offer the next useful step.
- Facts you were given are facts; anything you infer must be marked as inference.

You are speaking aloud as well as writing, so avoid markdown headers, bullet
symbols and code fences in conversational replies."""


ACKNOWLEDGEMENTS = [
    "On it, sir.",
    "I'll look into it.",
    "Working on that now.",
    "Give me a moment.",
    "Right away.",
    "I'll investigate that.",
    "Looking into it now, sir.",
]

RESEARCH_ACKS = [
    "I'll look into it, sir.",
    "I'll investigate and report back.",
    "Starting the research now.",
    "Give me a few moments to look through this.",
]

DIAGNOSTIC_ACKS = [
    "Let me take a look.",
    "Running the diagnostics now.",
    "I'll check the machine over.",
    "One moment — measuring.",
]

MAIL_ACKS = ["I'll check them, sir.", "Looking at your mail now.", "Checking the inbox."]

CALENDAR_ACKS = ["Let me check your calendar.", "Looking at your schedule."]

SCREEN_ACKS = ["Let me look.", "Taking a look at your screen."]

AUTOMATION_ACKS = [
    "On it — I'll talk you through it as I go.",
    "Right away. I'll keep you posted as I work through it.",
    "Starting now, sir — I'll narrate anything that takes a moment.",
]

_ACK_BY_KIND = {
    "research": RESEARCH_ACKS,
    "system": DIAGNOSTIC_ACKS,
    "diagnostics": DIAGNOSTIC_ACKS,
    "mail": MAIL_ACKS,
    "email": MAIL_ACKS,
    "calendar": CALENDAR_ACKS,
    "screen": SCREEN_ACKS,
    "automation": AUTOMATION_ACKS,
}

GREETINGS_MORNING = ["Good morning, sir.", "Morning, sir. How may I assist?"]
GREETINGS_AFTERNOON = ["Good afternoon, sir.", "Good afternoon. How may I assist?"]
GREETINGS_EVENING = ["Good evening, sir.", "Good evening. What can I do for you?"]
GREETINGS_NEUTRAL = ["Hello, sir.", "At your service.", "Yes, sir?"]

WAKE_RESPONSES = ["Yes, sir?", "Sir?", "Listening.", "Go ahead, sir.", "Yes?"]

DONE = ["Done, sir.", "Done.", "That's done.", "Taken care of.", "Complete."]

CANCELLED = ["Stopped.", "Cancelled, sir.", "I've halted that."]

UNKNOWN = [
    "I'm not sure how to do that yet.",
    "That's beyond what I can do at the moment, sir.",
]


class Personality:
    def __init__(self, config: Config):
        self._config = config
        self._recent: list[str] = []

    def reconfigure(self, config: Config) -> None:
        self._config = config

    @property
    def honorific(self) -> str:
        return self._config.personality.address_user_as or "sir"

    # -- phrasebook --------------------------------------------------------
    def _pick(self, options: Iterable[str]) -> str:
        pool = [o for o in options if o not in self._recent] or list(options)
        choice = random.choice(pool)
        self._recent.append(choice)
        if len(self._recent) > 6:
            self._recent.pop(0)
        return self._apply_honorific(choice)

    def _apply_honorific(self, text: str) -> str:
        honorific = self.honorific
        if honorific.lower() != "sir":
            # Phrasebook entries are written with "sir"; swap in the user's
            # preferred form, keeping the original capitalisation.
            def _swap(match: re.Match) -> str:
                word = match.group(0)
                return honorific.capitalize() if word[0].isupper() else honorific

            text = re.sub(r"\bsir\b", _swap, text, flags=re.I)
        frequency = self._config.personality.honorific_frequency
        if honorific.lower() in text.lower() and random.random() > frequency + 0.35:
            text = re.sub(r",?\s*\b" + re.escape(honorific) + r"\b", "", text, flags=re.I)
            text = text.strip()
            if text and text[0].islower():
                text = text[0].upper() + text[1:]
            if text and text[-1] not in ".?!":
                text += "."
        return text or f"Yes, {honorific}?"

    def greeting(self, now: dt.datetime | None = None) -> str:
        now = now or dt.datetime.now()
        if 5 <= now.hour < 12:
            return self._pick(GREETINGS_MORNING)
        if 12 <= now.hour < 18:
            return self._pick(GREETINGS_AFTERNOON)
        if 18 <= now.hour < 23:
            return self._pick(GREETINGS_EVENING)
        return self._pick(GREETINGS_NEUTRAL)

    def wake_response(self) -> str:
        return self._pick(WAKE_RESPONSES)

    def acknowledgement(self, long_running: bool = False, kind: str = "") -> str:
        """Acknowledge work that is about to start.

        Research earns "I'll look into it"; a diagnostic sweep or a mail check
        does not — saying "starting the research now" about the battery reads
        as a script rather than an assistant.
        """
        if kind in _ACK_BY_KIND:
            return self._pick(_ACK_BY_KIND[kind])
        return self._pick(ACKNOWLEDGEMENTS)

    def done(self) -> str:
        return self._pick(DONE)

    def cancelled(self) -> str:
        return self._pick(CANCELLED)

    def unknown(self) -> str:
        return self._pick(UNKNOWN)

    def failure(self, what: str, reason: str = "", remedy: str = "") -> str:
        text = what.rstrip(".") + "."
        if reason:
            text += f" {reason.rstrip('.')}."
        if remedy:
            text += f" {remedy.rstrip('.')}."
        return text

    # -- prompts -----------------------------------------------------------
    def system_prompt(self, extra: str = "") -> str:
        base = SYSTEM_IDENTITY.format(honorific=self.honorific)
        style = self._config.personality.style_notes
        if style:
            base += f"\nStyle notes: {style}"
        if extra:
            base += "\n\n" + extra
        return base


SPEECH_STRIP = re.compile(r"[*_`#>]|\[(.*?)\]\((.*?)\)")


def speakable(text: str, max_chars: int = 600) -> str:
    """Turn a written answer into something worth hearing.

    Markdown is stripped, URLs are shortened to their domain, and long answers
    are truncated at a sentence boundary — the detail stays on screen.
    """
    if not text:
        return ""
    cleaned = SPEECH_STRIP.sub(lambda m: m.group(1) or "", text)
    cleaned = re.sub(r"https?://([^\s/]+)\S*", r"\1", cleaned)
    cleaned = re.sub(r"^\s*[-•]\s*", "", cleaned, flags=re.M)
    cleaned = re.sub(r"\n{2,}", ". ", cleaned)
    cleaned = re.sub(r"\s*\n\s*", ". ", cleaned)
    cleaned = re.sub(r"\.{2,}", ".", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    if len(cleaned) <= max_chars:
        return cleaned
    cut = cleaned[:max_chars]
    for boundary in (". ", "? ", "! "):
        index = cut.rfind(boundary)
        if index > max_chars * 0.5:
            return cut[: index + 1]
    return cut.rsplit(" ", 1)[0] + "…"


def sentences(text: str) -> list[str]:
    """Split into natural speech units so TTS isn't fed fragments."""
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", (text or "").strip())
    return [p.strip() for p in parts if p.strip()]
