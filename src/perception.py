"""Turns a live page into (a) a compact text observation for the LLM and (b) a
ranked, multi-tier Locator per interactive element for the artifact.

This is the perceive side of the perceive/act seam described in REPORT.md: it
knows nothing about goals or steps, only "what's on screen and how would I find
it again." Swapping in a different surface (desktop/native) means replacing this
module's extraction (and the COORDINATES/ROLE_NAME tiers behind an OS accessibility
API instead of the DOM) while schema.py, locator.py's tier semantics, and everything
downstream stay the same.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from playwright.sync_api import Frame, Page

from .schema import Locator, LocatorKind, LocatorTier

_EXTRACT_JS = r"""
() => {
  function role(el) {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === 'a' && el.hasAttribute('href')) return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'input') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (t === 'submit' || t === 'button') return 'button';
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      return 'textbox';
    }
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    return 'generic';
  }
  function accessibleName(el) {
    const aria = el.getAttribute('aria-label');
    if (aria) return aria.trim();
    const labelledBy = el.getAttribute('aria-labelledby');
    if (labelledBy) {
      const t = labelledBy.split(/\s+/).map(id => (document.getElementById(id) || {}).innerText || '').join(' ').trim();
      if (t) return t;
    }
    if (el.labels && el.labels.length) return Array.from(el.labels).map(l => l.innerText).join(' ').trim();
    if (el.tagName === 'INPUT') return (el.getAttribute('placeholder') || el.value || '').trim();
    return (el.innerText || el.value || '').trim().slice(0, 120);
  }
  function cssPath(el) {
    if (el.id) return '#' + CSS.escape(el.id);
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && parts.length < 6) {
      let sel = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children).filter(c => c.tagName === node.tagName);
        if (siblings.length > 1) sel += `:nth-of-type(${siblings.indexOf(node) + 1})`;
      }
      parts.unshift(sel);
      node = parent;
    }
    return parts.join(' > ');
  }
  const selector = 'button, a[href], input, select, textarea, [role=button], [onclick], [data-test], [data-testid]';
  const els = Array.from(document.querySelectorAll(selector));
  return els.map(el => {
    const r = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    const visible = r.width > 0 && r.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
    return {
      tag: el.tagName.toLowerCase(),
      role: role(el),
      name: accessibleName(el),
      testId: el.getAttribute('data-test') || el.getAttribute('data-testid') || null,
      id: el.id || null,
      inputType: el.tagName === 'INPUT' ? (el.getAttribute('type') || 'text') : null,
      visible,
      x: r.x + r.width / 2,
      y: r.y + r.height / 2,
      cssPath: cssPath(el),
      disabled: !!el.disabled,
    };
  }).filter(e => e.visible && !e.disabled);
}
"""

_PAGE_TEXT_JS = """
() => document.body ? document.body.innerText.slice(0, 4000) : ''
"""


@dataclass
class ElementInfo:
    ref: str
    tag: str
    role: str
    name: str
    frame_path: list[str]
    locator: Locator
    raw: dict = field(default_factory=dict)

    def describe(self) -> str:
        bits = [f"[{self.ref}]", self.role]
        if self.name:
            bits.append(f'"{self.name}"')
        if self.raw.get("testId"):
            bits.append(f"test_id={self.raw['testId']}")
        if self.raw.get("inputType"):
            bits.append(f"type={self.raw['inputType']}")
        if self.frame_path:
            bits.append(f"(in frame, depth={len(self.frame_path)})")
        return " ".join(bits)


@dataclass
class Snapshot:
    url: str
    title: str
    visible_text: str
    elements: list[ElementInfo]

    def to_prompt_text(self) -> str:
        lines = [f"URL: {self.url}", f"TITLE: {self.title}", "", "VISIBLE TEXT (truncated):", self.visible_text.strip()[:1500],
                 "", "INTERACTIVE ELEMENTS:"]
        for el in self.elements:
            lines.append(el.describe())
        return "\n".join(lines)

    def by_ref(self, ref: str) -> ElementInfo | None:
        for el in self.elements:
            if el.ref == ref:
                return el
        return None


def _frame_path_for(frame: Frame) -> list[str]:
    path: list[str] = []
    f = frame
    while f.parent_frame is not None:
        parent = f.parent_frame
        handle = f.frame_element()
        idx = parent.evaluate(
            "(el) => Array.from(document.querySelectorAll('iframe,frame')).indexOf(el) + 1",
            handle,
        )
        path.append(f"xpath=(//iframe | //frame)[{idx}]")
        f = parent
    path.reverse()
    return path


def _build_tiers(raw: dict, frame_path: list[str]) -> list[LocatorTier]:
    tiers: list[LocatorTier] = []
    if raw.get("testId"):
        css = f'[data-test="{raw["testId"]}"], [data-testid="{raw["testId"]}"]'
        tiers.append(LocatorTier(kind=LocatorKind.TEST_ID, value=css, frame_path=frame_path, confidence=0.95))
    elif raw.get("id"):
        tiers.append(LocatorTier(kind=LocatorKind.TEST_ID, value=f'#{raw["id"]}', frame_path=frame_path, confidence=0.9))

    if raw.get("role") and raw.get("name"):
        tiers.append(LocatorTier(
            kind=LocatorKind.ROLE_NAME, value=f'{raw["role"]}|{raw["name"]}',
            frame_path=frame_path, confidence=0.75,
        ))

    name = (raw.get("name") or "").strip()
    if name and 0 < len(name) <= 60 and raw.get("tag") != "input":
        tiers.append(LocatorTier(kind=LocatorKind.TEXT, value=name, frame_path=frame_path, confidence=0.55))

    if raw.get("cssPath"):
        tiers.append(LocatorTier(kind=LocatorKind.CSS, value=raw["cssPath"], frame_path=frame_path, confidence=0.35))

    tiers.append(LocatorTier(
        kind=LocatorKind.COORDINATES, value=f'{raw["x"]:.1f},{raw["y"]:.1f}',
        frame_path=[], confidence=0.1,
    ))
    return tiers


def snapshot(page: Page, ref_prefix: str = "e") -> Snapshot:
    elements: list[ElementInfo] = []
    counter = 0
    for frame in page.frames:
        try:
            frame_path = _frame_path_for(frame) if frame != page.main_frame else []
            raws = frame.evaluate(_EXTRACT_JS)
        except Exception:
            continue
        for raw in raws:
            counter += 1
            ref = f"{ref_prefix}{counter}"
            tiers = _build_tiers(raw, frame_path)
            loc = Locator(tiers=tiers, element_role=raw.get("role"), element_label=raw.get("name"))
            elements.append(ElementInfo(
                ref=ref, tag=raw["tag"], role=raw.get("role") or "generic",
                name=raw.get("name") or "", frame_path=frame_path, locator=loc, raw=raw,
            ))

    try:
        text = page.evaluate(_PAGE_TEXT_JS)
    except Exception:
        text = ""

    return Snapshot(url=page.url, title=page.title(), visible_text=text, elements=elements)
