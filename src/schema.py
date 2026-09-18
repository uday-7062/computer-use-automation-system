"""Typed data models for the capability artifact and run results.

An Artifact is the reusable, versioned, agent-invocable "capability" produced by a
discovery run and consumed by the replay engine. It is intentionally decoupled from
the raw LLM transcript: nothing here requires a model to interpret it at replay time.
"""
from __future__ import annotations

import time
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# Locators
# --------------------------------------------------------------------------- #

class LocatorKind(str, Enum):
    TEST_ID = "test_id"          # data-test / data-testid / id attribute
    ROLE_NAME = "role_name"      # ARIA role + accessible name (accessibility tree)
    TEXT = "text"                # visible text content, exact or substring
    CSS = "css"                  # CSS selector (structural, most brittle)
    XPATH = "xpath"              # XPath (fallback for frames/tables with no other hook)
    COORDINATES = "coordinates"  # last-resort viewport-relative click point


class LocatorTier(BaseModel):
    """One candidate way to find an element, ranked by robustness."""

    kind: LocatorKind
    value: str
    frame_path: list[str] = Field(
        default_factory=list,
        description="CSS selectors of nested iframes to traverse before applying `value`, "
        "outermost first. Empty for the main frame.",
    )
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="Author's estimate of how likely this tier still resolves correctly "
        "after minor UI drift. Used only to order fallback attempts.",
    )


class Locator(BaseModel):
    """A ranked fallback chain of tiers. Replay tries tiers in order until one
    resolves to exactly one visible, actionable element."""

    tiers: list[LocatorTier]
    element_role: Optional[str] = Field(
        default=None, description="Best-effort ARIA role, for logging/debugging only."
    )
    element_label: Optional[str] = Field(
        default=None, description="Best-effort human-readable label, for logging/debugging only."
    )

    def reasoning_summary(self) -> str:
        return " > ".join(f"{t.kind.value}:{t.value!r}" for t in self.tiers)


# --------------------------------------------------------------------------- #
# Steps / actions
# --------------------------------------------------------------------------- #

class ActionType(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    FILL = "fill"
    SELECT = "select"
    PRESS_KEY = "press_key"
    WAIT_FOR = "wait_for"
    EXTRACT = "extract"


class RiskLevel(str, Enum):
    SAFE = "safe"              # read-only or trivially reversible (navigate, read, filter)
    LOW = "low"                # reversible state change (add to cart, fill a draft field)
    RISKY = "risky"            # irreversible / consequential (submit, delete, place order,
                                # move money) -- gated by guardrails.py, see policy field


class Checkpoint(BaseModel):
    """A condition asserted to confirm the flow actually reached the expected state,
    rather than assuming the previous action worked."""

    description: str
    locator: Optional[Locator] = None
    url_pattern: Optional[str] = Field(
        default=None, description="Regex matched against the current page URL."
    )
    text_pattern: Optional[str] = Field(
        default=None, description="Regex that must be found in the page's visible text."
    )
    timeout_ms: int = 8000


class ErrorClass(str, Enum):
    BUSINESS_OUTCOME = "business_outcome"  # legitimate, expected non-happy-path result
    RECOVERABLE = "recoverable"            # transient/known condition the engine can handle
    HARD_FAILURE = "hard_failure"          # unexpected; stop and surface for debugging


class RecoveryAction(str, Enum):
    NONE = "none"               # just classify and report, don't act
    DISMISS = "dismiss"         # dismiss/close the matched element (known interstitial)
    RETRY_STEP = "retry_step"   # wait briefly and retry the step that triggered the check
    ESCALATE = "escalate"       # pause and hand off to a human operator


class ErrorHandler(BaseModel):
    """One entry in the artifact's error taxonomy: "if this appears, it means X"."""

    code: str = Field(description="Stable machine-readable outcome code, e.g. 'ITEM_NOT_FOUND'.")
    description: str
    detect: Locator = Field(description="How to detect the condition (e.g. an error banner).")
    error_class: ErrorClass
    recovery: RecoveryAction = RecoveryAction.NONE
    max_retries: int = 0


class Step(BaseModel):
    id: str
    action: ActionType
    locator: Optional[Locator] = Field(
        default=None, description="Required for click/fill/select/press_key/extract/wait_for."
    )
    value_template: Optional[str] = Field(
        default=None,
        description="Literal value or '{{param_name}}' template, used by fill/select/"
        "press_key/navigate.",
    )
    extract_as: Optional[str] = Field(
        default=None, description="Output name this EXTRACT step populates."
    )
    extract_attr: Literal["text", "value", "href"] = "text"
    risk_level: RiskLevel = RiskLevel.SAFE
    post_condition: Optional[Checkpoint] = Field(
        default=None, description="Optional per-step assertion, checked immediately after acting."
    )
    timeout_ms: int = 8000
    notes: Optional[str] = Field(
        default=None, description="Why this locator/tier chain was chosen (robustness reasoning)."
    )


# --------------------------------------------------------------------------- #
# Capability contract (params / outputs)
# --------------------------------------------------------------------------- #

class ParamType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    BOOLEAN = "boolean"


class Param(BaseModel):
    name: str
    type: ParamType
    required: bool = True
    description: str
    example: Optional[str] = None
    sensitive: bool = Field(
        default=False,
        description="If true, value is redacted in all logs/evidence and never written "
        "into the stored artifact trace.",
    )


class Output(BaseModel):
    name: str
    type: ParamType
    description: str


# --------------------------------------------------------------------------- #
# The artifact itself
# --------------------------------------------------------------------------- #

class TargetApp(BaseModel):
    base_url: str
    allowed_domains: list[str]
    vendor_product: str = Field(
        description="Logical name of the underlying app/vendor product, independent of "
        "which tenant/instance is being driven. Used for cross-tenant artifact reuse."
    )
    surface: Literal["web", "legacy_web", "desktop"] = "web"


class Provenance(BaseModel):
    discovery_run_id: str
    model: str
    created_at: float = Field(default_factory=time.time)
    goal: str


class Artifact(BaseModel):
    """A versioned, reviewable, agent-invocable capability."""

    schema_version: Literal["1.0"] = "1.0"
    id: str = Field(description="Stable slug identifying this capability, e.g. 'add_item_to_cart_and_reach_checkout_review'.")
    version: int = Field(default=1, description="Bumped whenever steps/locators/contract change.")
    name: str
    description: str
    target: TargetApp
    params: list[Param]
    outputs: list[Output]
    steps: list[Step]
    checkpoint: Checkpoint = Field(description="Overall success condition for the whole flow.")
    error_handlers: list[ErrorHandler] = Field(default_factory=list)
    provenance: Provenance
    review_status: Literal["draft", "approved"] = "draft"

    def artifact_key(self) -> str:
        return f"{self.id}.v{self.version}"


# --------------------------------------------------------------------------- #
# Run results (shared shape returned by both discovery and replay)
# --------------------------------------------------------------------------- #

class RunStatus(str, Enum):
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    HARD_FAILURE = "hard_failure"
    ESCALATED = "escalated"


class RunResult(BaseModel):
    status: RunStatus
    outcome_code: Optional[str] = None
    message: str
    outputs: dict[str, Any] = Field(default_factory=dict)
    failed_step_id: Optional[str] = None
    expected: Optional[str] = None
    observed: Optional[str] = None
    evidence_dir: Optional[str] = None
    duration_ms: int = 0


class InterventionRequest(BaseModel):
    id: str
    run_id: str
    capability_id: Optional[str] = None
    goal: str
    step_id: Optional[str] = None
    reason: str
    screenshot_path: Optional[str] = None
    context_excerpt: str = ""
    cdp_endpoint: Optional[str] = None
    created_at: float = Field(default_factory=time.time)
    status: Literal["pending", "resolved"] = "pending"
    resolution_notes: Optional[str] = None
    resumed_at: Optional[float] = None
    human_actions: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Multi-run stability (stretch goal: confidence & approval)
# --------------------------------------------------------------------------- #

class StepTierUsage(BaseModel):
    """How consistently the same locator tier resolved a step across N replay
    runs -- a step that resolves via a different tier run-to-run is drifting or
    flaky even if every individual run reported success."""

    step_id: str
    tier_counts: dict[str, int] = Field(
        default_factory=dict, description="LocatorKind value -> number of runs it resolved via that tier."
    )


class StabilityReport(BaseModel):
    """Produced by `python -m src.cli stability`; consumed by `approve` as the
    evidence gate for flipping an artifact from draft to approved."""

    artifact_id: str
    artifact_version: int
    runs: int
    success_count: int
    business_outcome_count: int
    hard_failure_count: int
    success_rate: float
    step_tier_usage: list[StepTierUsage] = Field(default_factory=list)
    run_evidence_dirs: list[str] = Field(default_factory=list)
    created_at: float = Field(default_factory=time.time)
