"""Logging setup: readable console output plus a rolling file in the workspace."""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

_CONFIGURED = False


class _Formatter(logging.Formatter):
    COLORS = {
        "DEBUG": "\033[38;5;245m",
        "INFO": "\033[38;5;39m",
        "WARNING": "\033[38;5;214m",
        "ERROR": "\033[38;5;203m",
        "CRITICAL": "\033[48;5;203m",
    }
    RESET = "\033[0m"

    def __init__(self, color: bool = True):
        super().__init__("%(asctime)s %(levelname)-7s %(name)-24s %(message)s", "%H:%M:%S")
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if self.color:
            prefix = self.COLORS.get(record.levelname, "")
            if prefix:
                return f"{prefix}{text}{self.RESET}"
        return text


def setup_logging(level: str = "INFO", log_dir: Path | None = None) -> None:
    global _CONFIGURED
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    if not _CONFIGURED:
        console = logging.StreamHandler()
        console.setFormatter(_Formatter())
        root.addHandler(console)
        _CONFIGURED = True

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "jarvis.log"
        if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
            file_handler = logging.handlers.RotatingFileHandler(
                path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
            )
            file_handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s")
            )
            root.addHandler(file_handler)

    for noisy in ("httpx", "httpcore", "urllib3", "websockets", "multipart"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
