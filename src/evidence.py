"""Structured, redacted evidence capture shared by discovery, replay, and escalation.

Every run gets its own directory under evidence/ containing:
  run.log.jsonl   -- one JSON object per event, redacted, append-only
  screenshots/    -- PNGs, captured on state changes and always on failure/escalation
  result.json     -- the final RunResult (or InterventionRequest), written at the end

Nothing here ever writes raw parameter values for fields the guardrail config marks
sensitive, and every string is passed through Guardrails.redact() before hitting disk.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from playwright.sync_api import Page

from .guardrails import Guardrails

EVIDENCE_ROOT = Path(__file__).resolve().parent.parent / "evidence"


class EvidenceWriter:
    def __init__(self, run_kind: str, guardrails: Guardrails, run_id: str | None = None, root: Path | None = None):
        self.run_id = run_id or f"{run_kind}_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        self.root = root or EVIDENCE_ROOT
        self.dir = self.root / self.run_id
        self.screenshots_dir = self.dir / "screenshots"
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        self.guardrails = guardrails
        self._log_path = self.dir / "run.log.jsonl"
        self._shot_counter = 0
        self.log("run_started", {"run_kind": run_kind, "run_id": self.run_id})

    def log(self, event: str, data: dict[str, Any] | None = None) -> None:
        data = data or {}
        safe = self._redact_deep(data)
        record = {"ts": time.time(), "event": event, **safe}
        with self._log_path.open("a") as f:
            f.write(json.dumps(record, default=str) + "\n")

    def _redact_deep(self, obj: Any) -> Any:
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                if str(k).lower() in self.guardrails.config.never_log_field_names:
                    out[k] = "[REDACTED:sensitive_field]"
                else:
                    out[k] = self._redact_deep(v)
            return out
        if isinstance(obj, list):
            return [self._redact_deep(v) for v in obj]
        if isinstance(obj, str):
            return self.guardrails.redact(obj)
        return obj

    def screenshot(self, page: Page, label: str) -> str:
        self._shot_counter += 1
        name = f"{self._shot_counter:03d}_{label}.png"
        path = self.screenshots_dir / name
        try:
            page.screenshot(path=str(path), timeout=5000)
        except Exception as exc:  # noqa: BLE001 -- screenshot failure must never abort a run
            self.log("screenshot_failed", {"label": label, "error": str(exc)})
            return ""
        rel = str(path.relative_to(self.root.parent))
        self.log("screenshot", {"label": label, "path": rel})
        return rel

    def write_result(self, result: dict[str, Any]) -> None:
        (self.dir / "result.json").write_text(json.dumps(self._redact_deep(result), indent=2, default=str))

    def write_artifact_copy(self, artifact_json: dict[str, Any]) -> None:
        (self.dir / "artifact_produced.json").write_text(json.dumps(artifact_json, indent=2, default=str))
