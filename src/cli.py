"""Entry point. See README.md for the exact demo commands.

    python -m src.cli discover                          # run the LLM discovery pass
    python -m src.cli replay artifacts/<file>.json --param k=v ...
    python -m src.cli show artifacts/<file>.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")

from .guardrails import Guardrails
from .llm_agent import DiscoveryAgent, DiscoveryConfig
from .replay_engine import ReplayEngine
from .schema import Artifact, ErrorClass, ErrorHandler, Locator, LocatorKind, LocatorTier, Output, Param, ParamType, RecoveryAction

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS_DIR = ROOT / "artifacts"

CAPABILITY_ID = "add_item_to_cart_and_reach_checkout_review"
TARGET_BASE_URL = "https://www.saucedemo.com/"

DEFAULT_PARAMS = [
    Param(name="username", type=ParamType.STRING, required=True,
          description="Login username.", example="standard_user"),
    Param(name="password", type=ParamType.STRING, required=True, sensitive=True,
          description="Login password. Never written to logs/artifacts.", example="secret_sauce"),
    Param(name="item_name", type=ParamType.STRING, required=True,
          description="Exact product name to add to the cart.", example="Sauce Labs Backpack"),
    Param(name="first_name", type=ParamType.STRING, required=True,
          description="Shipping form first name.", example="Jane"),
    Param(name="last_name", type=ParamType.STRING, required=True,
          description="Shipping form last name.", example="Doe"),
    Param(name="zip_code", type=ParamType.STRING, required=True,
          description="Shipping form postal code.", example="94107"),
]

DEFAULT_OUTPUTS = [
    Output(name="item_price", type=ParamType.STRING, description="Listed price of the added item, e.g. '$29.99'."),
    Output(name="order_total", type=ParamType.STRING, description="Final total shown on the review page, e.g. 'Total: $32.39'."),
]

GOAL = (
    # Deliberately does NOT interpolate {password} (or any other Param.sensitive value):
    # this string is stored verbatim in Artifact.provenance.goal and logged to evidence,
    # so a sensitive value embedded here would defeat Param.sensitive's whole purpose.
    # The model still receives the real credential -- via the params listing built fresh
    # into the system prompt each turn (DiscoveryAgent._system_prompt), which is sent to
    # the LLM API but never itself written to disk.
    "Log in to Swag Labs with username '{username}' using the password given in the "
    "declared parameters below. "
    "Add the product named exactly '{item_name}' to the cart. Go to the cart and click Checkout. "
    "On the 'Checkout: Your Information' form, fill First Name='{first_name}', Last Name='{last_name}', "
    "Zip/Postal Code='{zip_code}', then click Continue. You should land on the 'Checkout: Overview' page. "
    "Extract the item's listed price into output 'item_price' and the final 'Total: $X' line into output "
    "'order_total'. Then finish -- do not click the final 'Finish' button, reaching the Overview/review page "
    "with both outputs extracted is the goal."
)


def _known_error_handlers() -> list[ErrorHandler]:
    """Hand-verified against the live app (see project notes): checkout-step-one's
    required-field validation banner. A discovery run only ever sees the happy path,
    so this taxonomy entry is authored from direct inspection of the real error state,
    not guessed -- exactly the kind of app-specific runtime condition Section 3.3 asks
    the replay contract to classify deliberately instead of surfacing as a raw timeout.
    """
    detect = Locator(
        tiers=[
            LocatorTier(kind=LocatorKind.TEST_ID, value='[data-test="error"]', confidence=0.95),
            LocatorTier(kind=LocatorKind.CSS, value='.error-message-container h3', confidence=0.6),
        ],
        element_role="alert", element_label="checkout validation error banner",
    )
    return [
        ErrorHandler(
            code="CHECKOUT_VALIDATION_ERROR",
            description="A required checkout field (first name / last name / zip) was missing or invalid.",
            detect=detect,
            error_class=ErrorClass.BUSINESS_OUTCOME,
            recovery=RecoveryAction.NONE,
        )
    ]


def cmd_discover(args: argparse.Namespace) -> None:
    guardrails = Guardrails()
    params = DEFAULT_PARAMS
    values = {
        "username": args.username, "password": args.password, "item_name": args.item_name,
        "first_name": args.first_name, "last_name": args.last_name, "zip_code": args.zip_code,
    }
    cfg = DiscoveryConfig(
        goal=GOAL.format(**values),
        capability_id=CAPABILITY_ID,
        capability_name="Add item to cart and reach checkout review",
        capability_description=(
            "Logs in, adds a named product to the cart, fills the shipping/checkout-info form, "
            "and reaches the Checkout: Overview review page, returning the item price and order total."
        ),
        target_base_url=TARGET_BASE_URL,
        params=params,
        param_values=values,
        declared_outputs=DEFAULT_OUTPUTS,
        vendor_product="Swag Labs demo e-commerce app (saucedemo)",
        max_steps=args.max_steps,
        headless=args.headless,
        model=args.model,
    )
    agent = DiscoveryAgent(guardrails)
    artifact, result = agent.run(cfg)

    print(f"\nDiscovery result: {result.status.value} -- {result.message}")
    if artifact is None:
        sys.exit(1)

    artifact.error_handlers = _known_error_handlers()
    ARTIFACTS_DIR.mkdir(exist_ok=True)
    out_path = ARTIFACTS_DIR / f"{artifact.artifact_key()}.json"
    out_path.write_text(artifact.model_dump_json(indent=2))
    print(f"Artifact saved to {out_path.relative_to(ROOT)}")
    print(f"Steps recorded: {len(artifact.steps)}")


def cmd_replay(args: argparse.Namespace) -> None:
    from .evidence import EvidenceWriter

    artifact = Artifact.model_validate_json(Path(args.artifact).read_text())
    guardrails = Guardrails()
    params = dict(kv.split("=", 1) for kv in args.param)

    evidence = EvidenceWriter("replay", guardrails)
    engine = ReplayEngine(artifact=artifact, guardrails=guardrails, headless=args.headless,
                           auto_approve_risky=args.auto_approve_risky)
    result = engine.run(params, evidence)

    print(f"\nReplay result: {result.status.value}")
    print(f"  message: {result.message}")
    if result.outputs:
        print(f"  outputs: {result.outputs}")
    if result.failed_step_id:
        print(f"  failed_step_id: {result.failed_step_id}")
        print(f"  expected: {result.expected}")
        print(f"  observed: {result.observed}")
    print(f"  evidence: {result.evidence_dir}")
    sys.exit(0 if result.status.value in ("success", "business_outcome") else 2)


def cmd_show(args: argparse.Namespace) -> None:
    artifact = Artifact.model_validate_json(Path(args.artifact).read_text())
    print(json.dumps(artifact.model_dump(mode="json"), indent=2))


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m src.cli")
    sub = ap.add_subparsers(dest="command", required=True)

    d = sub.add_parser("discover", help="Run the LLM-driven discovery loop and save a capability artifact.")
    d.add_argument("--username", default="standard_user")
    d.add_argument("--password", default="secret_sauce")
    d.add_argument("--item-name", default="Sauce Labs Backpack")
    d.add_argument("--first-name", default="Jane")
    d.add_argument("--last-name", default="Doe")
    d.add_argument("--zip-code", default="94107")
    d.add_argument("--max-steps", type=int, default=25)
    d.add_argument("--headless", action="store_true")
    d.add_argument("--model", default=None)
    d.set_defaults(func=cmd_discover, model=None)

    r = sub.add_parser("replay", help="Deterministically replay a saved artifact.")
    r.add_argument("artifact")
    r.add_argument("--param", action="append", default=[], help="key=value, repeatable")
    r.add_argument("--headless", action="store_true", default=True)
    r.add_argument("--headed", dest="headless", action="store_false")
    r.add_argument("--auto-approve-risky", action="store_true")
    r.set_defaults(func=cmd_replay)

    s = sub.add_parser("show", help="Pretty-print a saved artifact.")
    s.add_argument("artifact")
    s.set_defaults(func=cmd_show)

    args = ap.parse_args()
    if getattr(args, "model", None) is None and args.command == "discover":
        from .llm_agent import DEFAULT_MODEL
        args.model = DEFAULT_MODEL
    args.func(args)


if __name__ == "__main__":
    main()
