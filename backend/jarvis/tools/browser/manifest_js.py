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

**Beyond the top document (v3.0).** Modern pages put controls inside open
shadow roots (web components) and same-origin iframes (embedded forms,
widgets). The manifest walks all of them; a handle stamped inside one is
found again the same way, whichever browser runs the script.

**Never a password.** The fill script refuses password fields and payment
card fields outright: signing in and paying are the user's to do.

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
  var style = (el.ownerDocument.defaultView || window).getComputedStyle(el);
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
    var scope = el.getRootNode && el.getRootNode().getElementById ? el.getRootNode() : el.ownerDocument;
    var parts = by.split(/\\s+/).map(function(id) {
      var ref = scope.getElementById(id); return ref ? ref.textContent : ''; });
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

#: Every place an element can live: the document, open shadow roots and
#: same-origin iframes (with the iframe's offset, so "in view" stays true to
#: the screen). Cross-origin frames are out of reach by design.
_ROOTS = """
function jarvisRoots() {
  var out = [];
  function walk(root, x, y, depth) {
    out.push({root: root, x: x, y: y});
    if (depth > 5) return;
    var all = root.querySelectorAll('*');
    for (var i = 0; i < all.length; i++) {
      var el = all[i];
      if (el.shadowRoot) walk(el.shadowRoot, x, y, depth + 1);
      if (el.tagName === 'IFRAME' || el.tagName === 'FRAME') {
        try {
          var doc = el.contentDocument;
          if (doc && doc.documentElement) {
            var r = el.getBoundingClientRect();
            walk(doc, x + r.left, y + r.top, depth + 1);
          }
        } catch (e) {}
      }
    }
  }
  walk(document, 0, 0, 0);
  return out;
}
"""

_LOCATE = _ROOTS + """
function jarvisFind(handle) {
  var selector = '[data-jarvis-id="' + handle + '"]';
  var direct = document.querySelector(selector);
  if (direct) return direct;
  var roots = jarvisRoots();
  for (var i = 1; i < roots.length; i++) {
    var el = roots[i].root.querySelector(selector);
    if (el) return el;
  }
  return null;
}
"""

_MANIFEST_TEMPLATE = """(function(){
%(classify)s
%(visible)s
%(name)s
%(roots)s
var roleFilter = %(role_filter)s;
var limit = %(limit)s;
var offset = %(offset)s;
if (typeof window.__jarvisSeq !== 'number') { window.__jarvisSeq = 0; }
var chrome = 'header,nav,footer,[role=navigation],[role=banner],[role=contentinfo]';
var found = [];
var order = 0;
var viewH = window.innerHeight || document.documentElement.clientHeight;
var roots = jarvisRoots();
var passwordField = false;
var captcha = false;
for (var r = 0; r < roots.length && found.length < 600; r++) {
  var scope = roots[r];
  var nodes = scope.root.querySelectorAll(%(selector)s);
  for (var i = 0; i < nodes.length && found.length < 600; i++) {
    var el = nodes[i];
    order += 1;
    if (!jarvisVisible(el)) continue;
    if (el.tagName === 'INPUT' && (el.getAttribute('type') || '').toLowerCase() === 'password') passwordField = true;
    var role = jarvisRole(el);
    if (roleFilter && roleFilter.indexOf(role) === -1) continue;
    var own = el.getBoundingClientRect();
    var rect = {left: own.left + scope.x, top: own.top + scope.y, width: own.width, height: own.height,
                bottom: own.bottom + scope.y};
    var inView = rect.bottom > 0 && rect.top < viewH;
    var inChrome = !!el.closest(chrome);
    var isInput = ['field', 'select', 'checkbox', 'radio', 'combobox', 'textbox', 'searchbox'].indexOf(role) !== -1;
    var inFooter = !!el.closest('footer,[role=contentinfo]');
    var rank = inFooter ? 4 : (inChrome && !isInput) ? (inView ? 1 : 3) : (inView ? 0 : 2);
    found.push({el: el, role: role, rect: rect, inView: inView, rank: rank, order: order});
  }
}
// A challenge the user must pass: a visible CAPTCHA widget of real size, or
// a "checking your browser" interstitial. Not the invisible-reCAPTCHA badge
// many ordinary pages carry in a corner — that needs nobody.
var challenges = document.querySelectorAll('iframe[src*="recaptcha"],iframe[src*="hcaptcha"],' +
  'iframe[src*="challenges.cloudflare.com"],iframe[title*="captcha" i],iframe[title*="challenge" i],' +
  '[class*="captcha" i],[id*="captcha" i]');
for (var c = 0; c < challenges.length && !captcha; c++) {
  var box = challenges[c];
  var src = box.getAttribute('src') || '';
  var ident = ((box.getAttribute('class') || '') + ' ' + (box.id || '')).toLowerCase();
  if (/size=invisible/.test(src) || ident.indexOf('badge') !== -1 || box.closest('.grecaptcha-badge')) continue;
  if (box.tagName === 'TEXTAREA' || box.tagName === 'INPUT' || !jarvisVisible(box)) continue;
  var area = box.getBoundingClientRect();
  if (area.width >= 60 && area.height >= 30) captcha = true;
}
if (/^(just a moment|attention required|verify you are human)/i.test(document.title)) captcha = true;
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
                       ready: document.readyState,
                       signals: {password_field: passwordField, captcha: captcha}});
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
  type: (el.getAttribute('type') || '').toLowerCase(),
  autocomplete: (el.getAttribute('autocomplete') || '').toLowerCase(),
  suggests: !!(el.getAttribute('aria-autocomplete') || el.getAttribute('list') || role === 'combobox'),
  editable: !!el.isContentEditable,
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
var kind = (el.getAttribute('type') || '').toLowerCase();
var auto = (el.getAttribute('autocomplete') || '').toLowerCase();
var ident = ((el.getAttribute('name') || '') + ' ' + (el.id || '')).toLowerCase();
if (kind === 'password' || auto.indexOf('password') !== -1) {
  return JSON.stringify({ok: false, refused: true, reason: 'that is a password field — signing in is for the user to do; ask them to take over'});
}
if (auto.indexOf('cc-') === 0 || /card.?number|cardnum|cvv|cvc|security.?code|expir/.test(ident)) {
  return JSON.stringify({ok: false, refused: true, reason: 'that is a payment card field — JARVIS never enters payment details'});
}
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
var realm = el.ownerDocument.defaultView || window;
var proto = null;
if (tag === 'textarea') { proto = realm.HTMLTextAreaElement.prototype; }
else if (tag === 'input') { proto = realm.HTMLInputElement.prototype; }
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

#: The element that has keyboard focus — followed into shadow roots and
#: same-origin frames — stamped with a handle so it can be inspected like
#: any listed element.
_ACTIVE_SCRIPT = """(function(){
var el = document.activeElement;
for (var hops = 0; el && hops < 8; hops++) {
  if (el.shadowRoot && el.shadowRoot.activeElement) { el = el.shadowRoot.activeElement; continue; }
  if (el.tagName === 'IFRAME' || el.tagName === 'FRAME') {
    var inner = null;
    try { inner = el.contentDocument ? el.contentDocument.activeElement : null; } catch (e) {}
    if (inner) { el = inner; continue; }
  }
  break;
}
if (!el || el.tagName === 'BODY' || el.tagName === 'HTML') return JSON.stringify({handle: ''});
if (typeof window.__jarvisSeq !== 'number') { window.__jarvisSeq = 0; }
var handle = el.getAttribute('data-jarvis-id');
if (!handle) { window.__jarvisSeq += 1; handle = 'jv' + window.__jarvisSeq; el.setAttribute('data-jarvis-id', handle); }
return JSON.stringify({handle: handle});
})()"""

_SCROLL_TEMPLATE = """(function(){
%(locate)s
var handle = %(handle)s;
var direction = %(direction)s;
if (handle) {
  var el = jarvisFind(handle);
  if (!el) return JSON.stringify({ok: false, reason: 'stale handle — the page has changed since it was read'});
  el.scrollIntoView({block: 'center'});
} else if (direction === 'top') { window.scrollTo(0, 0); }
else if (direction === 'bottom') { window.scrollTo(0, document.documentElement.scrollHeight); }
else { window.scrollBy(0, (direction === 'up' ? -1 : 1) * Math.round(window.innerHeight * 0.8)); }
return JSON.stringify({ok: true, y: Math.round(window.scrollY),
                       height: document.documentElement.scrollHeight, view: window.innerHeight});
})()"""

_BACK_SCRIPT = "(function(){history.back();return JSON.stringify({ok:true});})()"

_KEY_TEMPLATE = """(function(){
%(locate)s
var handle = %(handle)s;
var key = %(key)s;
var el = handle ? jarvisFind(handle) : (document.activeElement || document.body);
if (!el) return JSON.stringify({ok: false, reason: 'stale handle — the page has changed since it was read'});
if (handle) { el.focus(); }
['keydown', 'keypress', 'keyup'].forEach(function(type) {
  el.dispatchEvent(new KeyboardEvent(type, {key: key, bubbles: true, cancelable: true}));
});
if (key === 'Enter' && el.form && typeof el.form.requestSubmit === 'function') { el.form.requestSubmit(); }
return JSON.stringify({ok: true});
})()"""

_FIND_TEXT_TEMPLATE = """(function(){
var wanted = %(text)s.toLowerCase();
var body = document.body ? (document.body.innerText || '') : '';
return JSON.stringify({found: body.toLowerCase().indexOf(wanted) !== -1, url: window.location.href});
})()"""

#: A cheap fingerprint of the page's current state. The mutation counter
#: (installed on first use in each document, same-origin frames included)
#: changes on *any* DOM change, including a client-side re-render that
#: rebuilds identical-looking markup — element counts alone would miss
#: exactly that.
_SIGNATURE_SCRIPT = """(function(){
function watch(win) {
  if (!win.__jarvisObserver) {
    win.__jarvisMutations = 0;
    win.__jarvisObserver = new win.MutationObserver(function(records) { win.__jarvisMutations += records.length; });
    win.__jarvisObserver.observe(win.document, {subtree: true, childList: true, attributes: true, characterData: true});
  }
  return win.__jarvisMutations;
}
var total = watch(window);
var state = document.readyState;
var frames = document.querySelectorAll('iframe,frame');
for (var i = 0; i < frames.length; i++) {
  try {
    var win = frames[i].contentWindow;
    if (win && win.document && win.document.documentElement) {
      total += watch(win);
      if (win.document.readyState !== 'complete') state = win.document.readyState;
    }
  } catch (e) {}
}
var body = document.body;
return state + ':' + window.location.href + ':' + total + ':' +
       (body ? body.getElementsByTagName('*').length : 0);
})()"""


def build_scroll_script(direction: str = "down", handle: str = "") -> str:
    return _SCROLL_TEMPLATE % {"locate": _LOCATE, "handle": json.dumps(str(handle or "")),
                               "direction": json.dumps(str(direction or "down"))}


def back_script() -> str:
    return _BACK_SCRIPT


def build_key_script(key: str, handle: str = "") -> str:
    return _KEY_TEMPLATE % {"locate": _LOCATE, "handle": json.dumps(str(handle or "")),
                            "key": json.dumps(str(key))}


def build_find_text_script(text: str) -> str:
    return _FIND_TEXT_TEMPLATE % {"text": json.dumps(str(text))}


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
        "roots": _ROOTS,
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


def active_element_script() -> str:
    return _ACTIVE_SCRIPT


def signature_script() -> str:
    return _SIGNATURE_SCRIPT
