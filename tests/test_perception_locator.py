from src import perception
from src.locator import resolve
from src.schema import LocatorKind


def test_snapshot_finds_login_form_controls(page):
    snap = perception.snapshot(page)
    names = {el.name for el in snap.elements}
    assert "Look Up" in names
    assert any(el.tag == "input" for el in snap.elements)


def test_element_with_no_test_hooks_falls_back_past_test_id(page):
    snap = perception.snapshot(page)
    lookup_btn = next(el for el in snap.elements if el.name == "Look Up")
    kinds = [t.kind for t in lookup_btn.locator.tiers]
    assert LocatorKind.TEST_ID not in kinds  # no id/data-test on this button in the fixture
    assert LocatorKind.ROLE_NAME in kinds
    assert LocatorKind.TEXT in kinds
    assert LocatorKind.COORDINATES in kinds  # always present as last resort


def test_resolve_clicks_via_role_name_tier_and_drives_the_flow(page):
    snap = perception.snapshot(page)
    member_id = next(el for el in snap.elements if el.tag == "input")
    resolved = resolve(page, member_id.locator)
    resolved.playwright_locator.fill("12345")

    lookup_btn = next(el for el in snap.elements if el.name == "Look Up")
    resolved_btn = resolve(page, lookup_btn.locator)
    resolved_btn.playwright_locator.click()

    assert "Balance: $4,532.10" in page.inner_text("#view-result")


def test_iframe_element_is_discovered_with_frame_path_and_is_clickable(page):
    snap = perception.snapshot(page)
    iframe_btn = next((el for el in snap.elements if el.name == "Show Branch Hours"), None)
    assert iframe_btn is not None
    assert len(iframe_btn.frame_path) == 1  # one level of nesting

    resolved = resolve(page, iframe_btn.locator)
    resolved.playwright_locator.click()

    assert "Mon-Fri 9-5" in page.frames[1].inner_text("body")
