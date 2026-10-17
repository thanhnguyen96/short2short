"""JavaScript snippets for the Flow page. Selectors live only here.

Prompt text goes through Input.insertText, never through JS string concat.
There is no js_type_prompt.
"""

from __future__ import annotations

import json
import re
import urllib.parse

JS_HELPERS: str = r"""
const fpVisible = (el) => !!(el && el.getClientRects && el.getClientRects().length);
const fpText = (el) => ((el && (el.innerText || el.textContent)) || '').replace(/\s+/g, ' ').trim();
const fpClickable = (el) =>
  el.closest('button,a,[role=button],[role=option],[role=radio],[tabindex]') || el;
// Only the agent panel's composer goes through the approval card. The page-level
// prompt box generates media directly with its own model and must never be used.
// Answered approval cards stay in the chat with their options aria-disabled.
const fpEnabled = (el) => {
  const c = fpClickable(el);
  return !(c.disabled || c.getAttribute('aria-disabled') === 'true');
};
// Card options are role=radio; the user's own "Approve" chat bubble must not count.
const fpApproveEls = () => [...document.querySelectorAll('[role=radio]')].filter((x) =>
  fpVisible(x) && /^(check\s*)?Approve$/.test(fpText(x)) && fpEnabled(x)
);
const fpAgentPanel = () => [...document.querySelectorAll('flow-agent-panel')].find(fpVisible);
const fpComposer = () => {
  const panel = fpAgentPanel();
  if (!panel) return null;
  const eds = [...panel.querySelectorAll('[contenteditable=true],[role=textbox]')];
  return eds.find(fpVisible) || null;
};
"""


def _wrap(body: str, *, async_fn: bool = False) -> str:
    """Prefix helpers and return an IIFE expression ending in JSON.stringify."""
    tag = "async function" if async_fn else "function"
    return f"({tag}() {{\n{JS_HELPERS}\n{body}\n}})()"


JS_PAGE_FACTS: str = _wrap(
    r"""/* fp_marker_page_facts */
return JSON.stringify({
  href: location.href,
  host: location.host,
  title: document.title
});"""
)

JS_LOGGED_IN: str = _wrap(
    r"""/* fp_marker_logged_in */
const composerEl = [
  ...document.querySelectorAll('[contenteditable=true],[role=textbox]')
].find(fpVisible);
const hasComposer = !!composerEl;
const host = location.host;
const href = location.href;
const loggedIn = hasComposer && host === 'flow.google.com' && !host.includes('accounts.google.com');
return JSON.stringify({
  logged_in: loggedIn,
  host: host,
  href: href,
  composer: hasComposer
});"""
)

JS_FOCUS_COMPOSER: str = _wrap(
    r"""/* fp_marker_focus */
const composerEl = fpComposer();
if (!composerEl) {
  if (!fpAgentPanel()) {
    const expand = [...document.querySelectorAll('button[aria-label="Expand"]')].find(fpVisible);
    if (expand) expand.click();
  }
  return JSON.stringify({ok: false, reason: 'no_composer'});
}
composerEl.focus();
return JSON.stringify({ok: true});"""
)

JS_COMPOSER_TEXT: str = _wrap(
    r"""/* fp_marker_composer_text */
const composerEl = fpComposer();
if (!composerEl) {
  return JSON.stringify({ok: false, text: ''});
}
return JSON.stringify({ok: true, text: fpText(composerEl)});"""
)

JS_VISIBILITY: str = _wrap(
    r"""/* fp_marker_visibility */
return JSON.stringify({state: document.visibilityState});"""
)

JS_CLICK_SEND: str = _wrap(
    r"""/* fp_marker_click_send */
const panel = fpAgentPanel();
const btn = panel
  ? [...panel.querySelectorAll('button[aria-label="Start generation"]')].find(fpVisible)
  : null;
if (!btn || btn.disabled || btn.getAttribute('aria-disabled') === 'true') {
  return JSON.stringify({ok: false, reason: 'no_send_button'});
}
btn.click();
return JSON.stringify({ok: true});"""
)

JS_PROJECTS: str = _wrap(
    r"""/* fp_marker_projects */
const uuidRe = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;
const anchors = [...document.querySelectorAll('a[href]')].filter(fpVisible);
const projects = [];
for (const a of anchors) {
  const href = a.href || '';
  const text = fpText(a);
  if (!text) continue;
  const path = a.pathname || href;
  if (href.includes('/project') || uuidRe.test(path)) {
    projects.push({text: text, href: href});
    if (projects.length >= 50) break;
  }
}
const hasNewProject = [...document.querySelectorAll('*')].some(
  el => fpVisible(el) && fpText(el) === 'New project'
);
return JSON.stringify({
  href: location.href,
  projects: projects,
  new_project: hasNewProject
});"""
)

JS_MEDIA_SNAPSHOT: str = _wrap(
    r"""/* fp_marker_media */
// Tile thumbnails use opaque /asb/ URLs; the poster image under flow-content.google/image/
// carries the same uuid as the video, so both paths count.
const uuidRe = /flow-content\.google\/(?:video|image)\/([0-9a-fA-F-]{36})/;
const seen = new Set();
const uuids = [];
const addUrl = (u) => {
  if (!u || typeof u !== 'string') return;
  const m = u.match(uuidRe);
  if (m && m[1] && !seen.has(m[1])) {
    seen.add(m[1]);
    uuids.push(m[1]);
  }
};
const tiles = document.querySelectorAll('flow-video-tile');
tiles.forEach(tile => {
  tile.querySelectorAll('img[src], video, source[src], a[href]').forEach(el => {
    addUrl(el.getAttribute('src'));
    addUrl(el.src);
    if (el.currentSrc) addUrl(el.currentSrc);
    addUrl(el.getAttribute('href'));
    addUrl(el.href);
  });
});
const perfEntries = (
  window.performance && performance.getEntriesByType
    ? performance.getEntriesByType('resource')
    : []
)
  .map(e => e.name || '')
  .filter(n => n.includes('flow-content.google/'));
perfEntries.forEach(addUrl);
const videoEntries = perfEntries.filter(n => n.includes('flow-content.google/video/'));
const latestVideoUrl = videoEntries.length ? videoEntries[videoEntries.length - 1] : '';
return JSON.stringify({uuids: uuids, latest_video_url: latestVideoUrl});"""
)

JS_TILE_COUNT: str = _wrap(
    r"""/* fp_marker_tile_count */
const tiles = [...document.querySelectorAll('flow-video-tile')].filter(fpVisible);
return JSON.stringify({count: tiles.length});"""
)


def js_open_tile(index: int) -> str:
    """Sync snippet: click the thumbnail of the index-th visible video tile."""
    if index < 0:
        raise ValueError("tile index must be >= 0")
    body = (
        "/* fp_marker_open_tile */\n"
        "const tiles = [...document.querySelectorAll('flow-video-tile')].filter(fpVisible);\n"
        f"const tile = tiles[{index}];\n"
        "if (!tile) return JSON.stringify({ok: false, reason: 'no_tile'});\n"
        "const img = tile.querySelector('img') || tile;\n"
        "img.click();\n"
        "return JSON.stringify({ok: true});"
    )
    return _wrap(body)


JS_LATEST_REPLY: str = _wrap(
    r"""/* fp_marker_reply */
const candidates = [...document.querySelectorAll('*')].filter(el => {
  if (!fpVisible(el)) return false;
  if (!(el.scrollHeight > el.clientHeight + 40)) return false;
  const rect = el.getBoundingClientRect();
  return rect.left > window.innerWidth * 0.45;
});
let best = null;
let maxLen = -1;
candidates.forEach(el => {
  const len = (el.innerText || '').length;
  if (len > maxLen) {
    maxLen = len;
    best = el;
  }
});
const node = best || document.body;
const source = best ? 'scroller' : 'body';
const fullText = fpText(node);
const text = fullText.slice(-4000);
return JSON.stringify({text: text, source: source});"""
)

JS_APPROVAL_TEXT: str = _wrap(
    r"""/* fp_marker_approval */
// The newest open card: walk up from the last enabled Approve option.
const opts = fpApproveEls();
if (!opts.length) {
  return JSON.stringify({text: ''});
}
let curr = opts[opts.length - 1];
for (let i = 0; i < 8 && curr.parentElement; i++) {
  const parent = curr.parentElement;
  const pt = fpText(parent);
  if (pt.length > 800) break;
  curr = parent;
  if (/credit/i.test(pt)) break;
}
const text = fpText(curr);
return JSON.stringify({text: /credit/i.test(text) ? text : ''});"""
)

JS_PROGRESS: str = _wrap(
    r"""/* fp_marker_progress */
const raw = (document.body && document.body.innerText) || '';
const lines = [];
const pctMatches = raw.match(/\d{1,3}%/g) || [];
for (let i = 0; i < Math.min(8, pctMatches.length); i++) {
  lines.push(pctMatches[i].slice(0, 240));
}
const rawLines = raw.split('\n');
for (const line of rawLines) {
  const trimmed = line.replace(/\s+/g, ' ').trim();
  if (!trimmed) continue;
  const up = trimmed.toUpperCase();
  if (up.includes('GENERATING') || up.includes('QUEUE') || up.includes('HIGH DEMAND')) {
    lines.push(trimmed.slice(0, 240));
  }
}
return JSON.stringify({lines: lines});"""
)

JS_CLICK_APPROVE: str = _wrap(
    r"""/* fp_marker_click_approve */
const els = fpApproveEls();
if (!els.length) {
  return JSON.stringify({ok: false, reason: 'no_approve'});
}
const target = fpClickable(els[els.length - 1]);
target.click();
return JSON.stringify({
  ok: true,
  tag: target.tagName || '',
  role: target.getAttribute('role') || ''
});"""
)

JS_APPROVE_STILL_THERE: str = _wrap(
    r"""/* fp_marker_approve_present */
const anyPresent = fpApproveEls().length > 0;
return JSON.stringify({present: anyPresent});"""
)

JS_NEW_PROJECT: str = _wrap(
    r"""/* fp_marker_new_project */
const els = [...document.querySelectorAll('*')].filter(
  el => fpVisible(el) && fpText(el) === 'New project'
);
if (!els.length) {
  return JSON.stringify({ok: false, reason: 'no_new_project'});
}
els.sort((a, b) => fpText(a).length - fpText(b).length);
fpClickable(els[0]).click();
/* navigation wait */
await new Promise((r) => setTimeout(r, 1500));
return JSON.stringify({ok: true, href: location.href});""",
    async_fn=True,
)


def js_new_project() -> str:
    """Async snippet: click the visible control whose text is exactly 'New project'."""
    return JS_NEW_PROJECT


def js_open_project(url: str) -> str:
    """Async snippet: click the visible link whose href starts with url, or set location.

    Raises ValueError if url is not https and host is not flow.google.com.
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "flow.google.com":
        raise ValueError("refusing non-flow project url")
    dumped_url = json.dumps(url)
    body = (
        "/* fp_marker_open_project */\n"
        f"const targetUrl = {dumped_url};\n"
        "const anchors = [...document.querySelectorAll('a[href]')].filter(fpVisible);\n"
        "const norm = (u) => u.replace(/[?#].*$/, '').replace(/\\/+$/, '');\n"
        "// Exact match only: sub-pages like /project/<id>/tools have no agent panel.\n"
        "const match = anchors.find(a => norm(a.href) === norm(targetUrl));\n"
        "if (match) {\n"
        "  match.click();\n"
        "} else {\n"
        "  location.assign(targetUrl);\n"
        "}\n"
        "/* navigation wait */\n"
        "await new Promise((r) => setTimeout(r, 1500));\n"
        "return JSON.stringify({ok: true, href: location.href});"
    )
    return _wrap(body, async_fn=True)


def js_fetch_media_b64(url: str) -> str:
    """Async snippet: fetch(url) with no credentials, return base64 or an error tag.

    Raises ValueError unless scheme is https and host is flow-content.google.
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "flow-content.google":
        raise ValueError("refusing non-media url")
    dumped_url = json.dumps(url)
    body = (
        "/* fp_marker_fetch_media */\n"
        f"const r = await fetch({dumped_url}, {{credentials: 'omit'}});\n"
        "if (!r.ok) return JSON.stringify({ok: false, status: r.status});\n"
        "const b = new Uint8Array(await r.arrayBuffer());\n"
        "let s = '';\n"
        "for (let i = 0; i < b.length; i += 0x8000) {\n"
        "  s += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000));\n"
        "}\n"
        "return JSON.stringify({ok: true, b64: btoa(s)});"
    )
    return _wrap(body, async_fn=True)


_MEDIA_URL_BODY: str = r"""/* fp_marker_perf_url */
const want = __WANT__;
const entries = (
  window.performance && performance.getEntriesByType
    ? performance.getEntriesByType('resource')
    : []
)
  .map(e => e.name || '')
  .concat([...document.querySelectorAll('video, source')].map(v => v.currentSrc || v.src || ''))
  .filter(n => n.includes('flow-content.google/video/') && (!want || n.includes(want)));
const perfUrl = entries.length ? entries[entries.length - 1] : '';
return JSON.stringify({url: perfUrl});"""

_UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")


def js_media_url_from_performance(uuid: str = "") -> str:
    """Sync snippet: last resource/video URL under flow-content.google/video/, or empty.

    With uuid, only URLs containing that uuid. Raises ValueError for a malformed uuid.
    """
    if uuid and not _UUID_RE.fullmatch(uuid):
        raise ValueError("refusing malformed uuid")
    return _wrap(_MEDIA_URL_BODY.replace("__WANT__", json.dumps(uuid)))


JS_OPEN_AGENT_SETTINGS: str = _wrap(
    r"""/* fp_marker_open_settings */
if (document.querySelector('flow-settings-view')) {
  return JSON.stringify({ok: true});
}
const panel = fpAgentPanel();
const btn = panel
  ? [...panel.querySelectorAll('button[aria-label="Settings"]')].find(fpVisible)
  : null;
if (!btn) return JSON.stringify({ok: false, reason: 'no_settings_button'});
btn.click();
return JSON.stringify({ok: true});"""
)

JS_CONFIRM_SETTING: str = _wrap(
    r"""/* fp_marker_confirm_setting */
const view = document.querySelector('flow-settings-view');
if (!view) return JSON.stringify({ok: false, reason: 'no_settings_view'});
const opts = [...view.querySelectorAll('mat-radio-button')].filter(
  b => /^(Always|Never)\b/.test(fpText(b))
);
const checked = opts.find(b => { const i = b.querySelector('input'); return i && i.checked; });
return JSON.stringify({ok: true, value: checked ? fpText(checked).split(' ')[0] : ''});"""
)

JS_SET_CONFIRM_ALWAYS: str = _wrap(
    r"""/* fp_marker_set_confirm_always */
const view = document.querySelector('flow-settings-view');
if (!view) return JSON.stringify({ok: false, reason: 'no_settings_view'});
const always = [...view.querySelectorAll('mat-radio-button')].find(
  b => /^Always\b/.test(fpText(b))
);
const input = always && always.querySelector('input');
if (!input) return JSON.stringify({ok: false, reason: 'no_always_option'});
input.click();
const save = [...view.querySelectorAll('button')].find(b => fpText(b) === 'Save');
if (!save) return JSON.stringify({ok: false, reason: 'no_save'});
save.click();
return JSON.stringify({ok: true});"""
)

JS_CLOSE_AGENT_SETTINGS: str = _wrap(
    r"""/* fp_marker_close_settings */
const panel = fpAgentPanel();
const back = panel
  ? [...panel.querySelectorAll('button[aria-label="Back"]')].find(fpVisible)
  : null;
if (back && document.querySelector('flow-settings-view')) back.click();
return JSON.stringify({ok: true});"""
)
