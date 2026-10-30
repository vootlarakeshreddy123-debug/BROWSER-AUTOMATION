"""Generic live DOM inspector for NEXA.

The inspector is deliberately site-agnostic.  It gathers enough structured
DOM evidence for a local model to reason about unfamiliar pages while keeping
the payload bounded.  It also inspects same/cross-origin Playwright frames and
open Shadow DOM roots, which are common on modern web applications.
"""
from __future__ import annotations

import base64
from typing import Any
from playwright.async_api import Page, Frame

DEFAULT_TIMEOUT_MS = 5000
MAX_ELEMENTS = 240
MAX_TEXT_LENGTH = 320
MAX_ATTRIBUTE_LENGTH = 600
MAX_HEADINGS = 40
MAX_FRAMES = 12


def _clean(value: Any, limit: int = MAX_TEXT_LENGTH) -> str:
    return " ".join(str(value or "").split())[:limit]


_JS = r"""
({maxElements, maxTextLength, maxAttributeLength}) => {
  const clean = (value, max) => String(value || "")
    .replace(/\s+/g, " ").trim().slice(0, max);
  const visible = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 &&
      s.display !== 'none' && s.visibility !== 'hidden' &&
      s.opacity !== '0' && !el.hasAttribute('hidden') &&
      el.getAttribute('aria-hidden') !== 'true';
  };
  const regionFor = (el) => {
    const semantic = el.closest('main,[role="main"],article,footer,header,nav,aside,[role="navigation"],[role="banner"],[role="contentinfo"]');
    if (!semantic) return 'content';
    const tag = semantic.tagName.toLowerCase();
    const role = (semantic.getAttribute('role') || '').toLowerCase();
    if (tag === 'main' || role === 'main') return 'main';
    if (tag === 'article') return 'article';
    if (tag === 'footer' || role === 'contentinfo') return 'footer';
    if (tag === 'header' || role === 'banner') return 'header';
    if (tag === 'nav' || role === 'navigation') return 'nav';
    if (tag === 'aside') return 'sidebar';
    return 'content';
  };
  const xpathFor = (el) => {
    if (el.id) return `//*[@id=${JSON.stringify(el.id)}]`;
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && node !== document.documentElement) {
      let index = 1, sibling = node.previousElementSibling;
      while (sibling) { if (sibling.tagName === node.tagName) index++; sibling = sibling.previousElementSibling; }
      parts.unshift(`${node.tagName.toLowerCase()}[${index}]`);
      node = node.parentElement;
    }
    return '/html[1]/' + parts.join('/');
  };
  const labelTextFor = (el) => {
    const bits = [];
    try { if (el.labels) for (const l of el.labels) bits.push(l.innerText || l.textContent); } catch (e) {}
    const lb = el.getAttribute('aria-labelledby');
    if (lb) for (const id of lb.split(/\s+/)) { const n = document.getElementById(id); if (n) bits.push(n.innerText || n.textContent); }
    if (!bits.length) { const wrap = el.closest('label'); if (wrap) bits.push(wrap.innerText || wrap.textContent); }
    return clean(bits.join(' '), maxTextLength);
  };
  const formHintFor = (el) => {
    const f = el.closest('form,[role="search"],[role="form"]');
    if (!f) return '';
    const bits = [f.getAttribute('role') || '', f.getAttribute('aria-label') || '', f.getAttribute('id') || '',
                  f.getAttribute('class') || '', f.getAttribute('action') || '', f.getAttribute('name') || ''];
    const h = f.querySelector('h1,h2,h3,h4,legend');
    if (h) bits.push(h.innerText || h.textContent);
    const submit = f.querySelector('button,input[type=submit]');
    if (submit) bits.push(submit.innerText || submit.value || submit.getAttribute('aria-label') || '');
    return clean(bits.join(' '), 240);
  };
  const selectors = 'a,button,input,textarea,select,option,[role],[contenteditable="true"]';
  const elements = [];
  const headings = [];
  const seen = new Set();
  const addElement = (el, shadow) => {
    if (!el || seen.has(el) || !visible(el) || elements.length >= maxElements * 2) return;
    seen.add(el);
    const r = el.getBoundingClientRect();
    const role = el.getAttribute('role') ||
      (el.tagName === 'A' ? 'link' : el.tagName === 'BUTTON' ? 'button' :
       el.tagName === 'INPUT' ? ((el.type || '').toLowerCase() === 'search' ? 'searchbox' : 'textbox') : null);
    const aria = el.getAttribute('aria-label');
    const placeholder = el.getAttribute('placeholder');
    const href = el.getAttribute('href');
    const isField = el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT';
    // accessible-name order for form controls: aria-label, associated <label>, then placeholder (what a role+name locator matches)
    let name = aria || (isField ? labelTextFor(el) : '') || placeholder || '';
    if (!name && el.tagName === 'INPUT') name = el.getAttribute('name') || el.getAttribute('type') || '';
    if (!name) name = el.innerText || el.textContent || '';
    elements.push({
      index: elements.length, tag: el.tagName.toLowerCase(), role,
      name: clean(name, maxTextLength), text: clean(el.innerText || el.textContent, maxTextLength),
      aria_label: clean(aria, maxTextLength), placeholder: clean(placeholder, maxTextLength),
      label: clean(el.getAttribute('aria-labelledby'), maxTextLength), href: clean(href, maxAttributeLength),
      type: clean(el.getAttribute('type'), 80), disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
      checked: !!el.checked, value: clean(el.value, maxTextLength), contenteditable: el.getAttribute('contenteditable') === 'true',
      visible: true, in_main: !!el.closest('main,[role="main"],article'), region: regionFor(el),
      viewport_top: Math.round(r.top), viewport_bottom: Math.round(r.bottom),
      label_text: labelTextFor(el), name_attr: clean(el.getAttribute('name') || el.id, 80),
      input_type: clean((el.getAttribute('type') || (el.tagName === 'INPUT' ? 'text' : '')).toLowerCase(), 40),
      autocomplete: clean(el.getAttribute('autocomplete'), 60), title: clean(el.getAttribute('title'), maxTextLength),
      readonly: !!el.readOnly || el.getAttribute('aria-readonly') === 'true',
      form_hint: formHintFor(el),
      xpath: shadow ? null : xpathFor(el), shadow_dom: !!shadow,
    });
  };
  const walk = (root, shadow=false) => {
    if (!root || !root.querySelectorAll) return;
    for (const el of Array.from(root.querySelectorAll(selectors))) addElement(el, shadow);
    for (const el of Array.from(root.querySelectorAll('*'))) {
      if (el.shadowRoot) walk(el.shadowRoot, true);
    }
  };
  walk(document, false);
  for (const el of Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6'))) {
    if (visible(el)) headings.push({tag: el.tagName.toLowerCase(), text: clean(el.innerText || el.textContent, 500)});
  }
  const sectionSummaries = [];
  const headingNodes = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6')).filter(visible);
  for (let i = 0; i < headingNodes.length && sectionSummaries.length < 40; i++) {
    const h = headingNodes[i];
    const level = Number(h.tagName.slice(1));
    const parts = [];
    let node = h.nextElementSibling;
    let guard = 0;
    while (node && guard++ < 80) {
      if (/^H[1-6]$/.test(node.tagName) && Number(node.tagName.slice(1)) <= level) break;
      if (visible(node)) {
        const value = clean(node.innerText || node.textContent, 700);
        if (value) parts.push(value);
      }
      node = node.nextElementSibling;
    }
    sectionSummaries.push({level, heading: clean(h.innerText || h.textContent, 500), text: clean(parts.join(' '), 1400)});
  }
  const bodyText = clean(document.body ? document.body.innerText : '', 10000);
  return {
    body_text: bodyText,
    elements: elements.slice(0, maxElements),
    headings: headings.slice(0, 40),
    sections: sectionSummaries,
    viewport: {width: innerWidth, height: innerHeight, scrollY: scrollY, scrollHeight: document.documentElement.scrollHeight},
  };
}
"""


async def _inspect_frame(frame: Frame) -> dict[str, Any]:
    try:
        return await frame.evaluate(_JS, {
            "maxElements": MAX_ELEMENTS,
            "maxTextLength": MAX_TEXT_LENGTH,
            "maxAttributeLength": MAX_ATTRIBUTE_LENGTH,
        })
    except Exception:
        return {"body_text": "", "elements": [], "headings": [], "sections": [], "viewport": {}}


async def snapshot(page: Page) -> dict[str, Any]:
    try: url = page.url
    except Exception: url = ""
    try: title = await page.title()
    except Exception: title = ""

    main = await _inspect_frame(page.main_frame)
    all_elements = list(main.get("elements") or [])
    headings = list(main.get("headings") or [])
    sections = list(main.get("sections") or [])
    frames_meta = []
    frame_count = 0

    # Each Playwright frame is inspected independently.  Cross-origin frames
    # are safe here because Playwright has frame-level execution privileges.
    for frame in list(page.frames)[1:MAX_FRAMES + 1]:
        frame_count += 1
        try:
            fdata = await _inspect_frame(frame)
            prefix = f"frame:{frame.url[:160]}"
            for el in fdata.get("elements") or []:
                el = dict(el)
                el["frame_url"] = frame.url[:500]
                el["frame_name"] = frame.name[:120]
                el["index"] = len(all_elements)
                all_elements.append(el)
                if len(all_elements) >= MAX_ELEMENTS: break
            for h in fdata.get("headings") or []:
                headings.append({**h, "frame_url": frame.url[:500]})
            for sec in fdata.get("sections") or []:
                sections.append({**sec, "frame_url": frame.url[:500]})
            frames_meta.append({"url": frame.url[:500], "name": frame.name[:120], "elements": len(fdata.get("elements") or [])})
        except Exception:
            continue
        if len(all_elements) >= MAX_ELEMENTS: break

    # Preserve the most useful controls: main/article/content elements first,
    # then semantic controls, then remaining elements.
    def score(el: dict[str, Any]) -> float:
        value = 0.0
        if el.get("region") in {"main", "article", "content"}: value += 8
        if el.get("role") in {"searchbox", "textbox", "button", "link", "tab", "menuitem"}: value += 5
        if el.get("href"): value += 2
        if el.get("shadow_dom"): value += 1
        if el.get("disabled"): value -= 10
        try: value += max(0, 4 - float(el.get("viewport_top", 9999)) / 500)
        except Exception: pass
        return value
    all_elements.sort(key=score, reverse=True)
    for i, el in enumerate(all_elements[:MAX_ELEMENTS]): el["index"] = i

    return {
        "url": url, "title": title, "current_url": url, "current_title": title,
        "body_text": _clean(main.get("body_text", ""), 9000),
        "elements": all_elements[:MAX_ELEMENTS],
        "headings": headings[:MAX_HEADINGS],
        "sections": sections[:MAX_HEADINGS],
        "frames": frames_meta,
        "frame_count": frame_count,
        "viewport": main.get("viewport", {}),
    }


async def screenshot_b64(page: Page, timeout_ms: int = 1500) -> str | None:
    try:
        data = await page.screenshot(type="jpeg", quality=60, timeout=timeout_ms)
        return base64.b64encode(data).decode("ascii")
    except Exception:
        return None

inspect_page = snapshot
get_snapshot = snapshot
