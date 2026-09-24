"""Seeing what the accessibility tree can't: on-screen text and numbered marks.

Some windows expose little or nothing to Accessibility — canvases, games,
some Electron apps, remote desktops. For those the window is photographed,
its text is read with Apple's on-device Vision framework (no model, no
network, a few hundred milliseconds), and everything worth pointing at gets
a number:

    [m1] button "Share"            ← a control from the accessibility tree
    [m2] text "Export as PDF…"     ← text read off the screenshot
    [m3] image (unlabelled)        ← a control with no name, still clickable

The operator clicks by number (``click_mark``), never by guessed
coordinates. When the thing to click has no words on it, a vision model is
shown the screenshot with the numbers drawn on and asked *which number* —
picking a mark is far more reliable than producing coordinates.

Pure logic here (building marks, converting between screenshot pixels and
screen points, drawing the overlay with Pillow when it's installed); the
text recognition itself is :func:`recognize_text`, macOS only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .ax import Control, Frame

#: Marks listed at most, in reading order.
MAX_MARKS = 120
#: Recognised text below this confidence is noise more often than not.
MIN_CONFIDENCE = 0.35


@dataclass
class TextBox:
    """Recognised text, in screenshot pixels (top-left origin)."""

    text: str
    x: float
    y: float
    w: float
    h: float
    confidence: float = 1.0


@dataclass
class Mark:
    number: int
    label: str
    kind: str
    frame: Frame
    source: str  # "ax" | "ocr"

    @property
    def handle(self) -> str:
        return f"m{self.number}"

    def line(self) -> str:
        label = self.label.replace('"', "'") if self.label else ""
        return f'[{self.handle}] {self.kind} "{label}"' if label else f"[{self.handle}] {self.kind} (unlabelled)"


def recognize_text(path: str | Path, *, fast: bool = False) -> list[TextBox]:
    """Text in an image, via Apple's Vision framework (macOS only;
    ``pyobjc-framework-Vision``). Synchronous — call it from a thread."""
    import Vision
    from Foundation import NSURL

    url = NSURL.fileURLWithPath_(str(path))
    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(url, {})
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(1 if fast else 0)   # 0 accurate, 1 fast
    request.setUsesLanguageCorrection_(not fast)
    ok, _error = handler.performRequests_error_([request], None)
    if not ok:
        return []
    width, height = image_size(path)
    boxes: list[TextBox] = []
    for observation in request.results() or []:
        candidates = observation.topCandidates_(1)
        if not candidates:
            continue
        top = candidates[0]
        box = observation.boundingBox()   # normalised, bottom-left origin
        boxes.append(from_normalised(str(top.string()), float(top.confidence()),
                                     (box.origin.x, box.origin.y, box.size.width, box.size.height),
                                     (width, height)))
    return boxes


def from_normalised(text: str, confidence: float, box: tuple[float, float, float, float],
                    size: tuple[int, int]) -> TextBox:
    """Vision's normalised, bottom-left-origin box → pixels, top-left origin."""
    bx, by, bw, bh = box
    width, height = size
    return TextBox(text=text, confidence=confidence, x=bx * width, y=(1.0 - by - bh) * height,
                   w=bw * width, h=bh * height)


def to_points(box: TextBox, size: tuple[int, int], window: Frame) -> Frame:
    """Screenshot pixels → global screen points (the unit clicks use).
    A Retina screenshot has two pixels per point; the ratio is measured,
    not assumed."""
    width, height = size
    sx = width / window.w if window.w else 1.0
    sy = height / window.h if window.h else 1.0
    return Frame(window.x + box.x / sx, window.y + box.y / sy, box.w / sx, box.h / sy)


def to_pixels(frame: Frame, size: tuple[int, int], window: Frame) -> tuple[float, float, float, float]:
    width, height = size
    sx = width / window.w if window.w else 1.0
    sy = height / window.h if window.h else 1.0
    return ((frame.x - window.x) * sx, (frame.y - window.y) * sy, frame.w * sx, frame.h * sy)


def build_marks(texts: list[TextBox], controls: list[Control], size: tuple[int, int],
                window: Frame) -> list[Mark]:
    """Controls (with their positions) and recognised text → numbered marks.

    Text inside a control names it when the control has no name of its own,
    and isn't listed again; text elsewhere gets a mark of its own. Numbered in
    reading order — top to bottom, then left to right — so the numbers make
    sense on the picture.
    """
    candidates: list[tuple[str, str, Frame, str]] = []
    placed: list[Frame] = []
    text_frames = [(t, to_points(t, size, window)) for t in texts
                   if t.text.strip() and t.confidence >= MIN_CONFIDENCE]
    for control in controls:
        frame = control.frame
        if frame is None or frame.empty or not frame.intersects(window) or not control.visible:
            continue
        label = control.label
        if not label:
            inside = [t.text for t, f in text_frames if _inside(f, frame)]
            label = " ".join(inside)[:80]
        candidates.append((label, control.role, frame, "ax"))
        placed.append(frame)
    for text, frame in text_frames:
        if any(_inside(frame, container) for container in placed):
            continue
        candidates.append((_clean(text.text), "text", frame, "ocr"))
    candidates.sort(key=lambda c: (round(c[2].y / 12), c[2].x))
    return [Mark(number=index, label=label, kind=kind, frame=frame, source=source)
            for index, (label, kind, frame, source) in enumerate(candidates[:MAX_MARKS], 1)]


def render_marks(marks: list[Mark], *, app: str = "", title: str = "") -> str:
    head = f"Marks on “{title}” — {app}" if title else f"Marks on {app or 'the screen'}"
    if not marks:
        return head + "\nNothing readable was found on screen."
    return "\n".join([head + " (click_mark with a number clicks it):", *(m.line() for m in marks)])


def draw_overlay(image_path: str | Path, marks: list[Mark], window: Frame,
                 out_path: str | Path) -> Path | None:
    """The screenshot with each mark's box and number drawn on — what a
    vision model is shown to pick a number. None without Pillow."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    with Image.open(image_path) as source:
        image = source.convert("RGB")
    size = image.size
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.load_default(size=max(12, size[0] // 90))
    except TypeError:                                # Pillow < 10.1
        font = ImageFont.load_default()
    for mark in marks:
        x, y, w, h = to_pixels(mark.frame, size, window)
        colour = (230, 40, 90) if mark.source == "ax" else (20, 110, 230)
        draw.rectangle([x, y, x + w, y + h], outline=colour, width=2)
        tag = str(mark.number)
        left, top, right, bottom = draw.textbbox((0, 0), tag, font=font)
        tw, th = right - left + 6, bottom - top + 4
        tx, ty = max(0, x - 1), max(0, y - th)
        draw.rectangle([tx, ty, tx + tw, ty + th], fill=colour)
        draw.text((tx + 3, ty + 1 - top), tag, fill=(255, 255, 255), font=font)
    out = Path(out_path)
    image.save(out, format="PNG")
    return out


def image_size(path: str | Path) -> tuple[int, int]:
    data = Path(path).read_bytes()[:32]
    if len(data) > 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    try:
        from PIL import Image

        with Image.open(path) as image:
            return image.size
    except Exception:
        return (0, 0)


def parse_pick(reply: str, count: int) -> int | None:
    """The mark number in a vision model's reply, if it named a real one."""
    match = re.search(r"\b(\d{1,3})\b", reply or "")
    if not match:
        return None
    number = int(match.group(1))
    return number if 1 <= number <= count else None


def _inside(inner: Frame, outer: Frame, slack: float = 2.0) -> bool:
    cx, cy = inner.center
    return (outer.x - slack <= cx <= outer.x + outer.w + slack
            and outer.y - slack <= cy <= outer.y + outer.h + slack)


def _clean(text: str) -> str:
    return " ".join(text.split())[:80]


__all__ = ["Mark", "TextBox", "build_marks", "draw_overlay", "from_normalised", "image_size",
           "parse_pick", "recognize_text", "render_marks", "to_pixels", "to_points"]
