"""Human-in-the-loop escalation and live-session control transfer.

The seam: automation always drives the browser through a Chromium instance launched
with a remote-debugging port (`--remote-debugging-port`). As long as that port is
open, *any* process can attach to the exact same live session via Chrome DevTools
Protocol (CDP) -- not a fresh browser, the same cookies/DOM/navigation state the
automation was mid-flow on.

On escalation we:
  1. capture context (screenshot, reason, current step, CDP endpoint) and write an
     InterventionRequest -- this is the "raise a ticket to a human operator" step;
  2. stop issuing commands from the automation process and block on a resume signal;
  3. a second process (operator_cli.py) attaches over CDP to the SAME page, acts on
     it, and writes the resume signal describing what it did;
  4. we detect the signal, snapshot "after", diff against "before" as a best-effort
     record of what changed, and hand control back to the caller.

`operator_cli.py` is a deliberately mocked operator surface (a CLI instead of a
co-browsing console -- out of scope per the brief) driving a *real* control-transfer
mechanism: a second driver attached to the live session, with explicit pause/resume
signaling and a record of who was in control and what they did.
"""
from __future__ import annotations

import json
import time
import uuid

from playwright.sync_api import Page

from .evidence import EVIDENCE_ROOT, EvidenceWriter
from .guardrails import Guardrails
from .schema import InterventionRequest

POINTER_PATH = EVIDENCE_ROOT / "pending_intervention.json"


class Escalation(Exception):
    """Raised by the agent loop / replay engine to signal a stuck/blocked state."""

    def __init__(self, reason: str, step_id: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.step_id = step_id


class Escalator:
    def __init__(self, evidence: EvidenceWriter, guardrails: Guardrails, cdp_endpoint: str):
        self.evidence = evidence
        self.guardrails = guardrails
        self.cdp_endpoint = cdp_endpoint

    def request_and_wait(
        self,
        page: Page,
        *,
        run_id: str,
        goal: str,
        capability_id: str | None,
        step_id: str | None,
        reason: str,
        poll_interval: float = 1.0,
        timeout_s: float = 900.0,
    ) -> InterventionRequest:
        before_shot = self.evidence.screenshot(page, "before_escalation")
        req = InterventionRequest(
            id=uuid.uuid4().hex[:10],
            run_id=run_id,
            capability_id=capability_id,
            goal=goal,
            step_id=step_id,
            reason=reason,
            screenshot_path=before_shot,
            context_excerpt=self.guardrails.redact(page.url),
            cdp_endpoint=self.cdp_endpoint,
        )
        req_path = self.evidence.dir / "intervention_request.json"
        req_path.write_text(req.model_dump_json(indent=2))
        POINTER_PATH.parent.mkdir(parents=True, exist_ok=True)
        POINTER_PATH.write_text(json.dumps({
            "run_id": run_id,
            "run_dir": str(self.evidence.dir.relative_to(EVIDENCE_ROOT.parent)),
            "cdp_endpoint": self.cdp_endpoint,
            "reason": reason,
            "goal": goal,
            "step_id": step_id,
            "screenshot": before_shot,
            "control": "human",
        }, indent=2))

        self.evidence.log("escalation_raised", {
            "reason": reason, "step_id": step_id, "cdp_endpoint": self.cdp_endpoint,
        })
        print("\n" + "=" * 72)
        print("HUMAN INTERVENTION REQUESTED")
        print(f"  run_id:   {run_id}")
        print(f"  goal:     {goal}")
        print(f"  step:     {step_id}")
        print(f"  reason:   {reason}")
        print(f"  screenshot: {before_shot}")
        print(f"  live session CDP endpoint: {self.cdp_endpoint}")
        print("  -> operator: run `python operator_cli.py` in another terminal,")
        print("     take over the SAME browser window, perform the fix, then")
        print("     signal resume from that tool.")
        print("=" * 72 + "\n")

        resume_path = self.evidence.dir / "resume.signal"
        waited = 0.0
        while not resume_path.exists():
            time.sleep(poll_interval)
            waited += poll_interval
            if waited >= timeout_s:
                raise TimeoutError(f"no resume signal received within {timeout_s}s")

        signal = json.loads(resume_path.read_text())
        after_shot = self.evidence.screenshot(page, "after_handoff")

        req.status = "resolved"
        req.resumed_at = time.time()
        req.resolution_notes = signal.get("notes", "")
        req.human_actions = signal.get("actions", [])
        req_path.write_text(req.model_dump_json(indent=2))

        self.evidence.log("escalation_resolved", {
            "resolution_notes": req.resolution_notes,
            "human_actions": req.human_actions,
            "after_screenshot": after_shot,
            "control": "automation",
        })

        if POINTER_PATH.exists():
            POINTER_PATH.unlink()
        resume_path.unlink()
        return req
