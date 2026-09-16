"""Command line entry point.

    jarvis                 start the assistant and open the interface
    jarvis serve           the same, without opening a browser
    jarvis ask "…"         one-shot question (useful for scripting and tests)
    jarvis doctor          check models, voice, permissions and the workspace
    jarvis models          list what each model slot resolves to
    jarvis config          print the effective configuration
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import threading
import time
import webbrowser

from .core.app import JarvisApp
from .core.config import create_store, load_config
from .core.logging import setup_logging

BANNER = """
     ██  █████  ██████  ██    ██ ██ ███████
     ██ ██   ██ ██   ██ ██    ██ ██ ██
     ██ ███████ ██████  ██    ██ ██ ███████
██   ██ ██   ██ ██   ██  ██  ██  ██      ██
 █████  ██   ██ ██   ██   ████   ██ ███████
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis", description="JARVIS — local AI for macOS")
    parser.add_argument("--config", help="path to a config file", default=None)
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the assistant (default)")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--no-browser", action="store_true")
    serve.add_argument("--no-voice", action="store_true")
    serve.add_argument("--dev", action="store_true", help="enable developer mode")

    ask = sub.add_parser("ask", help="ask a single question and print the answer")
    ask.add_argument("text", nargs="+")
    ask.add_argument("--json", action="store_true")

    sub.add_parser("doctor", help="check the installation")
    sub.add_parser("models", help="show model availability")
    sub.add_parser("config", help="print the effective configuration")

    args = parser.parse_args(argv)
    command = args.command or "serve"

    if command == "serve":
        return _serve(args)
    if command == "ask":
        return asyncio.run(_ask(" ".join(args.text), args.json, args.config))
    if command == "doctor":
        return asyncio.run(_doctor(args.config))
    if command == "models":
        return asyncio.run(_models(args.config))
    if command == "config":
        config = load_config()
        print(json.dumps(config.model_dump(), indent=2, default=str))
        return 0
    parser.print_help()
    return 1


def _serve(args) -> int:
    from pathlib import Path

    import uvicorn

    store = create_store(Path(args.config) if args.config else None)
    config = store.current
    if args.dev:
        store.update({"ui": {"developer_mode": True}})
        config = store.current
    host = args.host or config.server.host
    port = args.port or config.server.port

    setup_logging(config.log_level, config.logs_dir)
    print(BANNER)
    print(f"  Workspace : {config.workspace_path}")
    print(f"  Interface : http://{host}:{port}")
    print(f"  Models    : fast={config.models.fast.model}  general={config.models.general.model}")
    print(f"  Voice     : wake “{config.voice.wake_word}”, {config.voice.tts_engine} speech\n")

    from .server import FRONTEND_DIST, create_app

    if not FRONTEND_DIST.exists():
        print("  ⚠  The interface isn't built. Run: cd frontend && npm install && npm run build\n")

    jarvis = JarvisApp(store, enable_voice=not args.no_voice)
    app = create_app(jarvis)

    if config.server.open_browser and not args.no_browser:
        def _open() -> None:
            time.sleep(1.5)
            with contextlib.suppress(Exception):
                webbrowser.open(f"http://{host}:{port}")

        threading.Thread(target=_open, daemon=True).start()

    uvicorn.run(app, host=host, port=port, log_level=config.log_level.lower(),
                access_log=False)
    return 0


async def _ask(text: str, as_json: bool, config_path: str | None) -> int:
    from pathlib import Path

    store = create_store(Path(config_path) if config_path else None)
    jarvis = JarvisApp(store, enable_voice=False)
    await jarvis.startup()
    try:
        result = await jarvis.ask(text)
        if result.task_id:
            # Wait for the background task so a one-shot invocation is useful.
            task = jarvis.tasks.get(result.task_id)
            while task and task.cancellable:
                await asyncio.sleep(0.25)
        if as_json:
            print(json.dumps(
                {"text": result.text, "route": result.decision.as_dict(),
                 "duration_ms": round(result.duration_ms, 1)}, indent=2))
        else:
            print(result.text)
            print(f"\n  [{result.decision.kind}:{result.decision.name} via "
                  f"{result.decision.path}, {result.duration_ms:.0f} ms]", file=sys.stderr)
    finally:
        await jarvis.shutdown()
    return 0


async def _doctor(config_path: str | None) -> int:
    import platform
    from pathlib import Path

    store = create_store(Path(config_path) if config_path else None)
    jarvis = JarvisApp(store, enable_voice=True)
    status = await jarvis.status()

    print(BANNER)
    ok = True

    print("Platform")
    print(f"  {platform.platform()}")
    if not status["platform"]["is_macos"]:
        print("  ⚠  Not macOS — system, mail, calendar and screen tools will be limited.")
    print()

    print("Workspace")
    print(f"  {status['workspace']}  ({'exists' if jarvis.config.workspace_path.exists() else 'missing'})")
    print()

    print("Models")
    for name, provider in status["models"]["providers"].items():
        mark = "✓" if provider["available"] else "✗"
        print(f"  {mark} {name:<10} {provider['base_url']}")
        if provider["available"] and provider["models"]:
            print(f"      {len(provider['models'])} models: {', '.join(provider['models'][:6])}")
    for slot, info in status["models"]["slots"].items():
        if info.get("ready"):
            substituted = " (substituted)" if info.get("substituted") else ""
            print(f"  ✓ {slot:<8} → {info['resolved']}{substituted}")
        else:
            ok = False
            print(f"  ✗ {slot:<8} → unavailable: {info.get('reason', '')}")
    print()

    print("Voice")
    voice = status["voice"]
    for part in ("microphone", "stt", "tts", "wake"):
        info = voice.get(part) or {}
        mark = "✓" if info.get("ok") else "✗"
        print(f"  {mark} {part:<11} {info.get('engine', '')} — {info.get('note', '')}")
    print()

    if status["platform"]["is_macos"]:
        print("macOS permissions")
        report = await jarvis.permission_report()
        for entry in report["permissions"]:
            mark = "✓" if entry["granted"] else "✗"
            print(f"  {mark} {entry['label']:<16} {entry['why']}")
        print()

    print(f"Tools: {sum(len(v) for v in status['tools'].values())} registered across "
          f"{len(status['tools'])} categories")
    print(f"Capabilities: {', '.join(status['capabilities'])}")
    await jarvis.shutdown()
    return 0 if ok else 1


async def _models(config_path: str | None) -> int:
    from pathlib import Path

    store = create_store(Path(config_path) if config_path else None)
    jarvis = JarvisApp(store, enable_voice=False)
    status = await jarvis.models.status()
    print(json.dumps(status, indent=2))
    await jarvis.shutdown()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
