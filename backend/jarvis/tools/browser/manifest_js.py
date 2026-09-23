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

**What a model needs to see first.** A real page's chrome — a long header
of department links, a footer — comes before its content in document order.
The manifest therefore ranks elements: the page's main content inside the
viewport first, then header/navigation controls (with form fields such as a
search box promoted), then content below the fold, then the footer. The
limit applies after ranking, so "Add to Basket" is never cut off by 45
department links.

**Dynamic values** (a handle, fill text) are always embedded via
``json.dumps()``, never string concatenation — a stray quote in a scraped
product title must not be able to break out of the script.
"""

from __future__ import annotations

import json

#: Elements a manifest read considers "interactive".
_SELECTOR = ("a[href],button,input:not([type=hidden]),select,textarea,summary,"
             "[role=button],[role=link],[role=checkbox],[role=radio],[role=tab],[role=option],"
             "[role=menuitem],[role=combobox],[role=switch],[role=textbox],[role=searchbox],"
             "[onclick],[contenteditable=true],[tabindex]:not([tabindex='-1'])")

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
  if (tag === 'button' || tag === 'summary') return 'button';
  if (tag === 'input') {
    if (type === 'submit' || type === 'button' || type === 'reset' || type === 'image') return 'button';
    if (type === 'checkbox') return 'checkbox';
    if (type === 'radio') return 'radio';
    return 'field';
  }
  if (el.isContentEditable) return 'field';
  return 'control';
}
"""

_VISIBLE_CHECK = """
function jarvisVisible(el) {
  if (el.disabled) return false;
  var style = window.getComputedStyle(el);
  if (style.visibility === 'hidden' || style.display === 'none') return false;
  if (el.closest('[hidden],[aria-hidden=true]')) return false;
  var rect = el.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}
"""

#: The accessible name: what a person sees the control called, which is
#: often not its own text — a checkbox's label, a field's <label for>.
_NAME = """
function jarvisClean(s) { return (s || '').replace(/\\s+/g, ' ').trim(); }
function jarvisLabel(el) {
  var aria = el.getAttribute('aria-label');
  if (aria) return jarvisClean(aria);
  var by = el.getAttribute('aria-labelledby');
  if (by) {
    var parts = by.split(/\\s+/).map(function(id) {
      var ref = document.getElementById(id); return ref ? ref.textContent : ''; });
    var joined = jarvisClean(parts.join(' '));
    if (joined) return joined;
  }
  if (el.labels && el.labels.length) {
    var own = jarvisClean(el.labels[0].innerText || el.labels[0].textContent);
    if (own) return own.replace(/:$/, '');
  }
  var wrap = el.closest('label');
  if (wrap) {
    var text = jarvisClean(wrap.innerText || wrap.textContent);
    if (text) return text;
  }
  return '';
}
function jarvisText(el, role) {
  var tag = el.tagName.toLowerCase();
  var label = jarvisLabel(el);
  if (role === 'field' || role === 'select' || role === 'combobox' || role === 'textbox' ||
      role === 'searchbox' || role === 'checkbox' || role === 'radio' || role === 'switch') {
    return label || jarvisClean(el.getAttribute('placeholder')) || jarvisClean(el.getAttribute('title')) ||
           jarvisClean(el.getAttribute('name'));
  }
  var own = jarvisClean(el.innerText || el.textContent);
  if (!own && tag === 'input') own = jarvisClean(el.value);
  if (!own) {
    var img = el.querySelector && el.querySelector('img[alt]');
    if (img) own = jarvisClean(img.getAttribute('alt'));
  }
  return own || label || jarvisClean(el.getAttribute('title'));
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
%(name)s
var roleFilter = %(role_filter)s;
var limit = %(limit)s;
var offset = %(offset)s;
if (typeof window.__jarvisSeq !== 'number') { window.__jarvisSeq = 0; }
var chrome = 'header,nav,footer,[role=navigation],[role=banner],[role=contentinfo]';
var nodes = document.querySelectorAll(%(selector)s);
var seen = [];
var found = [];
var viewH = window.innerHeight || document.documentElement.clientHeight;
for (var i = 0; i < nodes.length && found.length < 600; i++) {
  var el = nodes[i];
  if (seen.indexOf(el) !== -1) continue;
  seen.push(el);
  if (!jarvisVisible(el)) continue;
  var role = jarvisRole(el);
  if (roleFilter && roleFilter.indexOf(role) === -1) continue;
  var rect = el.getBoundingClientRect();
  var inView = rect.bottom > 0 && rect.top < viewH;
  var inChrome = !!el.closest(chrome);
  var isInput = ['field', 'select', 'checkbox', 'radio', 'combobox', 'textbox', 'searchbox'].indexOf(role) !== -1;
  var inFooter = !!el.closest('footer,[role=contentinfo]');
  var rank = inFooter ? 4 : (inChrome && !isInput) ? (inView ? 1 : 3) : (inView ? 0 : 2);
  found.push({el: el, role: role, rect: rect, inView: inView, rank: rank, order: i});
}
found.sort(function(a, b) { return a.rank - b.rank || a.order - b.order; });
var out = [];
for (var j = offset; j < found.length && out.length < limit; j++) {
  var item = found[j];
  var el = item.el;
  var handle = el.getAttribute('data-jarvis-id');
  if (!handle) {
    window.__jarvisSeq += 1;
    handle = 'jv' + window.__jarvisSeq;
    el.setAttribute('data-jarvis-id', handle);
  }
  var tag = el.tagName.toLowerCase();
  var entry = {
    handle: handle,
    role: item.role,
    tag: tag,
    text: jarvisText(el, item.role).slice(0, 160),
    name: jarvisClean(el.getAttribute('aria-label') || el.getAttribute('name') || '').slice(0, 80),
    placeholder: jarvisClean(el.getAttribute('placeholder')).slice(0, 80),
    type: el.getAttribute('type') || '',
    href: tag === 'a' ? (el.getAttribute('href') || '') : '',
    value: (tag === 'input' || tag === 'textarea' || tag === 'select') && el.type !== 'submit' &&
           el.type !== 'button' ? String(el.value || '').slice(0, 200) : '',
    visible: item.inView,
    rect: {x: Math.round(item.rect.left), y: Math.round(item.rect.top),
           w: Math.round(item.rect.width), h: Math.round(item.rect.height)}
  };
  if (item.role === 'checkbox' || item.role === 'radio' || item.role === 'switch') {
    entry.checked = !!(el.checked || el.getAttribute('aria-checked') === 'true');
  }
  if (tag === 'select') {
    entry.options = Array.prototype.slice.call(el.options, 0, 15).map(function(o) {
      return jarvisClean(o.text); });
    var chosen = el.options[el.selectedIndex];
    entry.value = chosen ? jarvisClean(chosen.text) : '';
  }
  out.push(entry);
}
var main = document.querySelector('main,[role=main],#main,#content') || document.body;
var excerpt = jarvisClean(main ? (main.innerText || main.textContent) : '').slice(0, %(text_chars)s);
var dialogs = Array.prototype.slice.call(document.querySelectorAll('[role=dialog],[role=alertdialog],dialog[open],[role=alert]'))
  .filter(function(d) { return jarvisVisible(d) && jarvisClean(d.innerText); })
  .map(function(d) { return jarvisClean(d.innerText).slice(0, 200); });
return JSON.stringify({elements: out, total: found.length, offset: offset, url: window.location.href,
                       title: document.title, text: excerpt, dialogs: dialogs.slice(0, 3),
                       ready: document.readyState});
})()"""

_INSPECT_TEMPLATE = """(function(){
%(locate)s
%(classify)s
%(name)s
var el = jarvisFind(%(handle)s);
if (!el) return JSON.stringify({found: false});
var tag = el.tagName.toLowerCase();
var role = jarvisRole(el);
var form = el.form || el.closest('form');
return JSON.stringify({
  found: true, tag: tag, role: role,
  text: jarvisText(el, role).slice(0, 160),
  label: jarvisLabel(el).slice(0, 160),
  value: (tag === 'input' ? String(el.value || '') : '').slice(0, 120),
  id: el.id || '', name: el.getAttribute('name') || '', title: el.getAttribute('title') || '',
  href: tag === 'a' ? (el.href || '') : '',
  action: el.getAttribute('formaction') ? el.formAction : (form ? form.action : '')
});
})()"""

_CLICK_TEMPLATE = """(function(){
%(locate)s
%(classify)s
%(name)s
var el = jarvisFind(%(handle)s);
if (!el) return JSON.stringify({ok: false, reason: 'stale handle — the page has changed since it was read'});
var text = jarvisText(el, jarvisRole(el)).slice(0, 120);
el.scrollIntoView({block: 'center'});
el.click();
return JSON.stringify({ok: true, text: text, url: window.location.href, title: document.title});
})()"""

_FILL_TEMPLATE = """(function(){
%(locate)s
%(classify)s
%(name)s
var el = jarvisFind(%(handle)s);
if (!el) return JSON.stringify({ok: false, reason: 'stale handle — the page has changed since it was read'});
var label = jarvisText(el, jarvisRole(el)).slice(0, 120);
el.scrollIntoView({block: 'center'});
el.focus();
var tag = el.tagName.toLowerCase();
var text = %(text)s;
if (tag === 'select') {
  var wanted = text.trim().toLowerCase();
  var options = Array.prototype.slice.call(el.options);
  var match = options.filter(function(o) { return o.value.toLowerCase() === wanted || o.text.trim().toLowerCase() === wanted; })[0]
    || options.filter(function(o) { return o.text.trim().toLowerCase().indexOf(wanted) === 0; })[0]
    || options.filter(function(o) { return wanted && o.text.trim().toLowerCase().indexOf(wanted) !== -1; })[0];
  if (!match) {
    return JSON.stringify({ok: false, reason: 'no option matches "' + text + '" — the options are: ' +
      options.map(function(o) { return o.text.trim(); }).join(', ')});
  }
  el.value = match.value;
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  return JSON.stringify({ok: true, submitted: false, chose: match.text.trim(), text: label,
                         url: window.location.href, title: document.title});
}
var proto = null;
if (tag === 'textarea') { proto = window.HTMLTextAreaElement.prototype; }
else if (tag === 'input') { proto = window.HTMLInputElement.prototype; }
var applied = false;
if (proto) {
  var setter = Object.getOwnPropertyDescriptor(proto, 'value');
  if (setter && setter.set) {
    try { setter.set.call(el, text); applied = true; } catch (e) {}
  }
}
if (!applied) {
  if (el.isContentEditable) { el.textContent = text; } else { el.value = text; }
}
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
return JSON.stringify({ok: true, submitted: submitted, text: label, url: window.location.href,
                       title: document.title});
})()"""

_SUBMIT_TEMPLATE = """(function(){
%(locate)s
%(classify)s
%(name)s
var el = jarvisFind(%(handle)s);
if (!el) return JSON.stringify({ok: false, reason: 'stale handle — the page has changed since it was read'});
var text = jarvisText(el, jarvisRole(el)).slice(0, 120);
var form = el.tagName.toLowerCase() === 'form' ? el : el.form;
if (!form) {
  el.click();
  return JSON.stringify({ok: true, text: text, url: window.location.href, title: document.title});
}
if (typeof form.requestSubmit === 'function') { form.requestSubmit(); }
else { form.submit(); }
return JSON.stringify({ok: true, text: text, url: window.location.href, title: document.title});
})()"""

_READY_SCRIPT = "(function(){return document.readyState;})()"

#: A cheap fingerprint of the page's current state. The mutation counter
#: (installed on first use in each document) changes on *any* DOM change,
#: including a client-side re-render that rebuilds identical-looking markup —
#: element counts alone would miss exactly that.
_SIGNATURE_SCRIPT = """(function(){
if (!window.__jarvisObserver) {
  window.__jarvisMutations = 0;
  window.__jarvisObserver = new MutationObserver(function(records) { window.__jarvisMutations += records.length; });
  window.__jarvisObserver.observe(document, {subtree: true, childList: true, attributes: true, characterData: true});
}
var body = document.body;
return document.readyState + ':' + window.location.href + ':' + window.__jarvisMutations + ':' +
       (body ? body.getElementsByTagName('*').length : 0);
})()"""


def build_manifest_script(*, limit: int = 60, roles: list[str] | None = None, offset: int = 0,
                          text_chars: int = 1500) -> str:
    """A script that stamps and reports the page's interactive elements.

    Its result is JSON *text* (a JS string, not a JS object) — that is what
    survives the AppleScript round trip losslessly; callers parse it with
    :func:`json.loads`.
    """
    role_filter = json.dumps(list(roles)) if roles else "null"
    return _MANIFEST_TEMPLATE % {
        "classify": _CLASSIFY_ROLE,
        "visible": _VISIBLE_CHECK,
        "name": _NAME,
        "role_filter": role_filter,
        "limit": max(1, min(int(limit), 200)),
        "offset": max(0, int(offset)),
        "text_chars": max(0, min(int(text_chars), 6000)),
        "selector": json.dumps(_SELECTOR),
    }


def build_inspect_script(handle: str) -> str:
    """Describe the element behind *handle* — used by the permission gate."""
    return _INSPECT_TEMPLATE % {"locate": _LOCATE, "classify": _CLASSIFY_ROLE, "name": _NAME,
                                "handle": json.dumps(str(handle))}


def build_click_script(handle: str) -> str:
    return _CLICK_TEMPLATE % {"locate": _LOCATE, "classify": _CLASSIFY_ROLE, "name": _NAME,
                              "handle": json.dumps(str(handle))}


def build_fill_script(handle: str, text: str, submit: bool) -> str:
    return _FILL_TEMPLATE % {
        "locate": _LOCATE,
        "classify": _CLASSIFY_ROLE,
        "name": _NAME,
        "handle": json.dumps(str(handle)),
        "text": json.dumps(str(text)),
        "submit": "true" if submit else "false",
    }


def build_submit_script(handle: str) -> str:
    return _SUBMIT_TEMPLATE % {"locate": _LOCATE, "classify": _CLASSIFY_ROLE, "name": _NAME,
                               "handle": json.dumps(str(handle))}


def ready_script() -> str:
    return _READY_SCRIPT


def signature_script() -> str:
    return _SIGNATURE_SCRIPT
