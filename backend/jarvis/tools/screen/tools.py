"""Screen capture and visual understanding.

Capture is on demand by default: `capture_screen`/`analyse_screen` only ever
run because a tool call asked for one, the image is written into the
workspace, and the same image is shown in the UI so the user sees exactly
what JARVIS saw. `watch_screen` is the one exception — it exists for the
background screen watcher (`vision/watcher.py`, gated behind the
off-by-default `capabilities.screen_awareness`), which polls a cheap signal
constantly but only calls `watch_screen` — a real capture and a real
vision-model call — when that signal changes, and even then no more often
than `ScreenAwarenessConfig.min_vision_interval_s`. It deliberately shows
nothing in the UI and never uploads an image anywhere new; it just keeps
`ConversationState.screen` fresh via the same tool-result plumbing every
other tool uses.
"""

from __future__ import annotations

import base64
import datetime as dt
import io
from typing import Any

from ...core.errors import ModelUnavailable
from ...models.base import ChatMessage
from ...models.registry import Slot
from ...security.permissions import RiskLevel
from ..base import Tool, ToolContext, ToolResult, ToolSpec

#: Vision models don't need a retina-resolution screenshot, and a smaller image
#: is dramatically faster through a local model.
MAX_EDGE = 1400


class CaptureScreenTool(Tool):
    spec = ToolSpec(
        name="capture_screen",
        description="Capture the current screen to an image",
        parameters={
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["full", "window", "selection"],
                         "default": "full"},
                "display": {"type": "integer"},
            },
        },
        risk=RiskLevel.LOW,
        category="screen",
        expected_ms=900,
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if not self._deps.config.security.allow_screen_capture:
            return ToolResult.failure("Screen capture is disabled in the configuration.")
        capture = await capture_to_workspace(self._deps, ctx, args.get("mode", "full"),
                                             args.get("display"))
        return ToolResult(
            data={"path": str(capture["path"]), "width": capture["width"],
                  "height": capture["height"]},
            summary="Captured the screen.",
            display={"kind": "image", "title": "Screen capture", "image": capture["data_url"],
                     "path": str(capture["path"])},
        )


class AnalyseScreenTool(Tool):
    spec = ToolSpec(
        name="analyse_screen",
        description="Capture the screen and describe or answer a question about what is on it",
        parameters={
            "type": "object",
            "properties": {
                "question": {"type": "string",
                             "default": "Describe what is on this screen."},
                "mode": {"type": "string", "enum": ["full", "window", "selection"],
                         "default": "full"},
            },
        },
        risk=RiskLevel.LOW,
        category="screen",
        expected_ms=8000,
        examples=["What's on my screen?", "What does this error mean?", "Can you see what I'm doing?"],
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if not self._deps.config.security.allow_screen_capture:
            return ToolResult.failure("Screen capture is disabled in the configuration.")
        ctx.report("Capturing the screen…", tool="analyse_screen")
        capture = await capture_to_workspace(self._deps, ctx, args.get("mode", "full"))

        ctx.bus.publish(
            "screen.image", image=capture["data_url"], path=str(capture["path"]),
            task_id=ctx.task_id,
        )
        ctx.raise_if_cancelled()
        ctx.report("Analysing the image…", tool="analyse_screen")

        question = args.get("question") or "Describe what is on this screen."
        try:
            completion = await _describe_capture(self._deps, capture, question, Slot.VISION)
        except ModelUnavailable as exc:
            return ToolResult(
                ok=False,
                data={"path": str(capture["path"])},
                summary="I captured the screen, but no vision model is available to read it.",
                error=exc.detail or exc.user_message,
                display={"kind": "image", "title": "Screen capture",
                         "image": capture["data_url"], "path": str(capture["path"])},
            )
        answer = completion.text.strip()
        return ToolResult(
            data={"answer": answer, "path": str(capture["path"]), "model": completion.model},
            summary=answer,
            display={"kind": "image", "title": "Screen analysis", "image": capture["data_url"],
                     "path": str(capture["path"]), "text": answer},
        )


async def _describe_capture(deps, capture: dict[str, Any], question: str, slot: str):
    """Ask a vision-capable slot about a capture. Raises ModelUnavailable if none is ready."""
    prompt = (
        "You are looking at a screenshot of the user's Mac. Answer precisely and briefly. "
        "Read any visible text exactly. If the question can't be answered from the image, "
        "say so plainly.\n\nQuestion: " + question
    )
    messages = [
        ChatMessage("system", "You describe screenshots factually and concisely."),
        ChatMessage("user", prompt, images=[capture["base64"]]),
    ]
    return await deps.models.complete(slot, messages)


_WATCH_PROMPT = (
    "Briefly describe what's changed on screen and flag anything that plainly needs the "
    "user's attention (an error, a finished download, a blocking dialog)."
)


class WatchScreenTool(Tool):
    """Internal tool for the background screen watcher (see vision/watcher.py).

    Not offered to the planner/model shortlist — the watcher calls it
    directly through the registry, which is what gets its result absorbed
    into ConversationState.screen for free (see intelligence/state.py:
    attach()/_absorb(), category == "screen"). Deliberately no `display`
    payload: unlike analyse_screen, this never shows an image in the UI —
    the watcher's whole point is to stay silent unless narration is
    separately enabled.
    """

    spec = ToolSpec(
        name="watch_screen",
        description="Capture the screen and briefly note what changed, for the background watcher",
        parameters={"type": "object", "properties": {}},
        risk=RiskLevel.LOW,
        category="screen",
        expected_ms=3000,
    )

    def __init__(self, deps):
        self._deps = deps

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if not self._deps.config.security.allow_screen_capture:
            return ToolResult.failure("Screen capture is disabled in the configuration.")
        capture = await capture_to_workspace(self._deps, ctx, "full")
        ctx.raise_if_cancelled()
        try:
            completion = await _describe_capture(self._deps, capture, _WATCH_PROMPT, Slot.SCREEN_WATCH)
        except ModelUnavailable as exc:
            return ToolResult.failure(
                "The screen watcher's vision model isn't available.",
                detail=exc.detail or exc.user_message,
            )
        answer = completion.text.strip()
        return ToolResult(
            data={"answer": answer, "path": str(capture["path"]), "model": completion.model},
            summary=answer,
        )


async def capture_to_workspace(deps, ctx: ToolContext, mode: str = "full",
                               display: int | None = None) -> dict[str, Any]:
    """Capture, downscale, persist and encode a screenshot."""
    captures = deps.config.captures_dir
    captures.mkdir(parents=True, exist_ok=True)
    path = captures / f"screen-{dt.datetime.now():%Y%m%d-%H%M%S}.png"
    await deps.controller.capture_screen(
        path, display=display, window=(mode == "window"), interactive=(mode == "selection")
    )
    raw = path.read_bytes()
    width = height = 0
    encoded = raw
    try:
        from PIL import Image  # type: ignore

        with Image.open(io.BytesIO(raw)) as image:
            image = image.convert("RGB")
            width, height = image.size
            if max(image.size) > MAX_EDGE:
                ratio = MAX_EDGE / max(image.size)
                image = image.resize((int(width * ratio), int(height * ratio)))
                width, height = image.size
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=82)
            encoded = buffer.getvalue()
    except Exception:
        # Pillow is optional: fall back to the raw PNG.
        width, height = _png_size(raw)
    b64 = base64.b64encode(encoded).decode("ascii")
    mime = "image/jpeg" if encoded is not raw else "image/png"
    return {
        "path": path,
        "base64": b64,
        "data_url": f"data:{mime};base64,{b64}",
        "width": width,
        "height": height,
        "bytes": len(encoded),
    }


def _png_size(data: bytes) -> tuple[int, int]:
    if len(data) > 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return 0, 0


def screen_tools(deps) -> list[Tool]:
    tools: list[Tool] = [CaptureScreenTool(deps), AnalyseScreenTool(deps)]
    if deps.config.capabilities.screen_awareness:
        tools.append(WatchScreenTool(deps))
    return tools
