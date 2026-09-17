#!/usr/bin/env python3
"""Builds a small, hand-authored artifact used ONLY to demonstrate the RISKY-step /
human-escalation replay path end to end (see evidence/ and REPORT.md section 5).

This is NOT the main deliverable capability -- that one (add_item_to_cart_and_reach_
checkout_review) comes from the real LLM discovery run via `python -m src.cli discover`.
This one exists because the discovery run is instructed to stop *before* the
irreversible "Finish" button precisely so a human has to approve it; to produce real
evidence of that approval path firing, we replay this artifact, which continues one
step further and marks that step RISKY, and resolve the resulting escalation with
scripts/operator_finish_confirmation.json via operator_cli.py.

Locators below were confirmed against the live app on saucedemo.com immediately
before authoring this file (see project history) rather than guessed.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.schema import (  # noqa: E402
    ActionType, Artifact, Checkpoint, ErrorClass, ErrorHandler, Locator, LocatorKind,
    LocatorTier, Output, Param, ParamType, Provenance, RiskLevel, Step, TargetApp,
)

ROOT = Path(__file__).resolve().parent.parent


def css(value: str, confidence: float = 0.95) -> Locator:
    return Locator(tiers=[LocatorTier(kind=LocatorKind.TEST_ID, value=value, confidence=confidence)])


def step(id_, action, test_id=None, value=None, risk=RiskLevel.SAFE, notes=None, extract_as=None):
    loc = css(f'[data-test="{test_id}"]') if test_id else None
    return Step(id=id_, action=action, locator=loc, value_template=value, risk_level=risk,
                notes=notes, extract_as=extract_as)


ARTIFACT = Artifact(
    id="checkout_finish_demo",
    version=1,
    name="[DEMO] Complete checkout and place order",
    description=(
        "Escalation-mechanism demo only (see module docstring): logs in, adds an item, "
        "fills checkout info, and places the order via the irreversible Finish button. "
        "That final step is RISKY and requires human confirmation via the live-session "
        "handoff (src/escalation.py) before it is executed."
    ),
    target=TargetApp(
        base_url="https://www.saucedemo.com/",
        allowed_domains=["www.saucedemo.com", "saucedemo.com"],
        vendor_product="Swag Labs demo e-commerce app (saucedemo)",
        surface="web",
    ),
    params=[
        Param(name="username", type=ParamType.STRING, description="Login username.", example="standard_user"),
        Param(name="password", type=ParamType.STRING, sensitive=True, description="Login password.", example="secret_sauce"),
        Param(name="first_name", type=ParamType.STRING, description="Shipping first name.", example="Jane"),
        Param(name="last_name", type=ParamType.STRING, description="Shipping last name.", example="Doe"),
        Param(name="zip_code", type=ParamType.STRING, description="Shipping zip.", example="94107"),
    ],
    outputs=[Output(name="confirmation_text", type=ParamType.STRING, description="Order confirmation banner text.")],
    steps=[
        step("fill_username", ActionType.FILL, "username", "{{username}}"),
        step("fill_password", ActionType.FILL, "password", "{{password}}"),
        step("click_login", ActionType.CLICK, "login-button"),
        step("add_to_cart", ActionType.CLICK, "add-to-cart-sauce-labs-backpack"),
        step("open_cart", ActionType.CLICK, "shopping-cart-link"),
        step("click_checkout", ActionType.CLICK, "checkout"),
        step("fill_first_name", ActionType.FILL, "firstName", "{{first_name}}"),
        step("fill_last_name", ActionType.FILL, "lastName", "{{last_name}}"),
        step("fill_zip", ActionType.FILL, "postalCode", "{{zip_code}}"),
        step("click_continue", ActionType.CLICK, "continue"),
        Step(
            id="click_finish", action=ActionType.CLICK,
            locator=Locator(
                tiers=[LocatorTier(kind=LocatorKind.TEST_ID, value='[data-test="finish"]', confidence=0.95)],
                element_label="Finish",
            ),
            risk_level=RiskLevel.RISKY,
            notes="Places the order -- irreversible on a real system. Gated by guardrails.yaml "
                  "(risky_click_text_patterns: 'finish' -> require_confirmation) and, independently, "
                  "by this step's own risk_level, so the replay engine escalates to a human before acting "
                  "even if the click-text policy were ever loosened.",
        ),
        step("extract_confirmation", ActionType.EXTRACT, extract_as="confirmation_text"),
    ],
    checkpoint=Checkpoint(
        description="Order confirmation page reached",
        url_pattern=r"checkout-complete\.html",
        text_pattern=r"Thank you for your order!",
    ),
    error_handlers=[
        ErrorHandler(
            code="CHECKOUT_VALIDATION_ERROR",
            description="A required checkout field (first name / last name / zip) was missing or invalid.",
            detect=Locator(tiers=[LocatorTier(kind=LocatorKind.TEST_ID, value='[data-test="error"]', confidence=0.95)]),
            error_class=ErrorClass.BUSINESS_OUTCOME,
        )
    ],
    provenance=Provenance(discovery_run_id="hand_authored_demo", model="n/a (hand-authored)",
                           goal="demonstrate the RISKY-step human-confirmation replay path"),
)

# extract_confirmation needs a real locator (Step() helper above left it None for brevity).
# Confirmed live: <h2 data-test="complete-header">Thank you for your order!</h2>
ARTIFACT.steps[-1].locator = Locator(tiers=[
    LocatorTier(kind=LocatorKind.TEST_ID, value='[data-test="complete-header"]', confidence=0.95),
    LocatorTier(kind=LocatorKind.TEXT, value="Thank you for your order!", confidence=0.6),
])

if __name__ == "__main__":
    out = ROOT / "artifacts" / f"{ARTIFACT.artifact_key()}.json"
    out.write_text(ARTIFACT.model_dump_json(indent=2))
    print(f"wrote {out}")
