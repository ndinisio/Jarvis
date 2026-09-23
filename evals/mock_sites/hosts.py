"""Which real hostnames the evaluation browser serves from the mock sites."""

from __future__ import annotations

from urllib.parse import urlsplit

#: hostname → mock path prefix (see ``app.py``).
HOSTS: dict[str, str] = {
    "www.amazon.co.uk": "amazon", "amazon.co.uk": "amazon",
    "www.amazon.com": "amazon", "amazon.com": "amazon",
    "mail.example.com": "mail",
    "events.example.com": "events",
    "tasks.example.com": "tasks",
    "duckduckgo.com": "search", "www.duckduckgo.com": "search", "html.duckduckgo.com": "search",
    "www.google.com": "search", "google.com": "search", "www.google.co.uk": "search",
    "www.bing.com": "search", "bing.com": "search",
}

#: Where each mock site lives, for tasks that start from a known page.
HOME: dict[str, str] = {
    "amazon": "https://www.amazon.co.uk/",
    "mail": "https://mail.example.com/",
    "events": "https://events.example.com/",
    "tasks": "https://tasks.example.com/",
    "search": "https://duckduckgo.com/",
}


def mock_url(url: str, server: str) -> str | None:
    """Translate a real URL into the mock server URL that serves it, or
    ``None`` when the host isn't one the mock covers (the evaluation browser
    blocks those — tasks never need the real internet)."""
    parts = urlsplit(url)
    prefix = HOSTS.get((parts.hostname or "").lower())
    if prefix is None:
        return None
    path = parts.path or "/"
    query = f"?{parts.query}" if parts.query else ""
    return f"{server.rstrip('/')}/{prefix}{path}{query}"
