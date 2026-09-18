# Computer-Use Automation System

A small, real implementation of the interface.ai take-home: an LLM drives a live
browser to accomplish a goal once ("discovery"), the successful run is compiled into
a typed, versioned **capability artifact**, and that artifact is then **replayed
deterministically** — no LLM in the loop — with structured error handling, an
allowlist/guardrail layer, and a human-escalation path that can take over the live
browser session mid-run.

Target application: **[saucedemo.com](https://www.saucedemo.com/)**, a public demo
e-commerce app explicitly suggested as a proxy target by the assignment brief
("add a specific item to the cart and reach the checkout review page"). No real
credentials, PII, or production system is touched — saucedemo publishes its own test
credentials for exactly this purpose.

See **[REPORT.md](REPORT.md)** for the design write-up (architecture, artifact
schema, determinism/error handling, heterogeneity & multi-tenant story, escalation
model, safety guardrails, and cuts).

## Project layout

```
src/
  schema.py          typed artifact/capability contract (pydantic)
  perception.py       DOM/accessibility snapshot -> ranked, multi-tier locators
  locator.py           resolves a Locator against a live page, with fallback
  guardrails.py + guardrails.yaml   allowlist, risk policy, PII redaction
  llm_agent.py         discovery loop (LLM observe/decide/act -> Artifact)
  replay_engine.py     deterministic replay executor + error taxonomy
  escalation.py         human-in-the-loop control transfer (CDP-based)
  evidence.py            structured, redacted run logging + screenshots
  cli.py                  entry point (discover / replay / show)
operator_cli.py       standalone "operator console" (mocked UI, real handoff)
artifacts/              saved capability artifacts (the reusable output)
evidence/                per-run logs, screenshots, results (see below)
tests/                    offline test suite (no network / API key required)
```

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
playwright install chromium

cp .env.example .env
# edit .env and set OPENAI_API_KEY=sk-...
# optional: OPENAI_MODEL=gpt-4o (default) or another vision + tool-calling model
# your key has access to
```

## Running without live services (offline)

The full test suite runs against a local, static HTML fixture
(`tests/fixtures/legacy_app/`) over a `file://` URL — no network access and no API
key required. This fixture is deliberately built with **no `data-test`/`id` hooks**
on some controls and one nested `<iframe>`, to exercise the locator fallback chain
(role+name / text / structural CSS) and frame traversal the way a real legacy
surface would force.

```bash
source venv/bin/activate
python -m pytest -q
```

## Demo path (live)

### 1. Discovery — LLM drives the real browser

```bash
source venv/bin/activate
python -m src.cli discover
# add --headless to run without a visible window; omit to watch it work.
# Override the goal's inputs: --item-name "Sauce Labs Fleece Jacket" etc.
```

This logs in, adds the item to the cart, fills the checkout form, and stops at the
"Checkout: Overview" review page (the brief's own suggested example) — extracting
`item_price` and `order_total` as outputs before finishing. Every observation,
model tool call, and screenshot is written to `evidence/discovery_<timestamp>/`,
and the resulting artifact is saved to
`artifacts/add_item_to_cart_and_reach_checkout_review.v1.json` with
`review_status: "draft"`.

### 2. Build confidence, then approve (stretch goal)

A fresh (`draft`) artifact can't be replayed unattended — `python -m src.cli
replay` refuses by default, to avoid running an unreviewed capability in
production. Build a track record first:

```bash
python -m src.cli stability artifacts/add_item_to_cart_and_reach_checkout_review.v1.json \
  --param username=standard_user --param password=secret_sauce \
  --param item_name="Sauce Labs Backpack" \
  --param first_name=Jane --param last_name=Doe --param zip_code=94107 \
  --runs 5
```

Replays the artifact 5 times and reports a success rate plus, per step, which
locator tier resolved on each run (a step that resolves via a different tier
run-to-run is drifting even on runs that individually report success). Saves
`evidence/stability_<id>_<ts>/report.json`. Then:

```bash
python -m src.cli approve artifacts/add_item_to_cart_and_reach_checkout_review.v1.json \
  --stability-report evidence/stability_<id>_<ts>/report.json
```

Flips `review_status: draft -> approved` only if the report meets
`--min-success-rate` (default 100%). From here on, `replay` runs without any
extra flag. (A one-off supervised run of a draft artifact can bypass this with
`replay ... --allow-draft`, used below for the escalation-demo artifact, which
is intentionally never approved — see step 6.)

### 3. Replay — deterministic, no LLM

```bash
python -m src.cli replay artifacts/add_item_to_cart_and_reach_checkout_review.v1.json \
  --param username=standard_user --param password=secret_sauce \
  --param item_name="Sauce Labs Backpack" \
  --param first_name=Jane --param last_name=Doe --param zip_code=94107
```

Prints a structured result (`success` / `business_outcome` / `hard_failure`) with
outputs, and writes full evidence to `evidence/replay_<timestamp>/`. Try a
different `--param item_name=...` too (e.g. `"Sauce Labs Fleece Jacket"`) — the
capability is genuinely parameterized, not hardcoded to what was recorded.

### 4. Replay hitting a bad-input business outcome

```bash
python -m src.cli replay artifacts/add_item_to_cart_and_reach_checkout_review.v1.json \
  --param username=standard_user --param password=secret_sauce \
  --param item_name="Sauce Labs Backpack" \
  --param first_name="" --param last_name=Doe --param zip_code=94107
```

Saucedemo's checkout form rejects the empty first name; the replay engine detects
the app's own `[data-test="error"]` banner via the artifact's `error_handlers` and
returns `status=business_outcome, outcome_code=CHECKOUT_VALIDATION_ERROR` — a
legitimate result the caller needs, not a crash. See REPORT.md §3.

### 5. Replay hitting a genuine hard failure

```bash
python -m src.cli replay artifacts/add_item_to_cart_and_reach_checkout_review.v1.json \
  --param username=standard_user --param password=secret_sauce \
  --param item_name="Nonexistent Product XYZ" \
  --param first_name=Jane --param last_name=Doe --param zip_code=94107
```

No such product exists, so every locator tier for the "Add to cart" click
correctly fails to resolve, and replay reports `status=hard_failure` with the
failed step, and what was expected vs. what was tried. See REPORT.md §3 for a
real bug this caught during development (an earlier version silently clicked
the *wrong* product instead of failing).

### 6. Human escalation / live-session handoff

```bash
python -m src.cli replay artifacts/checkout_finish_demo.v1.json --allow-draft \
  --param username=standard_user --param password=secret_sauce \
  --param first_name=Jane --param last_name=Doe --param zip_code=94107
```

`checkout_finish_demo.v1.json` is a small hand-authored artifact (see
`scripts/build_escalation_demo_artifact.py`) that continues one step further than
the main capability and clicks the irreversible "Finish" button — marked `RISKY`.
It's intentionally never approved (`--allow-draft` is required every time) since
it exists purely to demonstrate the escalation mechanism, not as a production
capability. The replay engine pauses, writes `evidence/<run>/intervention_request.json`,
and prints the live CDP endpoint. In a second terminal:

```bash
python operator_cli.py                      # interactive: type commands yourself, or
python operator_cli.py --script evidence/operator_scripts/confirm_finish.json   # scripted
```

`operator_cli.py` attaches to the **same live browser session** over Chrome
DevTools Protocol (not a fresh one), lets the operator act, and signals resume.
The paused replay process then verifies the outcome and finishes. See REPORT.md §5.

## Config

`src/guardrails.yaml` is the single allowlist/policy file both discovery and replay
read: allowed domains, allowed action types, which click text patterns are
`risky` (`block` / `require_confirmation` / `flag`), and PII redaction patterns
applied to every log line and stored artifact.

## What's mocked, and why

- **Operator console** is a CLI (`operator_cli.py`), not a co-browsing UI — explicitly
  out of scope per the brief. The control-transfer mechanism it exercises (attach to
  the live session over CDP, pause/resume signaling, action logging) is real.
- **Multi-tenant / desktop surfaces** are design-only (REPORT.md §4), not built — the
  brief asks for a credible design story here, not an implementation.

See REPORT.md §7 for the full list of cuts and what's next.
