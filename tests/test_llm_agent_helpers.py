"""Unit tests for llm_agent.py's pure, browser-free logic.

These specifically cover the two real bugs found and fixed while producing the
live discovery evidence in this repo: a click's locator not being templated by
its parameter (so replay always re-clicked the originally-discovered element
regardless of what a caller asked for), and unbounded conversation-history growth
that blew through this account's rate limit around turn 17 of a real run.
"""
from src.llm_agent import _compact_old_observations, _parameterize_locator
from src.schema import Locator, LocatorKind, LocatorTier


def _tier_kinds(locator: Locator) -> list[LocatorKind]:
    return [t.kind for t in locator.tiers]


def test_parameterize_locator_templates_tier_containing_literal_value():
    loc = Locator(tiers=[
        LocatorTier(kind=LocatorKind.TEST_ID, value='[data-test="Sauce Labs Backpack"]', confidence=0.95),
    ])
    out = _parameterize_locator(loc, "item_name", "Sauce Labs Backpack")
    assert out.tiers[0].value == '[data-test="{{item_name}}"]'


def test_parameterize_locator_templates_tier_containing_slug():
    loc = Locator(tiers=[
        LocatorTier(kind=LocatorKind.TEST_ID, value='[data-test="add-to-cart-sauce-labs-backpack"]', confidence=0.95),
    ])
    out = _parameterize_locator(loc, "item_name", "Sauce Labs Backpack")
    assert out.tiers[0].value == '[data-test="add-to-cart-{{item_name|slug}}"]'


def test_parameterize_locator_drops_ambiguous_tiers_including_coordinates():
    # This is the exact shape of a real bug caught live in this repo's own
    # evidence: a role/text tier shared by every product's "Add to cart" button,
    # AND the coordinates tier, are both unsafe to keep once the element is
    # meant to vary by item_name -- coordinates in particular doesn't check
    # identity at all, so it silently clicked the wrong (originally-discovered)
    # product and reported success when a requested item_name didn't exist,
    # instead of the correct hard_failure. Only tiers that were actually
    # templated to the parameter may survive.
    loc = Locator(tiers=[
        LocatorTier(kind=LocatorKind.TEST_ID, value='[data-test="add-to-cart-sauce-labs-backpack"]', confidence=0.95),
        LocatorTier(kind=LocatorKind.ROLE_NAME, value="button|Add to cart", confidence=0.75),
        LocatorTier(kind=LocatorKind.TEXT, value="Add to cart", confidence=0.55),
        LocatorTier(kind=LocatorKind.COORDINATES, value="500.0,356.0", confidence=0.1),
    ])
    out = _parameterize_locator(loc, "item_name", "Sauce Labs Backpack")
    kinds = _tier_kinds(out)
    assert LocatorKind.ROLE_NAME not in kinds
    assert LocatorKind.TEXT not in kinds
    assert LocatorKind.COORDINATES not in kinds
    assert kinds == [LocatorKind.TEST_ID]


def test_parameterize_locator_falls_back_to_original_tiers_if_nothing_matches():
    # Defensive case: if the param value/slug appears nowhere (shouldn't happen
    # for a correctly-tagged click, but must not silently produce an empty,
    # unresolvable locator).
    loc = Locator(tiers=[LocatorTier(kind=LocatorKind.CSS, value="#unrelated", confidence=0.5)])
    out = _parameterize_locator(loc, "item_name", "Sauce Labs Backpack")
    assert len(out.tiers) == 1
    assert out.tiers[0].value == "#unrelated"


def test_parameterize_locator_does_not_touch_fill_field_identity():
    # The other half of the same bug: a fill target's OWN locator (e.g. the
    # username textbox) must never be templated by the *typed* value, since the
    # field's identity has nothing to do with what gets typed into it. Because
    # none of this locator's content-based tiers mention "standard_user" (nor
    # its slug), and COORDINATES is never kept for a parameterized call, nothing
    # matches -- the "nothing matched" fallback returns the tiers untouched.
    # llm_agent.py's _handle_act_like still gates this call on action == "click"
    # so a fill's locator is never even passed through here in practice.
    username_field_locator = Locator(tiers=[
        LocatorTier(kind=LocatorKind.TEST_ID, value='[data-test="username"]', confidence=0.95),
        LocatorTier(kind=LocatorKind.CSS, value="#user-name", confidence=0.35),
        LocatorTier(kind=LocatorKind.COORDINATES, value="640.0,173.5", confidence=0.1),
    ])
    out = _parameterize_locator(username_field_locator, "username", "standard_user")
    assert out == username_field_locator  # untouched, via the "nothing matched" fallback


def test_compact_old_observations_keeps_last_two_in_full():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": [{"type": "text", "text": "OBSERVATION (turn 0):\nurl a"}, {"type": "image_url", "image_url": {}}]},
        {"role": "assistant", "content": "..."},
        {"role": "user", "content": [{"type": "text", "text": "OBSERVATION (turn 1):\nurl b"}, {"type": "image_url", "image_url": {}}]},
        {"role": "assistant", "content": "..."},
        {"role": "user", "content": [{"type": "text", "text": "OBSERVATION (turn 2):\nurl c"}, {"type": "image_url", "image_url": {}}]},
    ]
    _compact_old_observations(messages)

    assert isinstance(messages[1]["content"], str)
    assert "trimmed" in messages[1]["content"]
    assert isinstance(messages[3]["content"], list)  # second-to-last observation: kept in full
    assert isinstance(messages[5]["content"], list)  # most recent observation: kept in full


def test_compact_old_observations_is_idempotent_across_repeated_calls():
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "OBSERVATION (turn 0):\nurl a"}, {"type": "image_url", "image_url": {}}]},
        {"role": "user", "content": [{"type": "text", "text": "OBSERVATION (turn 1):\nurl b"}, {"type": "image_url", "image_url": {}}]},
        {"role": "user", "content": [{"type": "text", "text": "OBSERVATION (turn 2):\nurl c"}, {"type": "image_url", "image_url": {}}]},
    ]
    _compact_old_observations(messages)
    _compact_old_observations(messages)  # simulate being called again next turn

    assert isinstance(messages[0]["content"], str)
    assert isinstance(messages[1]["content"], list)
    assert isinstance(messages[2]["content"], list)
