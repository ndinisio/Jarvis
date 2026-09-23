"""Run the mock sites on a local port in a background thread."""

from __future__ import annotations

import socket
import threading
import time

import httpx
import uvicorn

from .app import create_app


class MockServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self.host = host
        self.port = port or _free_port(host)
        self.app = create_app()
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> MockServer:
        config = uvicorn.Config(self.app, host=self.host, port=self.port, log_level="warning",
                                access_log=False, lifespan="off")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, name="mock-sites", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{self.url}/__health", timeout=0.5).status_code == 200:
                    return self
            except httpx.HTTPError:
                time.sleep(0.05)
        raise RuntimeError("the mock sites didn't start")

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    # -- ground truth ----------------------------------------------------------
    def reset(self, setup: dict | None = None) -> None:
        httpx.post(f"{self.url}/__reset", json=setup or {}, timeout=5.0).raise_for_status()

    def state(self) -> dict:
        return httpx.get(f"{self.url}/__state", timeout=5.0).json()

    def __enter__(self) -> MockServer:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


def _free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]
