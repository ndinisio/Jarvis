"""Screen capture and visual understanding.

Capture is strictly on demand. There is no polling loop, no background
screenshot timer and no silent upload: a capture happens only because a tool
call asked for one, the image is written into the workspace, and the same image
is shown in the UI so the user sees exactly what JARVIS saw.
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
        prompt = (
            "You are looking at a screenshot of the user's Mac. Answer precisely and briefly. "
            "Read any visible text exactly. If the question can't be answered from the image, "
            "say so plainly.\n\nQuestion: " + question
        )
        messages = [
            ChatMessage("system", "You describe screenshots factually and concisely."),
            ChatMessage("user", prompt, images=[capture["base64"]]),
        ]
        try:
            completion = await self._deps.models.complete(Slot.VISION, messages)
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
    return [CaptureScreenTool(deps), AnalyseScreenTool(deps)]
