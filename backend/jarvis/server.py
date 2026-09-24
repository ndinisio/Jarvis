"""HTTP + WebSocket server.

The UI is a pure projection of the event bus: it subscribes over a WebSocket,
receives every state change as it happens, and sends back a small set of
commands. REST endpoints exist for the things that aren't event-shaped —
configuration, status, one-shot audio transcription.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .core import auth
from .core.app import JarvisApp
from .core.events import EventType
from .core.logging import get_logger

log = get_logger("jarvis.server")

FRONTEND_DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"

#: API routes reachable without the session token: the health check a
#: launcher polls before it has anything to authenticate with.
PUBLIC_API_PATHS = {"/api/health"}

#: The Vite development server (``scripts/dev.sh``), which serves the
#: interface on its own port and proxies /api and /ws here. Trusted for CORS
#: and as a WebSocket Origin — the session token is still required.
DEV_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")


def create_app(jarvis: JarvisApp | None = None, *, host: str | None = None,
               port: int | None = None) -> FastAPI:
    jarvis = jarvis or JarvisApp()
    # The address this server will actually be reached at — what a
    # legitimate browser Origin header must name. Callers that bind
    # somewhere other than the configured default (``jarvis serve --port``)
    # pass it through; otherwise the configured one is right.
    host = host or jarvis.config.server.host
    port = port or jarvis.config.server.port
    token = auth.session_token()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        auth.redact_server_logs()  # uvicorn's logging is configured by now
        await jarvis.startup()
        try:
            yield
        finally:
            await jarvis.shutdown()

    app = FastAPI(title="JARVIS", version=__version__, lifespan=lifespan, docs_url="/api/docs")
    app.state.jarvis = jarvis
    app.state.session_token = token

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(DEV_ORIGINS),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def require_session_token(request: Request, call_next):
        # Every API call needs the token — reads included: /api/memory is
        # your whole conversation history. The interface itself (/, assets)
        # stays open: it has to load before it can know the token.
        path = request.url.path
        # A CORS preflight never carries custom headers; the request it
        # clears the way for still has to bring the token.
        if request.method != "OPTIONS" and path.startswith("/api/") and path not in PUBLIC_API_PATHS:
            given = (request.headers.get(auth.TOKEN_HEADER)
                     or request.query_params.get(auth.TOKEN_PARAM))
            if not auth.tokens_match(given, token):
                return JSONResponse({"error": "missing or invalid session token"}, status_code=401)
        return await call_next(request)

    # ------------------------------------------------------------------
    # WebSocket: the live event stream
    # ------------------------------------------------------------------
    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        # Checked before accepting, so a refused client never receives a
        # single event — not even the status handshake.
        if not auth.tokens_match(websocket.query_params.get(auth.TOKEN_PARAM), token):
            log.warning("refused a WebSocket connection without a valid session token")
            await websocket.close(code=1008)
            return
        origin = websocket.headers.get("origin")
        if not auth.origin_allowed(origin, host=host, port=port, trusted=DEV_ORIGINS):
            log.warning("refused a WebSocket connection from %s", origin)
            await websocket.close(code=1008)
            return
        await websocket.accept()
        log.info("UI connected (%d total)", jarvis.bus.subscriber_count + 1)

        # The handshake always lands first, so the UI knows what it is talking
        # to before any replayed events arrive.
        status = await jarvis.status()
        await websocket.send_json({"type": EventType.HELLO, **status})

        subscription = jarvis.bus.subscribe(replay=20)

        async def pump() -> None:
            try:
                async for event in subscription:
                    await websocket.send_json(event.as_dict())
            except (WebSocketDisconnect, RuntimeError):
                pass

        pump_task = asyncio.create_task(pump())

        try:
            while True:
                message = await websocket.receive_json()
                await _handle_command(jarvis, message, websocket)
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            log.debug("socket closed: %s", exc)
        finally:
            subscription.close()
            pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump_task
            log.info("UI disconnected")

    # ------------------------------------------------------------------
    # REST
    # ------------------------------------------------------------------
    @app.get("/api/session")
    async def session() -> dict[str, Any]:
        # Reaching this at all means the token was accepted (the middleware
        # answers 401 otherwise): how the interface tells "JARVIS is down"
        # from "this tab belongs to an earlier run".
        return {"ok": True}

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        return await jarvis.status()

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        # Unauthenticated (a launcher polls it before it has the token), so
        # it says only that this is JARVIS and which version — nothing about
        # the user or the machine.
        return {"ok": True, "app": "jarvis", "version": __version__}

    @app.get("/api/config")
    async def get_config() -> dict[str, Any]:
        from .core.app import _public_config

        return _public_config(jarvis.config)

    @app.patch("/api/config")
    async def patch_config(request: Request) -> dict[str, Any]:
        from .core.app import _public_config

        patch = await request.json()
        config = jarvis.config_store.update(patch)
        return _public_config(config)

    @app.get("/api/tools")
    async def tools() -> dict[str, Any]:
        return {"tools": jarvis.deps.registry.specs(),
                "categories": jarvis.deps.registry.by_category()}

    @app.get("/api/tasks")
    async def tasks() -> dict[str, Any]:
        return {"tasks": jarvis.tasks.snapshot()}

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str) -> dict[str, Any]:
        return {"cancelled": jarvis.tasks.cancel(task_id)}

    @app.post("/api/tasks/{task_id}/pause")
    async def pause_task(task_id: str) -> dict[str, Any]:
        return {"paused": jarvis.tasks.pause(task_id)}

    @app.post("/api/tasks/{task_id}/resume")
    async def resume_task(task_id: str) -> dict[str, Any]:
        return {"resumed": jarvis.tasks.resume(task_id)}

    @app.post("/api/tasks/{task_id}/take-over")
    async def take_over_task(task_id: str) -> dict[str, Any]:
        return {"taken_over": await jarvis.orchestrator.take_over(task_id)}

    @app.get("/api/audit")
    async def audit_recent() -> dict[str, Any]:
        """The latest audit records (security/audit.py)."""
        return {"records": jarvis.deps.audit.recent() if jarvis.deps.audit else []}

    @app.get("/api/audit/{key}")
    async def audit_entries(key: str) -> dict[str, Any]:
        """Every action taken for one task (or one request outside a task)."""
        return {"key": key, "entries": jarvis.deps.audit.entries(key) if jarvis.deps.audit else []}

    @app.get("/api/telemetry")
    async def telemetry() -> dict[str, Any]:
        return {"summary": jarvis.telemetry.summary(), "recent": jarvis.telemetry.recent(),
                "requests": jarvis.telemetry.requests()}

    @app.get("/api/memory")
    async def memory() -> dict[str, Any]:
        return jarvis.memory.snapshot()

    @app.delete("/api/memory/facts/{fact_id}")
    async def delete_fact(fact_id: int) -> dict[str, Any]:
        return {"deleted": await jarvis.memory.forget_fact(fact_id)}

    @app.delete("/api/memory/conversation")
    async def clear_conversation() -> dict[str, Any]:
        return {"deleted": await jarvis.memory.clear_conversation()}

    @app.get("/api/skills")
    async def skills() -> dict[str, Any]:
        library = jarvis.deps.skills
        return {"enabled": library is not None, "skills": library.listing() if library else []}

    @app.delete("/api/skills/{skill_id}")
    async def forget_skill(skill_id: str) -> dict[str, Any]:
        library = jarvis.deps.skills
        return {"forgotten": bool(library and library.forget(skill_id))}

    @app.get("/api/permissions")
    async def permissions() -> dict[str, Any]:
        return await jarvis.permission_report()

    @app.post("/api/permissions/open")
    async def open_permission_pane(request: Request) -> dict[str, Any]:
        body = await request.json()
        panes = {
            "microphone": "Privacy_Microphone",
            "screen_recording": "Privacy_ScreenCapture",
            "accessibility": "Privacy_Accessibility",
            "automation": "Privacy_Automation",
            "mail": "Privacy_Automation",
            "calendar": "Privacy_Calendars",
        }
        jarvis.controller.open_privacy_settings(panes.get(body.get("kind", ""), "Privacy_Microphone"))
        return {"opened": True}

    @app.get("/api/models")
    async def models() -> dict[str, Any]:
        return await jarvis.models.status()

    @app.post("/api/ask")
    async def ask(request: Request) -> dict[str, Any]:
        body = await request.json()
        result = await jarvis.ask(body.get("text", ""), body.get("source", "text"))
        return {
            "text": result.text, "spoken": result.spoken, "task_id": result.task_id,
            "route": result.decision.as_dict(), "duration_ms": result.duration_ms,
            "error": result.error,
        }

    @app.post("/api/voice/transcribe")
    async def transcribe(request: Request) -> dict[str, Any]:
        """Transcribe audio captured by the browser (fallback microphone path)."""
        if jarvis.voice is None:
            return JSONResponse({"error": "voice is disabled"}, status_code=400)
        body = await request.json()
        raw = body.get("audio", "")
        sample_rate = int(body.get("sample_rate", 16000))
        try:
            data = base64.b64decode(raw)
        except Exception:
            return JSONResponse({"error": "invalid audio payload"}, status_code=400)
        text = await jarvis.voice.transcribe_audio(data, sample_rate)
        if text and body.get("dispatch", True):
            asyncio.create_task(jarvis.ask(text, source="voice"))
        return {"text": text}

    @app.post("/api/voice/speak")
    async def speak(request: Request) -> dict[str, Any]:
        body = await request.json()
        if jarvis.voice is None:
            return {"spoken": False}
        return {"spoken": await jarvis.voice.speak(body.get("text", ""))}

    @app.get("/api/voice/voices")
    async def voices() -> dict[str, Any]:
        if jarvis.voice is None:
            return {"voices": []}
        return {"voices": await jarvis.voice.tts.voices()}

    # ------------------------------------------------------------------
    # static frontend
    # ------------------------------------------------------------------
    if FRONTEND_DIST.exists():
        app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(FRONTEND_DIST / "index.html")

        @app.get("/{path:path}")
        async def spa(path: str) -> FileResponse:
            candidate = FRONTEND_DIST / path
            if candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(FRONTEND_DIST / "index.html")
    else:

        @app.get("/")
        async def missing_ui() -> JSONResponse:
            return JSONResponse(
                {
                    "error": "The interface hasn't been built yet.",
                    "fix": "Run `npm install && npm run build` in the frontend directory, "
                           "or `./scripts/dev.sh` for the development server.",
                },
                status_code=503,
            )

    return app


async def _handle_command(jarvis: JarvisApp, message: dict[str, Any],
                          websocket: WebSocket) -> None:
    """Commands the UI can send. Deliberately a short list."""
    kind = message.get("type", "")

    if kind == "utterance":
        text = (message.get("text") or "").strip()
        if text:
            asyncio.create_task(jarvis.ask(text, message.get("source", "text")))

    elif kind == "cancel":
        task_id = message.get("task_id")
        if task_id:
            jarvis.tasks.cancel(task_id)
        else:
            jarvis.tasks.cancel_latest()
        if jarvis.voice is not None:
            await jarvis.voice.stop_speaking()

    elif kind == "pause" and message.get("task_id"):
        jarvis.tasks.pause(str(message["task_id"]))

    elif kind == "resume" and message.get("task_id"):
        jarvis.tasks.resume(str(message["task_id"]))

    elif kind == "take_over" and message.get("task_id"):
        await jarvis.orchestrator.take_over(str(message["task_id"]))

    elif kind == "confirm.response":
        jarvis.permissions.resolve(
            message.get("id", ""), bool(message.get("approved")), bool(message.get("remember"))
        )

    elif kind == "voice.start":
        if jarvis.voice is not None:
            await jarvis.voice.start()

    elif kind == "voice.stop":
        if jarvis.voice is not None:
            await jarvis.voice.stop()

    elif kind == "voice.push_to_talk":
        # The browser captured audio; transcribe and route it.
        if jarvis.voice is not None and message.get("audio"):
            data = base64.b64decode(message["audio"])
            text = await jarvis.voice.transcribe_audio(data, int(message.get("sample_rate", 16000)))
            if text:
                asyncio.create_task(jarvis.ask(text, source="voice"))

    elif kind == "speech.finished":
        # The browser's speech synthesis reported completion.
        engine = getattr(jarvis.voice, "tts", None) if jarvis.voice else None
        if engine is not None and hasattr(engine, "notify_finished"):
            engine.notify_finished()

    elif kind == "config.update":
        jarvis.config_store.update(message.get("patch", {}))

    elif kind == "status":
        await websocket.send_json({"type": EventType.HELLO, **(await jarvis.status())})

    elif kind == "ping":
        await websocket.send_json({"type": "pong", "ts": message.get("ts")})
