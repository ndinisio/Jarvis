"""The control channel, attacked for real: a running JARVIS on a real port,
and the connections a web page or another program on this Mac could try.

    PYTHONPATH=backend python -m evals.run_security

JARVIS's event stream can answer a pending confirmation — the gate between
JARVIS and a purchase, a send or a delete — so anything but its own page must
be turned away (backend/jarvis/core/auth.py). The unit tests check this with
a test client; this runs the actual server and connects to it over TCP, the
way an attacker would. Every attempt must be refused, and the page's own
connection must still work. Results go to ``evals/results/security-*.json``
for the gate report.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
import tempfile
import time
from pathlib import Path

from .suites import ROOT


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def _ws(url: str, origin: str | None) -> bool:
    """Did the handshake succeed and the stream stay open long enough to
    send a command?"""
    import websockets

    headers = {"Origin": origin} if origin else {}
    try:
        async with websockets.connect(url, additional_headers=headers, open_timeout=5) as stream:
            await stream.send(json.dumps({"type": "confirm.response", "id": "x", "approved": True}))
            await asyncio.wait_for(stream.recv(), timeout=3)
            return True
    except Exception:
        return False


async def run() -> list[dict]:
    import httpx
    import uvicorn
    from jarvis.core.app import JarvisApp
    from jarvis.core.config import Config, ConfigStore
    from jarvis.server import create_app

    workspace = Path(tempfile.mkdtemp(prefix="jarvis-security-"))
    config = Config(workspace=str(workspace / "JARVIS"))
    config.voice.enabled = False
    config.voice.tts_engine = "off"
    config.ensure_workspace()
    jarvis = JarvisApp(ConfigStore(config, workspace / "config.json"), enable_voice=False)
    port = _free_port()
    api = create_app(jarvis, host="127.0.0.1", port=port)
    token = api.state.session_token
    server = uvicorn.Server(uvicorn.Config(api, host="127.0.0.1", port=port, log_level="warning",
                                           lifespan="off"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)

    base, ws = f"http://127.0.0.1:{port}", f"ws://127.0.0.1:{port}/ws"
    own = f"http://127.0.0.1:{port}"
    results: list[dict] = []

    def check(name: str, refused: bool, expected_refused: bool = True) -> None:
        results.append({"id": name, "ok": refused == expected_refused, "refused": refused})

    try:
        check("event stream, no token", not await _ws(ws, own))
        check("event stream, wrong token", not await _ws(f"{ws}?token={'x' * 43}", own))
        check("event stream, right token from another site",
              not await _ws(f"{ws}?token={token}", "https://evil.example"))
        check("event stream, its own page", not await _ws(f"{ws}?token={token}", own),
              expected_refused=False)
        async with httpx.AsyncClient(base_url=base, timeout=5) as client:
            check("settings without the token", (await client.get("/api/config")).status_code in {401, 403})
            check("changing settings without the token",
                  (await client.patch("/api/config", json={"security": {"autonomy": "consequential_only"}}))
                  .status_code in {401, 403})
            check("audit trail without the token", (await client.get("/api/audit")).status_code in {401, 403})
            check("health check is public", (await client.get("/api/health")).status_code != 200,
                  expected_refused=False)
            check("settings with the token",
                  (await client.get("/api/config", headers={"X-Jarvis-Token": token})).status_code != 200,
                  expected_refused=False)
    finally:
        server.should_exit = True
        await serving
        await jarvis.shutdown()
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)
    started = time.time()
    results = asyncio.run(run())
    for result in results:
        print(f"{'PASS' if result['ok'] else 'FAIL'}  {result['id']}")
    passed = sum(r["ok"] for r in results)
    summary = {"total": len(results), "passed": passed,
               "success_rate": round(passed / len(results), 3) if results else 0.0}
    print(json.dumps(summary))
    out = Path(args.out) if args.out else ROOT / "results" / f"security-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"suite": "security", "model": "real", "started": started,
                               "summary": summary, "results": results}, indent=2), encoding="utf-8")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
