"""Allowlist enforcement, risk classification, and redaction.

Both the discovery loop and the replay engine call `check_action` before touching
the page, and `redact` before anything is written to disk. This is the one module
every execution path is required to route through -- it is the safety backstop,
independent of what the LLM or a stored artifact claims is safe.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

_CONFIG_PATH = Path(__file__).parent / "guardrails.yaml"


@dataclass
class RiskPolicy:
    pattern: str
    reason: str
    policy: str  # "block" | "require_confirmation" | "flag"


@dataclass
class GuardrailConfig:
    allowed_domains: list[str]
    allowed_actions: list[str]
    risky_click_text_patterns: list[RiskPolicy]
    max_steps: int
    step_timeout_ms: int
    navigation_timeout_ms: int
    max_consecutive_failures_before_escalation: int
    pii_patterns: list[tuple[str, re.Pattern]]
    never_log_field_names: list[str]

    @classmethod
    def load(cls, path: Path = _CONFIG_PATH) -> "GuardrailConfig":
        raw = yaml.safe_load(path.read_text())
        risky = [RiskPolicy(**r) for r in raw.get("risky_click_text_patterns", [])]
        pii = [
            (p["name"], re.compile(p["regex"]))
            for p in raw.get("pii_redaction_patterns", [])
        ]
        return cls(
            allowed_domains=[d.lower() for d in raw["allowed_domains"]],
            allowed_actions=raw["allowed_actions"],
            risky_click_text_patterns=risky,
            max_steps=raw["max_steps"],
            step_timeout_ms=raw["step_timeout_ms"],
            navigation_timeout_ms=raw["navigation_timeout_ms"],
            max_consecutive_failures_before_escalation=raw["max_consecutive_failures_before_escalation"],
            pii_patterns=pii,
            never_log_field_names=[f.lower() for f in raw.get("never_log_field_names", [])],
        )


class GuardrailViolation(Exception):
    """Raised when an action falls outside the allowlist and must not proceed."""


@dataclass
class ActionCheck:
    allowed: bool
    risk: str  # "safe" | "flag" | "require_confirmation" | "block"
    reason: str = ""


class Guardrails:
    def __init__(self, config: GuardrailConfig | None = None):
        self.config = config or GuardrailConfig.load()

    # -- domain / action allowlist -------------------------------------------------

    def check_domain(self, url: str) -> None:
        host = (urlparse(url).hostname or "").lower()
        if not any(host == d or host.endswith("." + d) for d in self.config.allowed_domains):
            raise GuardrailViolation(
                f"navigation to '{host}' is outside the allowlist {self.config.allowed_domains}"
            )

    def check_action_type(self, action: str) -> None:
        if action not in self.config.allowed_actions:
            raise GuardrailViolation(f"action type '{action}' is not in the allowed_actions list")

    # -- risk classification --------------------------------------------------------

    def classify_click(self, visible_text: str) -> ActionCheck:
        text = (visible_text or "").lower()
        for rp in self.config.risky_click_text_patterns:
            if rp.pattern in text:
                allowed = rp.policy != "block"
                return ActionCheck(allowed=allowed, risk=rp.policy, reason=rp.reason)
        return ActionCheck(allowed=True, risk="safe")

    # -- redaction --------------------------------------------------------------------

    def redact(self, text: Any) -> Any:
        if text is None:
            return text
        s = str(text)
        for name, pattern in self.config.pii_patterns:
            s = pattern.sub(f"[REDACTED:{name}]", s)
        return s

    def redact_value_for_field(self, field_name: str, value: Any) -> Any:
        if field_name and field_name.lower() in self.config.never_log_field_names:
            return "[REDACTED:sensitive_field]"
        return self.redact(value)

    def redact_dict(self, d: dict[str, Any]) -> dict[str, Any]:
        return {k: self.redact_value_for_field(k, v) for k, v in d.items()}
