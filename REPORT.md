# Report

## 1. Architecture

The system has four stages that share exactly two things: a `Guardrails` instance
and an `Artifact`/`RunResult` contract (`src/schema.py`).

```
 goal + params            LLM discovery loop            capability artifact
 ──────────────► src/llm_agent.py ───────────────► artifacts/<id>.v<N>.json
                    │  observe (src/perception.py)          │
                    │  decide  (OpenAI, tool-calling)        │  no LLM from here on
                    │  act     (Playwright)                   ▼
                    │                                deterministic replay
                    └─ escalate ─► src/escalation.py ◄── src/replay_engine.py
                                    (human takes the             │
                                     same live session)   structured RunResult
                                                          (success / business_
                                                           outcome / hard_failure)
```

**Single process, synchronous, no queue.** Both discovery and replay are one
Python process driving one Playwright browser. The brief explicitly discourages
building scaling infrastructure ("queues, clusters... prematurely building that
infrastructure is not [rewarded]"); a real deployment would put replay behind a
worker pool per Section 3.7's multi-tenant story, but that's future infrastructure,
not core logic, so it's not here.

**Perception is DOM/accessibility-based, not screenshot-coordinate-only.**
`src/perception.py` extracts every interactive element via `querySelectorAll` plus
role/accessible-name computation (the same signals a screen reader uses), across
all frames (including classic `<frame>`/`<frameset>` legacy layouts, not just
`<iframe>`). A screenshot is *also* sent to the model each turn for grounding, but
the model always acts by referencing a stable element ref from the structured
observation, never by inventing a selector or clicking raw pixels. This was the
central bias called for in Section 3.1 ("bias toward an approach that would still
work when the surface has no clean DOM"): coordinates are kept only as the last
fallback tier, not the primary mechanism.

**The same locator-resolution code path serves discovery and replay.**
`src/locator.py`'s `resolve()` is called by `llm_agent.py` while executing the
model's chosen action *and* by `replay_engine.py` while executing a saved step.
This is deliberate: the robustness of a locator is proven the moment it's first
used, not asserted after the fact, and there is exactly one implementation of
"how do we find this element" to keep correct.

**Guardrails is the one required dependency of every execution path.**
`src/guardrails.py` loads `guardrails.yaml` (domain allowlist, action allowlist,
risky-click policy, PII redaction patterns) and is threaded through discovery,
replay, and evidence logging. Nothing writes to disk or touches the page without
passing through it first — see §6.

**Key trade-off:** I chose accessibility/DOM extraction over a pure vision
computer-use loop (raw screenshot + click coordinates only). Vision-only is more
surface-agnostic in theory, but coordinates are the *least* durable locator in an
enterprise app where window chrome, zoom, and minor layout shifts are common, and
they carry zero semantic information into the artifact for a human reviewer to
audit ("click at 412,588" vs. "click the button with role=button, name='Finish'").
For a system whose stated purpose is producing *reviewable, reusable* capabilities,
that legibility mattered more than the marginal generality vision-only would add —
and COORDINATES is still in the tier chain as the true last resort.

**LLM provider:** OpenAI (`gpt-4o` by default, overridable via `OPENAI_MODEL`),
via `chat.completions` function-calling with image input for grounding. Provider
choice is explicitly left open by the brief; the provider-specific surface is
isolated to `_TOOLS` and `DiscoveryAgent._call_model()` in `src/llm_agent.py`,
which normalize a response into a provider-agnostic `ToolUse(id, name, input)` —
everything else (perception, guardrails, artifact compilation) doesn't know or
care which vendor answered.

## 2. Artifact schema

`src/schema.py`'s `Artifact` is the reusable capability contract. Design goals, in
order: **(a)** a calling AI agent can invoke it correctly from `params`/`outputs`
alone, without reading the steps; **(b)** a human reviewer can audit *why* each
locator should keep working, not just *what* it targets; **(c)** it degrades
gracefully instead of crashing when the world doesn't cooperate.

- **`Locator` is a ranked list of `LocatorTier`s, not a single selector.** Each
  tier has a `kind` (`test_id > role_name > text > css > xpath > coordinates`,
  roughly most-to-least durable) and a `confidence`. Replay tries tiers in order
  and uses the first that resolves — so a capability recorded against an app that
  *has* `data-test` hooks today keeps working if a future version drops them and
  only the accessible name survives, without a re-recording. This is the single
  most consequential decision in the schema: it's what makes "record once, replay
  many, survive drift" plausible for the legacy surfaces Section 1 describes,
  instead of a single brittle CSS path per step.
- **Locator tier values can themselves be templated** (`{{param}}`, or
  `{{param|slug}}` for the lower-cased/hyphenated form apps commonly derive
  `data-test`/id attributes from). This exists because of a real bug I hit and
  fixed during development: a `param_name`-tagged click (e.g. "Add to cart" for
  a caller-supplied `item_name`) only templated `Step.value_template`, which is
  meaningless for a click — the *locator* is what determines which of six
  "Add to cart" buttons gets pressed, and it was still hardcoded to whichever
  product the model happened to click during discovery, so replaying with a
  different `item_name` silently added the wrong item. The fix templates the
  locator itself wherever the discovery-time element's identity textually
  contained the parameter's value or slug, and **drops** any fallback tier that
  doesn't (e.g. a generic `role=button, name="Add to cart"` tier shared by every
  product) rather than keep it as a false fallback that could silently resolve
  to the wrong element if the top tier ever drifted. It deliberately does *not*
  apply to fill/select/press_key: there the parameter is the value being typed,
  not what selects the target field, and templating the locator there would
  strip a perfectly good, unrelated field locator down to nothing. Verified live:
  the recorded `add_item_to_cart_and_reach_checkout_review` capability was
  discovered against "Sauce Labs Backpack" and replays correctly against
  "Sauce Labs Fleece Jacket" (`evidence/replay_*` — different price/total
  extracted, same steps, zero re-recording).
- **`Step.notes` captures *why* a locator was chosen**, written at the moment of
  the discovery action (`llm_agent.py` asks the model for one sentence of
  reasoning per action). A reviewer approving a `draft` artifact for production use
  can see the robustness argument, not just infer it.
- **`params`/`outputs` are typed and separate from `steps`.** An agent calling this
  capability only needs the contract (§3.2 of the brief calls this out explicitly:
  "a clear contract, not just a step list"). `Param.sensitive` marks fields (e.g. a
  password) that must never be written to the stored artifact trace or any log —
  enforced in `evidence.py`/`guardrails.py`, not just documented.
- **`checkpoint` is separate from per-step `post_condition`.** A step can assert
  it individually succeeded (e.g., the checkout form actually advanced) while the
  artifact's top-level `checkpoint` is the one thing that defines "the goal is
  reached" for the whole flow — these are different questions and conflating them
  makes partial-success states unrepresentable.
- **`error_handlers` is the flow's own error taxonomy**, each entry pairing a
  `Locator` (how to *detect* the condition) with an `error_class`
  (`business_outcome` / `recoverable` / `hard_failure`) and a `recovery` action.
  This is what lets a validation-error banner produce a clean, callable result
  instead of a generic timeout — see §3.
- **`schema_version` + `Artifact.version` + `review_status`** (`draft`/`approved`)
  exist so a capability can be revised without breaking the contract silently, and
  so unattended production replay is gated on human sign-off: `ReplayEngine.run()`
  refuses a `draft` artifact by default, and `python -m src.cli approve` only
  flips it to `approved` given a stability report meeting a minimum success rate
  — see §3.
- **`TargetApp.vendor_product`** is a separate field from `base_url` on purpose:
  see §4.

## 3. Determinism & error handling

Replay (`src/replay_engine.py`) never calls the LLM. Determinism comes from three
things: (1) the artifact's steps and locators are fixed at discovery time; (2) each
locator tier is tried with an explicit wait rather than a fixed sleep, so timing
noise doesn't change *what* happens, only *how long* it takes; (3) every runtime
branch is classified through the artifact's own `error_handlers` before any
generic fallback fires, so app-specific conditions are handled deliberately.

**The three-way result contract is the core of this:**

| `RunStatus` | Meaning | Example in this repo |
|---|---|---|
| `success` | Checkpoint verified, outputs returned | reached "Checkout: Overview" |
| `business_outcome` | A legitimate, expected non-happy-path result | `CHECKOUT_VALIDATION_ERROR` when a required field is blank (demoed in README §3) |
| `hard_failure` | Unexpected; carries `failed_step_id` / `expected` / `observed` | a locator's every tier fails to resolve |

After every step, `_check_error_handlers()` gives each of the artifact's declared
handlers a short (600ms) chance to match against the current page before we move
on. A match short-circuits with a clean classification instead of letting a stale
element or a slow-loading error banner turn into an opaque Playwright timeout
several steps later. `RecoveryAction` supports `dismiss` (close a known
interstitial and continue), `retry_step` (transient slowness), and `escalate`
(hand off to a human) — the last one reuses exactly the same escalation mechanism
as a `RISKY` step confirmation (§5), so there is one pause/resume code path, not two.

**Locators, not sleeps, absorb timing.** `src/locator.py`'s `resolve()` gives the
first (most-robust) tier the full step timeout and waits for visibility rather than
polling `count()` — this was a real bug I hit and fixed during development: an
earlier version checked `.count() == 0` before waiting, which raced any click that
triggered a client-side navigation (e.g. Cart → Checkout) and failed spuriously.
Later fallback tiers get a short timeout each, since if the top tier were correct
we'd already have returned — this bounds the worst case instead of summing full
timeouts across five tiers of a genuinely-broken locator.

**Checkpoints are keyed on URL, not on arbitrary displayed text.** Discovery lets
the model name a `checkpoint_text` it saw on the final screen as its own proof of
success, but compiling that string directly into the checkpoint's `text_pattern`
is a trap: the first live run's checkpoint text was "Total: $32.39" — correct for
that run, but the total is a function of *which item* `item_name` requests, so
replaying the same capability for a different item would fail its own checkpoint
on a legitimate success. `_compile_artifact` instead derives `checkpoint.url_pattern`
from the page's actual URL path at the moment of success (parameter-independent
for a routed web app) and keeps the model's text only as a human-readable
`description`. General rule worth stating plainly: a checkpoint must be invariant
across the parameter space the artifact declares, or it isn't testing the goal,
it's testing one specific invocation of it.

**Secondary: UI drift.** Because the target class of app changes slowly (Section 1
of the brief), drift detection here is intentionally lightweight rather than a
separate visual-diff subsystem: if the *top* locator tier stops resolving,
`resolve()` automatically falls back to the next tier and *replay still succeeds*
— which is itself a drift signal worth recording, not just absorbing silently.

**Stretch goals: multi-run stability + confidence & approval.** These two are
implemented together because they answer the same question from two angles: "is
this capability reliable enough to trust." `python -m src.cli stability` replays
an artifact N times against the same params and, for every step, aggregates
*which locator tier resolved on each run* (via a new `locator_resolved` evidence
event) into a `StabilityReport` — a step that succeeds every run but drifts
between tiers is flagged even though every individual run looked fine.
`python -m src.cli approve` then flips `Artifact.review_status: draft -> approved`,
but only if a stability report for that exact `(artifact_id, version)` meets a
minimum success rate (100% by default) — and `ReplayEngine.run()` now refuses to
execute a `draft` artifact at all unless the caller explicitly passes
`require_approval=False` (`--allow-draft` on the CLI) for a one-off supervised
run. This is a real, load-bearing gate, not just a schema field: it's what
"gate unattended replay on an approval state" from the brief's stretch-goal list
actually means in this codebase (`evidence/stability_*/report.json`,
`src/replay_engine.py`'s gate at the top of `run()`).

**A stability report validates the *checkpoint*, not the *outputs*.** This
limitation is worth stating plainly rather than glossing over, because I hit it
for real: a discovery run can reach the correct final page (checkpoint passes)
while having recorded an EXTRACT step that reads the wrong element — in one
actual run here, `item_price` was extracted from the page's section heading
("Checkout: Overview") instead of the product's price, because the goal text
given to the model was ambiguous about which on-screen number counted as "the
item's listed price." Five stability runs of that artifact would have reported
100% success, because checkpoint-reaching and output-correctness are different
properties, and `stability` only measures the first. I fixed the actual cause
(tightened the discovery goal's wording to name the specific price, unambiguously,
by its position relative to the product), not the symptom — a report can't
sanity-check semantic correctness of an output it doesn't have a schema for
beyond `type: string`, and pretending otherwise would be worse than documenting
the gap. A more complete fix (not built here) would let `Output` declare a
`value_pattern` the way `Checkpoint.text_pattern` does, so `stability` could
reject a report where an output's value never looks like a price.

**A second real bug caught by the *hard_failure* evidence specifically, not the
success-path stability runs**: `_parameterize_locator` (§2) originally kept the
COORDINATES tier as an unconditional last resort even for a parameterized click.
Replaying `add_item_to_cart_and_reach_checkout_review` with an `item_name` that
matches no real product should fail (no such element exists) — instead, every
content-based tier correctly failed to resolve, fell through to COORDINATES,
and that tier just clicked whatever pixel position was captured at discovery
time (the originally-recorded product's button), reporting a clean `success`
with that product's price. Wrong result, reported with total confidence. Fixed
by dropping COORDINATES too whenever a locator is being parameterized by a
click's target-selecting param — a pixel position checks no identity at all, so
it is strictly less safe than even the ambiguous role/text tiers already
excluded, not a legitimate fallback. Verified live: the same nonexistent-item
replay now correctly returns `hard_failure` with the exact selectors it tried.
Both of these are documented here instead of quietly fixed and hidden because
they're the clearest evidence in this repo that testing against a live target
finds classes of bugs a schema review alone would not.

## 4. Heterogeneity & multi-tenant

**Surface abstraction.** The seam is the perceive/act boundary
(`perception.py` ↔ everything downstream). `snapshot()` today speaks DOM +
accessibility tree; nothing above it — `schema.py`, `locator.py`'s tier semantics,
`replay_engine.py` — knows or cares that the surface is a browser. Extending to a
legacy server-rendered web app needs no new *kind* of tier: `ROLE_NAME` and `TEXT`
already work off accessible names computed from `<label for>`/`aria-*`, which
survive table layouts and absent test IDs (proven by `tests/fixtures/legacy_app/`,
a deliberately hookless, table-based, framed fixture the offline test suite runs
against). Extending to a **desktop app** means swapping `perception.py`'s
extraction for an OS accessibility API (e.g. macOS `AXUIElement` / Windows UIA) that
emits the same `ElementInfo`/`Locator` shape — role, accessible name, and a
coordinate fallback map directly onto what those APIs already expose — and
swapping Playwright calls in `replay_engine.py`/`locator.py` for the platform's
input-injection API. `Artifact.target.surface` (`web` / `legacy_web` / `desktop`)
already exists as the dispatch key; nothing in the schema is web-specific.

**Multi-tenant reuse.** `TargetApp.vendor_product` is deliberately separate from
`base_url`: the artifact's identity for reuse purposes is "this is a Swag-Labs-style
checkout flow," not "this is tenant 42's instance of it." The design I'd build out
(not built here, per Section 3.7's "design, not necessarily build"):
- Key the artifact catalog by `(vendor_product, capability_id)`, with `base_url`
  and any tenant-specific text/branding supplied as **overrides** at replay time —
  most `TEXT`/`ROLE_NAME` tiers are already parameterizable this way without
  touching the step list.
- A tenant-specific override layer (a thin patch: "this tenant's build calls the
  checkout button `data-test=proceed` instead of `checkout`") stored separately
  from the base artifact, applied at load time — so 200 tenants on the same vendor
  product share one recorded flow plus N small diffs, not N full recordings.
- **Drift detection**: since `resolve()` already reports *which tier* satisfied
  each step, aggregating "tier index used" per tenant per capability over time is
  a cheap, real signal — a tenant whose replays are quietly sliding from tier 0 to
  tier 2 is drifting and due for review, before it fails outright. This is exactly
  what the stretch goal "multi-run stability" would formalize; the raw signal
  already exists in every `run.log.jsonl` (`error_handler_matched`/tier data), it
  just isn't aggregated across runs yet.
- Canonicalization (`/item/12345` → `/item/:id`) is the same idea applied to
  `value_template`/URL patterns and would live in the same override layer.

## 5. Escalation & handoff

**Detecting "stuck."** Three independent triggers, all routing to the same
mechanism: (1) the model itself calls the `escalate` tool during discovery when it
can't find a safe way forward; (2) `N` consecutive action failures
(`guardrails.yaml: max_consecutive_failures_before_escalation`) auto-escalates
rather than looping indefinitely; (3) a step or click matched as `RISKY`
(`RiskLevel.RISKY` on the artifact, or a `require_confirmation` click-text pattern
in `guardrails.yaml` — checked independently, so loosening one doesn't bypass the
other) always escalates before acting, regardless of whether anything has gone
wrong yet — an irreversible action needing sign-off isn't a failure mode, it's a
policy.

**Taking control of the live session, for real.** The browser is always launched
with `--remote-debugging-port` (`src/escalation.py`). On escalation, the automation
process (a) screenshots and writes `InterventionRequest` + a pointer file with the
CDP endpoint, goal, step, and reason, then (b) blocks polling for a `resume.signal`
file — it does not close the browser or the page. A **separate process**,
`operator_cli.py`, connects to that *exact* endpoint via
`chromium.connect_over_cdp(...)`, gets the same `browser.contexts[0].pages[0]` —
same cookies, same DOM, same mid-flow state — lets the operator act (a tiny
command language: `fill`/`click`/`press`/`note`, or a scripted JSON action list for
reproducible demos), and writes the resume signal with a log of what it did. The
waiting process detects the signal, takes an "after" screenshot, appends the
operator's actions and notes to the evidence trail, and resumes. I verified this
live end-to-end (`evidence/replay_*` for the `checkout_finish_demo` artifact):
pause on the `RISKY` "Finish" click, a second process attaches over CDP, confirms,
and the original replay process completes and returns outputs.

**Who's in control** is explicit, not implied: the pointer file's `control` field
flips `"human"` → (deleted, i.e. automation) on resume, and every state transition
is a logged event (`escalation_raised` / `step_confirmed_by_human` /
`escalation_resolved`), so a real dashboard could show current ownership without
guessing from the absence of automation log lines.

**What's mocked:** the "operator console" is a CLI, explicitly allowed by the
brief's scope note. What is *not* mocked is the control-transfer model — CDP
attachment to the live session, explicit pause/resume signaling, and a recorded
account of what the human did — which is the part the brief says has to be real.
A production version would swap `operator_cli.py`'s command loop for a real
co-browsing UI (e.g. an embedded live view over the same CDP connection) without
touching `escalation.py` at all.

## 6. Safety

**Allowlist.** `guardrails.yaml` is the single source of truth for which domains
(`allowed_domains`) and action types (`allowed_actions`) are permitted; both
`llm_agent.py` and `replay_engine.py` call `Guardrails.check_domain()` /
`check_action_type()` before every navigation/action — including navigations the
model itself requests during discovery, so a model that tries to wander off-target
is blocked at the guardrail layer, not by hoping the prompt is followed.

**Risk classification is defense-in-depth, checked twice, independently.** A click
is classified risky if *either* the artifact's own `Step.risk_level == RISKY` (set
at discovery time) *or* the click's visible text matches a `risky_click_text_pattern`
in `guardrails.yaml` (checked fresh, at replay time, off the live page — not
trusted from the stored artifact). `policy: block` refuses outright (e.g.
"delete"); `require_confirmation` escalates to a human and only proceeds on
explicit resume (e.g. "finish"/"place order" — demoed live, §5); `flag` logs and
proceeds (e.g. "remove," reversible here). This means a compromised or malformed
artifact can't silently mark a dangerous action safe — the live-page check still
catches it.

**Redaction.** Every string written to a log line, `result.json`, or artifact
passes through `Guardrails.redact()` (regex patterns for SSNs, card numbers,
emails, long digit runs) via `EvidenceWriter._redact_deep()`, and any field whose
*name* matches `never_log_field_names` (password, ssn, account_number, token, ...)
is replaced outright rather than pattern-matched — verified in
`tests/test_replay_engine.py::test_replay_never_writes_raw_password_to_evidence_log`
and live in the `checkout_finish_demo` evidence (`password` never appears in
`run.log.jsonl`). Crucially, this redaction is **write-path only**: the in-memory
`RunResult.outputs` returned to the caller is never mutilated, since the calling
agent is entitled to real data (e.g. an actual balance) — only what touches disk
is redacted. That split is deliberate and is the main limit of this model: if a
caller logs the returned `RunResult` itself downstream, this system's guardrail
can't follow it there.

**Limits.** The risky-pattern list is a curated, English-text-substring allowlist
of *known* dangerous verbs — it will not catch a risky action phrased in a way
that isn't in the list, and it's per-click, not a semantic understanding of
consequence. `review_status` (§3) now backs a real gate on top of this (unattended
replay refuses a `draft` artifact), but that gate checks provenance/track-record,
not the click-text policy itself — the two are independent layers, deliberately,
so a compromised or malformed artifact can't get itself "approved" its way past
the live-page risky-click check.

## 7. Cuts

Built thin-but-real for every core requirement; cut breadth, not requirements.
What's missing, in order I'd build it next:

1. **Output semantic validation** (§3) — a stability report currently proves the
   *checkpoint* is reached reliably, not that every extracted output is
   semantically sane. `Output` could grow an optional `value_pattern` (mirroring
   `Checkpoint.text_pattern`) that `stability`/`approve` check per run, so
   "reached the right page but extracted the wrong field" (a real bug hit and
   fixed by hand here) would fail the gate automatically instead of needing a
   human to eyeball outputs before approving. Highest-value next step now that
   confidence/approval and stability are built.
2. **Cross-tenant override layer** (§4) — designed, not built. I'd build the
   override-patch format and demonstrate one artifact applied to a second saucedemo
   look-alike (a locally hosted variant) before touching a second real vendor app.
3. **Assisted fallback** (bounded, single-step LLM recovery on replay failure) —
   deliberately left out to keep replay's "no LLM in the decision loop" guarantee
   unambiguous for this submission; would be an explicit, separately-logged escape
   hatch, not a default.
4. **Real operator UI** in place of `operator_cli.py` — out of scope per the brief;
   the control-transfer mechanism underneath it is what's real (§5).
5. **Desktop/legacy-web surface implementations** — designed (§4), not built; the
   brief doesn't ask for them.

**Stretch goals implemented:** multi-run stability + confidence & approval gating
(§3) — chosen together because they compose (approval is gated on a stability
report), and because building them surfaced two real bugs (§3) that the "happy
path once" demo alone would never have caught.
