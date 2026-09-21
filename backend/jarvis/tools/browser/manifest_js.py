"""JavaScript payloads for the grounded web-page interaction primitives.

Kept out of ``tools.py`` so driver logic isn't buried in embedded
JavaScript. Every script here is one JS expression — an immediately-invoked
function, every statement explicitly semicolon-terminated — so it stays
correct if flattened onto a single line, which is exactly what happens when
it's embedded into an AppleScript ``do JavaScript``/``execute ... javascript``
string literal (AppleScript's own newline handling is not something this
code relies on).

**The grounding mechanism.** :func:`build_manifest_script` walks the DOM and
stamps each interactive element it finds with a ``data-jarvis-id`` attribute
at the moment the manifest is read, then reports that id back as the
element's ``handle``. A later action script re-locates the element with
``document.querySelector('[data-jarvis-id="<handle>"]')`` — one shared
artifact between the read step and the act step, instead of re-deriving
intent from prose each time the way the screenshot-to-label pipeline does.

**Dynamic values** (a handle, fill text) are always embedded via
``json.dumps()``, never string concatenation — a stray quote in a scraped
product title must not be able to break out of the script.
"""

from __future__ import annotations

import json

#: Elements a manifest read considers "interactive".
_SELECTOR = "a,button,input,select,textarea,[role],[onclick],[contenteditable=true]"

#: Shared by every script that needs to classify an element's role the same
#: way the manifest did, so a handle collected from one read resolves to the
#: kind of control an action script expects.
_CLASSIFY_ROLE = """
function jarvisRole(el) {
  var tag = el.tagName.toLowerCase();
  var role = (el.getAttribute('role') || '').toLowerCase();
  var type = (el.getAttribute('type') || '').toLowerCase();
  if (role) return role;
  if (tag === 'a') return 'link';
  if (tag === 'select') return 'select';
  if (tag === 'textarea') return 'field';
  if (tag === 'button') return 'button';
  if (tag === 'input') {
    if (type === 'submit' || type === 'button' || type === 'reset' || type === 'image') return 'button';
    if (type === 'checkbox') return 'checkbox';
    if (type === 'radio') return 'radio';
    return 'field';
  }
  return 'control';
}
"""

_VISIBLE_CHECK = """
function jarvisVisible(el) {
  if (el.disabled) return false;
  var style = window.getComputedStyle(el);
  if (style.visibility === 'hidden' || style.display === 'none') return false;
  var rect = el.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}
"""

_LOCATE = """
function jarvisFind(handle) {
  return document.querySelector('[data-jarvis-id="' + handle + '"]');
}
"""

_MANIFEST_TEMPLATE = """(function(){
%(classify)s
%(visible)s
var roleFilter = %(role_filter)s;
var limit = %(limit)s;
if (typeof window.__jarvisSeq !== 'number') { window.__jarvisSeq = 0; }
var nodes = document.querySelectorAll(%(selector)s);
var out = [];
for (var i = 0; i < nodes.length && out.length < limit; i++) {
  var el = nodes[i];
  if (!jarvisVisible(el)) continue;
  var role = jarvisRole(el);
  if (roleFilter && roleFilter.indexOf(role) === -1) continue;
  var handle = el.getAttribute('data-jarvis-id');
  if (!handle) {
    window.__jarvisSeq += 1;
    handle = 'jv' + window.__jarvisSeq;
    el.setAttribute('data-jarvis-id', handle);
  }
  var rect = el.getBoundingClientRect();
  out.push({
    handle: handle,
    role: role,
    tag: el.tagName.toLowerCase(),
    text: (el.innerText || el.value || el.placeholder || '').trim().slice(0, 200),
    name: el.getAttribute('aria-label') || el.getAttribute('name') || '',
    type: el.getAttribute('type') || '',
    href: el.tagName.toLowerCase() === 'a' ? (el.href || '') : '',
    value: (el.value !== undefined ? String(el.value) : '').slice(0, 500),
    rect: {x: Math.round(rect.left), y: Math.round(rect.top),
           w: Math.round(rect.width), h: Math.round(rect.height)}
  });
}
return JSON.stringify({elements: out, url: window.location.href, title: document.title});
})()"""

_CLICK_TEMPLATE = """(function(){
%(locate)s
var el = jarvisFind(%(handle)s);
if (!el) return JSON.stringify({ok: false, reason: 'stale handle — the page has changed since it was read'});
el.scrollIntoView({block: 'center'});
el.click();
return JSON.stringify({ok: true, url: window.location.href, title: document.title});
})()"""

_FILL_TEMPLATE = """(function(){
%(locate)s
var el = jarvisFind(%(handle)s);
if (!el) return JSON.stringify({ok: false, reason: 'stale handle — the page has changed since it was read'});
el.scrollIntoView({block: 'center'});
el.focus();
var tag = el.tagName.toLowerCase();
var proto = null;
if (tag === 'textarea') { proto = window.HTMLTextAreaElement.prototype; }
else if (tag === 'input') { proto = window.HTMLInputElement.prototype; }
var applied = false;
if (proto) {
  var setter = Object.getOwnPropertyDescriptor(proto, 'value');
  if (setter && setter.set) {
    try { setter.set.call(el, %(text)s); applied = true; } catch (e) {}
  }
}
if (!applied) { el.value = %(text)s; }
el.dispatchEvent(new Event('input', {bubbles: true}));
el.dispatchEvent(new Event('change', {bubbles: true}));
var submitted = false;
if (%(submit)s) {
  var form = el.form;
  if (form) {
    if (typeof form.requestSubmit === 'function') { form.requestSubmit(); }
    else { form.submit(); }
    submitted = true;
  } else {
    el.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
    el.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', bubbles: true}));
  }
}
return JSON.stringify({ok: true, submitted: submitted, url: window.location.href, title: document.title});
})()"""

_SUBMIT_TEMPLATE = """(function(){
%(locate)s
var el = jarvisFind(%(handle)s);
if (!el) return JSON.stringify({ok: false, reason: 'stale handle — the page has changed since it was read'});
var form = el.tagName.toLowerCase() === 'form' ? el : el.form;
if (!form) {
  el.click();
  return JSON.stringify({ok: true, url: window.location.href, title: document.title});
}
if (typeof form.requestSubmit === 'function') { form.requestSubmit(); }
else { form.submit(); }
return JSON.stringify({ok: true, url: window.location.href, title: document.title});
})()"""


def build_manifest_script(*, limit: int = 60, roles: list[str] | None = None) -> str:
    """A script that stamps and reports the page's interactive elements.

    Its result is JSON *text* (a JS string, not a JS object) — that is what
    survives the AppleScript round trip losslessly; callers parse it with
    :func:`json.loads`.
    """
    role_filter = json.dumps(list(roles)) if roles else "null"
    bounded_limit = max(1, min(int(limit), 200))
    return _MANIFEST_TEMPLATE % {
        "classify": _CLASSIFY_ROLE,
        "visible": _VISIBLE_CHECK,
        "role_filter": role_filter,
        "limit": bounded_limit,
        "selector": json.dumps(_SELECTOR),
    }


def build_click_script(handle: str) -> str:
    return _CLICK_TEMPLATE % {"locate": _LOCATE, "handle": json.dumps(str(handle))}


def build_fill_script(handle: str, text: str, submit: bool) -> str:
    return _FILL_TEMPLATE % {
        "locate": _LOCATE,
        "handle": json.dumps(str(handle)),
        "text": json.dumps(str(text)),
        "submit": "true" if submit else "false",
    }


def build_submit_script(handle: str) -> str:
    return _SUBMIT_TEMPLATE % {"locate": _LOCATE, "handle": json.dumps(str(handle))}
