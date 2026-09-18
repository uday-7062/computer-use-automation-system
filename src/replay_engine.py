"""Deterministic replay: executes a saved Artifact against a live surface with
input parameters, no LLM in the decision loop, and reports a structured RunResult.

Result contract (RunStatus):
  SUCCESS           -- checkpoint verified, declared outputs returned.
  BUSINESS_OUTCOME  -- a known, legitimate non-happy-path result (e.g. "item not
                       found"). Not a crash; the caller needs this, not a stack trace.
  HARD_FAILURE      -- unexpected: locator never resolved, checkpoint never reached,
                       a guardrail blocked the run. Carries failed_step_id/expected/
                       observed for debugging.
  ESCALATED         -- handed off to a human mid-run and is now waiting/resolved
                       (only returned if the run could not resume automatically).

Every runtime condition first passes through `artifact.error_handlers` (the flow's
own error taxonomy) before we fall back to generic timeout/locator-failure handling,
so app-specific "record not found" banners are classified deliberately rather than
surfacing as opaque timeouts.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from playwright.sync_api import Page, sync_playwright

from .evidence import EvidenceWriter
from .escalation import Escalation, Escalator
from .guardrails import Guardrails, GuardrailViolation
from .locator import LocatorResolutionError, resolve
from .schema import (
    ActionType, Artifact, Checkpoint, ErrorClass, RecoveryAction, RiskLevel,
    RunResult, RunStatus, Step,
)

_TEMPLATE_RE = re.compile(r"\{\{\s*(\w+)(?:\|(\w+))?\s*\}\}")


def render_template(template: str | None, params: dict) -> str | None:
    """Substitutes {{param}} with its raw value, or {{param|slug}} with a
    lowercase/hyphenated form -- the convention this app (and many real ones)
    use to derive stable attribute values (e.g. data-test) from a display name.
    Used both for typed values (Step.value_template) and, via _render_locator
    below, for locator tier values that were parameterized at discovery time.
    """
    if template is None:
        return None

    def _sub(m: re.Match) -> str:
        name, filt = m.group(1), m.group(2)
        val = str(params.get(name, ""))
        if filt == "slug":
            val = val.strip().lower().replace(" ", "-")
        return val

    return _TEMPLATE_RE.sub(_sub, template)


def render_locator(locator, params: dict):
    """Returns a copy of `locator` with every tier's value template-rendered
    against `params`. A no-op for tiers with no {{...}} placeholder (the common
    case for hand-authored artifacts), so this is safe to call unconditionally."""
    if locator is None:
        return None
    return locator.model_copy(update={
        "tiers": [t.model_copy(update={"value": render_template(t.value, params) or t.value}) for t in locator.tiers]
    })


class HardFailure(Exception):
    def __init__(self, message: str, step_id: str | None = None, expected: str | None = None, observed: str | None = None):
        super().__init__(message)
        self.step_id = step_id
        self.expected = expected
        self.observed = observed


class BusinessOutcome(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def verify_checkpoint(page: Page, checkpoint: Checkpoint) -> tuple[bool, str]:
    if checkpoint.url_pattern and not re.search(checkpoint.url_pattern, page.url):
        return False, f"url {page.url!r} did not match /{checkpoint.url_pattern}/"
    if checkpoint.text_pattern:
        try:
            body_text = page.evaluate("() => document.body.innerText")
        except Exception:
            body_text = ""
        if not re.search(checkpoint.text_pattern, body_text or ""):
            return False, f"page text did not match /{checkpoint.text_pattern}/"
    if checkpoint.locator:
        try:
            resolve(page, checkpoint.locator, timeout_ms=checkpoint.timeout_ms)
        except LocatorResolutionError as exc:
            return False, f"checkpoint element not found (tried: {exc.tiers_tried})"
    return True, "checkpoint satisfied"


@dataclass
class ReplayEngine:
    artifact: Artifact
    guardrails: Guardrails
    headless: bool = True
    auto_approve_risky: bool = False
    cdp_port: int = 9333

    require_approval: bool = True

    def run(self, params: dict, evidence: EvidenceWriter) -> RunResult:
        start = time.time()

        if self.require_approval and self.artifact.review_status != "approved":
            result = RunResult(
                status=RunStatus.HARD_FAILURE,
                message=(
                    f"artifact {self.artifact.artifact_key()!r} is in review_status="
                    f"{self.artifact.review_status!r}, not 'approved'. Unattended replay is gated on "
                    f"approval (stretch goal: confidence & approval) -- run `python -m src.cli stability` "
                    f"to build a track record, then `python -m src.cli approve` once it's reliable, or "
                    f"pass allow_draft=True / --allow-draft for a one-off supervised run."
                ),
                evidence_dir=str(evidence.dir),
            )
            evidence.log("blocked_unapproved_artifact", {"review_status": self.artifact.review_status})
            evidence.write_result(result.model_dump(mode="json"))
            return result

        missing = [p.name for p in self.artifact.params if p.required and p.name not in params]
        if missing:
            result = RunResult(
                status=RunStatus.HARD_FAILURE,
                message=f"missing required params: {missing}",
                evidence_dir=str(evidence.dir),
            )
            evidence.write_result(result.model_dump(mode="json"))
            return result

        evidence.log("replay_started", {
            "artifact_id": self.artifact.id, "artifact_version": self.artifact.version,
            "params": self.guardrails.redact_dict({k: v for k, v in params.items()}),
        })

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=self.headless,
                args=[f"--remote-debugging-port={self.cdp_port}"],
            )
            context = browser.new_context(viewport={"width": 1280, "height": 800})
            page = context.new_page()
            cdp_endpoint = f"http://127.0.0.1:{self.cdp_port}"
            escalator = Escalator(evidence, self.guardrails, cdp_endpoint)

            try:
                result = self._run_steps(page, params, evidence, escalator)
            except GuardrailViolation as exc:
                result = RunResult(status=RunStatus.HARD_FAILURE, message=f"guardrail violation: {exc}")
                evidence.screenshot(page, "guardrail_violation")
            except HardFailure as exc:
                result = RunResult(
                    status=RunStatus.HARD_FAILURE, message=str(exc), failed_step_id=exc.step_id,
                    expected=exc.expected, observed=exc.observed,
                )
                evidence.screenshot(page, "hard_failure")
            except BusinessOutcome as exc:
                result = RunResult(status=RunStatus.BUSINESS_OUTCOME, outcome_code=exc.code, message=exc.message)
                evidence.screenshot(page, "business_outcome")
            finally:
                browser.close()

        result.duration_ms = int((time.time() - start) * 1000)
        result.evidence_dir = str(evidence.dir)
        evidence.log("replay_finished", {"status": result.status.value, "message": result.message})
        evidence.write_result(result.model_dump(mode="json"))
        return result

    def _run_steps(self, page: Page, params: dict, evidence: EvidenceWriter, escalator: Escalator) -> RunResult:
        self.guardrails.check_domain(self.artifact.target.base_url)
        page.goto(self.artifact.target.base_url, timeout=self.guardrails.config.navigation_timeout_ms)
        evidence.screenshot(page, "start")

        outputs: dict[str, object] = {}

        for step in self.artifact.steps:
            self.guardrails.check_action_type(step.action.value)
            evidence.log("step_started", {"step_id": step.id, "action": step.action.value})

            if step.risk_level == RiskLevel.RISKY and not self.auto_approve_risky:
                escalator.request_and_wait(
                    page, run_id=evidence.run_id, goal=self.artifact.description,
                    capability_id=self.artifact.id, step_id=step.id,
                    reason=f"step {step.id!r} is marked RISKY ({step.notes or 'irreversible action'}); "
                           f"human confirmation/action required before proceeding",
                )
                evidence.log("step_confirmed_by_human", {"step_id": step.id})
                if step.post_condition:
                    ok, detail = verify_checkpoint(page, step.post_condition)
                    if not ok:
                        raise HardFailure(f"post-condition failed after human handoff: {detail}", step.id)
                continue

            try:
                self._execute_action(page, step, params, outputs, evidence)
            except LocatorResolutionError as exc:
                self._check_error_handlers(page, step, evidence)  # may raise BusinessOutcome/HardFailure
                raise HardFailure(
                    f"could not resolve target element for step {step.id!r}",
                    step.id, expected=step.notes or "element present", observed=f"tried: {exc.tiers_tried}",
                )

            try:
                self._check_error_handlers(page, step, evidence)
            except Escalation as esc:
                escalator.request_and_wait(
                    page, run_id=evidence.run_id, goal=self.artifact.description,
                    capability_id=self.artifact.id, step_id=step.id, reason=esc.reason,
                )
                evidence.log("step_resumed_after_escalation", {"step_id": step.id})

            if step.post_condition:
                ok, detail = verify_checkpoint(page, step.post_condition)
                if not ok:
                    raise HardFailure(f"post-condition failed for step {step.id!r}: {detail}", step.id,
                                       expected=step.post_condition.description, observed=detail)

            evidence.log("step_completed", {"step_id": step.id})

        ok, detail = verify_checkpoint(page, self.artifact.checkpoint)
        evidence.screenshot(page, "final")
        if not ok:
            raise HardFailure(f"final checkpoint not satisfied: {detail}", expected=self.artifact.checkpoint.description, observed=detail)

        for out in self.artifact.outputs:
            if out.name not in outputs:
                outputs[out.name] = None

        return RunResult(status=RunStatus.SUCCESS, message="goal reached; checkpoint verified", outputs=outputs)

    def _execute_action(self, page: Page, step: Step, params: dict, outputs: dict, evidence: EvidenceWriter) -> None:
        timeout = step.timeout_ms or self.guardrails.config.step_timeout_ms
        value = render_template(step.value_template, params)

        if step.action == ActionType.NAVIGATE:
            url = value or self.artifact.target.base_url
            self.guardrails.check_domain(url)
            page.goto(url, timeout=self.guardrails.config.navigation_timeout_ms)
            return

        if step.locator is None:
            raise HardFailure(f"step {step.id!r} ({step.action.value}) has no locator", step.id)
        resolved = resolve(page, render_locator(step.locator, params), timeout_ms=timeout)
        evidence.log("locator_resolved", {
            "step_id": step.id,
            "tier_kind": resolved.tier.kind.value,
            "tier_confidence": resolved.tier.confidence,
        })

        if step.action == ActionType.CLICK:
            text_for_risk = step.locator.element_label or ""
            check = self.guardrails.classify_click(text_for_risk)
            if not check.allowed:
                raise GuardrailViolation(f"click on {text_for_risk!r} blocked by policy: {check.reason}")
            if resolved.playwright_locator is not None:
                resolved.playwright_locator.click(timeout=timeout)
            else:
                page.mouse.click(*resolved.point)  # type: ignore[misc]

        elif step.action == ActionType.FILL:
            if resolved.playwright_locator is None:
                raise HardFailure(f"FILL requires an addressable element, step {step.id!r}", step.id)
            resolved.playwright_locator.fill(value or "", timeout=timeout)

        elif step.action == ActionType.SELECT:
            if resolved.playwright_locator is None:
                raise HardFailure(f"SELECT requires an addressable element, step {step.id!r}", step.id)
            resolved.playwright_locator.select_option(value, timeout=timeout)

        elif step.action == ActionType.PRESS_KEY:
            if resolved.playwright_locator is None:
                raise HardFailure(f"PRESS_KEY requires an addressable element, step {step.id!r}", step.id)
            resolved.playwright_locator.press(value or "Enter", timeout=timeout)

        elif step.action == ActionType.WAIT_FOR:
            pass  # resolve() above already waited for visibility

        elif step.action == ActionType.EXTRACT:
            if resolved.playwright_locator is None:
                raise HardFailure(f"EXTRACT requires an addressable element, step {step.id!r}", step.id)
            if step.extract_attr == "text":
                val = resolved.playwright_locator.inner_text(timeout=timeout)
            elif step.extract_attr == "value":
                val = resolved.playwright_locator.input_value(timeout=timeout)
            else:
                val = resolved.playwright_locator.get_attribute("href", timeout=timeout)
            if step.extract_as:
                outputs[step.extract_as] = val

    def _check_error_handlers(self, page: Page, step: Step, evidence: EvidenceWriter) -> None:
        for handler in self.artifact.error_handlers:
            try:
                resolve(page, handler.detect, timeout_ms=600)
            except LocatorResolutionError:
                continue  # expected: this condition simply isn't present

            evidence.log("error_handler_matched", {
                "code": handler.code, "error_class": handler.error_class.value, "step_id": step.id,
            })
            evidence.screenshot(page, f"error_{handler.code.lower()}")

            if handler.error_class == ErrorClass.BUSINESS_OUTCOME:
                raise BusinessOutcome(handler.code, handler.description)
            if handler.error_class == ErrorClass.HARD_FAILURE:
                raise HardFailure(handler.description, step.id, expected="no error banner", observed=handler.code)
            if handler.error_class == ErrorClass.RECOVERABLE:
                if handler.recovery == RecoveryAction.DISMISS:
                    try:
                        el = resolve(page, handler.detect, timeout_ms=600)
                        if el.playwright_locator is not None:
                            el.playwright_locator.click(timeout=600)
                    except Exception:
                        pass
                    return
                if handler.recovery == RecoveryAction.RETRY_STEP:
                    time.sleep(1.0)
                    return
                if handler.recovery == RecoveryAction.ESCALATE:
                    raise Escalation(handler.description, step.id)
            return
