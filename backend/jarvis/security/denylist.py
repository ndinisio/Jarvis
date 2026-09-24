"""Places JARVIS never reads or operates, whatever it's asked.

Some things are the user's alone: password managers and the Keychain,
banking and payment sites, and the parts of System Settings that grant
permissions (including to JARVIS itself). JARVIS may *open* them when asked —
"open 1Password", "go to my bank's website" — but it doesn't read what they
show or press anything in them: it says the rest is theirs, and stops.

Enforced where the surfaces are reached, not by the model's good sense: the
native surface refuses a blocked app or window before reading or acting, the
page tools refuse a blocked site, and the registry refuses any app action
naming a blocked app. All three lists are in ``security`` settings (the
defaults are in ``core/config.py``).
"""

from __future__ import annotations

from urllib.parse import urlparse

from ..core.errors import PermissionDenied


class Refused(PermissionDenied):
    code = "refused"


def app_refusal(config, app: str, window: str = "") -> str:
    """Why JARVIS won't read or operate *app* (showing *window*), or ""."""
    security = getattr(config, "security", None)
    name = (app or "").strip()
    if security is None or not name:
        return ""
    lowered = name.lower()
    for blocked in security.blocked_apps:
        entry = blocked.strip().lower()
        if entry and (lowered == entry or lowered.startswith(entry + " ")):
            return (f"{name} is one of the apps JARVIS never reads or operates (security.blocked_apps) — "
                    "it's the user's to use. Tell the user; don't try another way in.")
    title = (window or "").lower()
    for blocked in security.blocked_windows:
        owner, _, part = blocked.partition(":")
        if owner.strip().lower() == lowered and part.strip() and part.strip().lower() in title:
            return (f"{name}'s “{part.strip()}” is somewhere JARVIS never operates (security.blocked_windows) "
                    "— it's the user's to change. Tell the user; don't try another way in.")
    return ""


def site_refusal(config, url: str) -> str:
    """Why JARVIS won't read or operate the page at *url*, or ""."""
    security = getattr(config, "security", None)
    if security is None or not url:
        return ""
    parsed = urlparse(url if "//" in url else f"//{url}")
    host = (parsed.hostname or "").lower()
    where = host + (parsed.path or "")
    if not host:
        return ""
    for blocked in security.blocked_sites:
        entry = blocked.strip().lower()
        if not entry:
            continue
        if "." not in entry:
            hit = entry in host
        elif "/" in entry:
            hit = where.startswith(entry) or where.split(".", 1)[-1].startswith(entry)
        else:
            hit = host == entry or host.endswith("." + entry)
        if hit:
            return (f"{host} is a site JARVIS never reads or operates (security.blocked_sites) — "
                    "banking, payments and passwords are the user's. Tell the user it's open for them; "
                    "don't try another way in.")
    return ""


def refuse_app(config, app: str, window: str = "") -> None:
    reason = app_refusal(config, app, window)
    if reason:
        raise Refused(reason, detail="denylist")


def refuse_site(config, url: str) -> None:
    reason = site_refusal(config, url)
    if reason:
        raise Refused(reason, detail="denylist")
