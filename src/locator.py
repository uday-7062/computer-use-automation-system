"""Resolves a schema.Locator (a ranked fallback chain) against a live Playwright page.

Tier semantics (how `LocatorTier.value` is interpreted, chosen at capture time in
perception.py and replayed identically here -- this is the seam that keeps replay
deterministic and independent of the LLM):

  TEST_ID      CSS selector targeting a data-test/data-testid/id attribute.
  CSS          A structural CSS selector (nth-of-type path). Most brittle to UI drift.
  XPATH        An XPath expression (no 'xpath=' prefix; added here).
  ROLE_NAME    "<aria role>|<accessible name>" -- resolved via get_by_role, which is
               backed by the accessibility tree, so it keeps working even with no DOM
               test hooks and often survives markup rewrites that keep the same UI.
  TEXT         Visible text substring, resolved via get_by_text.
  COORDINATES  "x,y" in the page's viewport space at capture time. Last resort, main
               frame only -- used when nothing else in the surface is addressable.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Union

from playwright.sync_api import FrameLocator, Locator as PWLocator, Page

from .schema import Locator, LocatorKind, LocatorTier

Scope = Union[Page, FrameLocator]


class LocatorResolutionError(Exception):
    def __init__(self, message: str, tiers_tried: list[str]):
        super().__init__(message)
        self.tiers_tried = tiers_tried


def _enter_frames(page: Page, frame_path: list[str]) -> Scope:
    scope: Scope = page
    for sel in frame_path:
        scope = scope.frame_locator(sel)
    return scope


def _tier_to_playwright(scope: Scope, tier: LocatorTier) -> PWLocator | None:
    if tier.kind == LocatorKind.TEST_ID:
        return scope.locator(tier.value)
    if tier.kind == LocatorKind.CSS:
        return scope.locator(tier.value)
    if tier.kind == LocatorKind.XPATH:
        return scope.locator(f"xpath={tier.value}")
    if tier.kind == LocatorKind.ROLE_NAME:
        role, _, name = tier.value.partition("|")
        return scope.get_by_role(role, name=name, exact=False)  # type: ignore[arg-type]
    if tier.kind == LocatorKind.TEXT:
        return scope.get_by_text(tier.value, exact=False)
    return None  # COORDINATES has no Playwright Locator equivalent


@dataclass
class ResolvedElement:
    tier: LocatorTier
    playwright_locator: PWLocator | None  # None means COORDINATES -- use .point instead
    point: tuple[float, float] | None = None


def resolve(page: Page, locator: Locator, timeout_ms: int = 8000) -> ResolvedElement:
    """Try each tier in order; return the first that resolves to a visible element.

    The first (most-robust) tier gets the full timeout, since it is usually correct
    and this is the call that has to tolerate an in-flight navigation or a slow
    render -- a plain `.count()` check would race the page and fail spuriously right
    after a click that triggers a navigation. Later tiers are fallbacks for when the
    top tier is genuinely wrong (drift), so they get a short timeout each rather than
    each waiting the full budget.
    """
    tried: list[str] = []
    for i, tier in enumerate(locator.tiers):
        tried.append(f"{tier.kind.value}:{tier.value}")
        tier_timeout = timeout_ms if i == 0 else min(timeout_ms, 2000)
        try:
            if tier.kind == LocatorKind.COORDINATES:
                x_str, y_str = tier.value.split(",")
                return ResolvedElement(tier=tier, playwright_locator=None,
                                        point=(float(x_str), float(y_str)))

            scope = _enter_frames(page, tier.frame_path)
            pw_loc = _tier_to_playwright(scope, tier)
            if pw_loc is None:
                continue
            candidate = pw_loc.first
            candidate.wait_for(state="visible", timeout=tier_timeout)
            return ResolvedElement(tier=tier, playwright_locator=candidate)
        except Exception:
            continue
    raise LocatorResolutionError(
        f"no locator tier resolved for element (label={locator.element_label!r})", tried
    )
