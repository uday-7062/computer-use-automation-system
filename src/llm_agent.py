"""The discovery loop: an LLM drives a live browser (observe -> decide -> act) to
accomplish a natural-language goal, and the successful trace is compiled into a
reusable Artifact.

Perception is DOM/accessibility-tree based (src/perception.py) plus a screenshot for
grounding -- not raw coordinate-only computer-use -- because the target environment
(Section 1 of the brief) is legacy enterprise UI where the accessibility tree is
often the only stable signal. The model acts by referencing element refs from the
latest snapshot ("[e7]"), never by inventing selectors itself; every acted-on element
already carries the same ranked locator-tier chain that replay will use, so the
artifact's robustness reasoning is attached at the moment of the action, not
reverse-engineered afterward.

Every action -- the model's included -- passes through the same Guardrails instance
replay uses (domain allowlist, click-risk classification, redaction), so a run can
never leave the sandboxed target even if the model tries.

LLM provider: OpenAI (chat.completions with function/tool calling + vision). The
brief leaves provider choice open ("LLM provider / model... is your call"); the tool
schema and message-construction logic below are the only provider-specific parts --
everything downstream (perception, guardrails, artifact compilation) is provider-
agnostic and consumes a normalized `ToolUse(id, name, input)`, so swapping providers
means editing `_TOOLS` and `_call_model()` only.
"""
from __future__ import annotations

import base64
import json
import os
import time
import uuid
from dataclasses import dataclass

from openai import OpenAI, RateLimitError
from playwright.sync_api import Page, sync_playwright

from . import perception
from .escalation import Escalator
from .evidence import EvidenceWriter
from .guardrails import Guardrails, GuardrailViolation
from .schema import (
    ActionType, Artifact, Checkpoint, Locator, LocatorKind, Output, Param, Provenance,
    RiskLevel, RunResult, RunStatus, Step, TargetApp,
)

DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "act",
            "description": "Act on a specific interactive element from the latest observation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": "Element ref from the observation, e.g. 'e7'."},
                    "action": {"type": "string", "enum": ["click", "fill", "select", "press_key"]},
                    "value": {"type": "string", "description": "Text to type/select/press. Omit for click."},
                    "param_name": {
                        "type": "string",
                        "description": "Set this whenever the action is specific to one of the goal's "
                                       "declared parameter values -- including a CLICK, e.g. clicking "
                                       "'Add to cart' for a specific product named by the `item_name` "
                                       "parameter. Give that parameter's name here so the recorded "
                                       "capability targets the parameter (e.g. re-clickable for ANY "
                                       "item_name on replay) instead of hardcoding this one element. "
                                       "Do not set it for actions that would be identical regardless of "
                                       "parameter values (e.g. clicking a generic 'Continue' button).",
                    },
                    "reasoning": {"type": "string", "description": "One sentence: why this action, and why this element is a reliable target."},
                },
                "required": ["ref", "action", "reasoning"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "navigate",
            "description": "Navigate the browser to a URL.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}, "reasoning": {"type": "string"}},
                "required": ["url", "reasoning"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "extract",
            "description": "Read data off the page into a named output the capability will return.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string"},
                    "output_name": {"type": "string"},
                    "attr": {"type": "string", "enum": ["text", "value", "href"]},
                    "reasoning": {"type": "string"},
                },
                "required": ["ref", "output_name", "reasoning"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Declare the goal reached (or unreachable). Only call with success=true once "
                            "you can see the goal's end state on screen.",
            "parameters": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "summary": {"type": "string"},
                    "checkpoint_text": {
                        "type": "string",
                        "description": "A short, distinctive piece of visible text on the final page that "
                                        "proves success (used as the replay checkpoint). Required if success=true.",
                    },
                },
                "required": ["success", "summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "escalate",
            "description": "Call this if you are stuck: an unexpected state, ambiguous UI, or an action "
                            "you should not take without a human (e.g. anything destructive/irreversible).",
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        },
    },
]


def _compact_old_observations(messages: list[dict]) -> None:
    """Cap context growth: keep only the two most recent OBSERVATION messages
    (text + screenshot) in full; collapse older ones to a one-line text summary.

    This directly fixes a real failure hit during development of this system: an
    earlier version kept every turn's full element list and image in the
    conversation forever, and by turn ~17 a single request exceeded this account's
    30k-tokens/minute rate limit (openai.RateLimitError, TPM exceeded). The model
    only needs recent screen state in detail; it already has its own past
    reasoning and tool calls in the (cheap, text-only) assistant/tool messages.
    """
    obs_indices = [
        i for i, m in enumerate(messages)
        if m.get("role") == "user" and isinstance(m.get("content"), list)
    ]
    for i in obs_indices[:-2]:
        content = messages[i]["content"]
        text_block = next((c for c in content if c.get("type") == "text"), None)
        summary = text_block["text"].splitlines()[0] if text_block else "OBSERVATION"
        messages[i] = {"role": "user", "content": f"{summary} [older observation trimmed to save context]"}


def _parameterize_locator(locator: Locator, param_name: str, param_value: str) -> Locator:
    """Templates a captured locator's tiers against the real parameter value used
    in this run, so the recorded capability targets the *parameter*, not the one
    concrete element clicked during discovery.

    This exists because of a real bug caught during development: tagging a CLICK
    with `param_name` only templated `Step.value_template` (meaningful for typed
    text), while the element that gets clicked is determined entirely by the
    *locator* -- so an "add to cart" click recorded for one product silently
    stayed hardcoded to that product's data-test id no matter what `item_name`
    a replay was given. Fix: search each tier's value for the literal param value
    or its slug (lower-cased, hyphenated -- how this app derives data-test ids
    from display names) and template it; any tier that doesn't contain either is
    ambiguous across different param values (e.g. a generic "Add to cart" role
    name shared by every product) and is dropped rather than kept as a fallback
    that could silently resolve to the wrong element.
    """
    slug = param_value.strip().lower().replace(" ", "-") if param_value else ""
    kept: list = []
    for t in locator.tiers:
        v = t.value
        if param_value and param_value in v:
            kept.append(t.model_copy(update={"value": v.replace(param_value, "{{" + param_name + "}}")}))
        elif slug and slug in v:
            kept.append(t.model_copy(update={"value": v.replace(slug, "{{" + param_name + "|slug}}")}))
        elif t.kind == LocatorKind.COORDINATES:
            kept.append(t)
    if not kept:
        kept = list(locator.tiers)
    return locator.model_copy(update={"tiers": kept})


@dataclass
class ToolUse:
    """Normalized tool call, shaped identically regardless of LLM provider."""
    id: str
    name: str
    input: dict


@dataclass
class DiscoveryConfig:
    goal: str
    capability_id: str
    capability_name: str
    capability_description: str
    target_base_url: str
    params: list[Param]
    param_values: dict[str, str]
    declared_outputs: list[Output]
    vendor_product: str
    max_steps: int = 25
    headless: bool = False
    model: str = DEFAULT_MODEL


class DiscoveryAgent:
    def __init__(self, guardrails: Guardrails, cdp_port: int = 9222):
        self.guardrails = guardrails
        self.cdp_port = cdp_port
        self.client = OpenAI()

    def _system_prompt(self, cfg: DiscoveryConfig) -> str:
        params_desc = "\n".join(
            f"  - {p.name} ({p.type.value}): {p.description} = {cfg.param_values.get(p.name, p.example)!r}"
            for p in cfg.params
        )
        return f"""You are operating a real web browser to accomplish a goal, one action at a time.

GOAL: {cfg.goal}

You were given these parameter values for this run -- whenever an action (typing,
selecting, OR clicking) is specific to one of these values, ALWAYS pass its
`param_name` in the `act` call, so the recorded capability is reusable for a
DIFFERENT value on replay instead of being hardcoded to this one run. This applies
just as much to clicks as to typed text: e.g. if a parameter names which product to
click "Add to cart" on, that click's `param_name` must be set to that parameter --
otherwise the recorded capability will always click the same product regardless of
what a future caller asks for. Only omit `param_name` for actions that would be
identical no matter what these values are (e.g. clicking a generic "Continue" or
"Login" button).
{params_desc or '  (none)'}

Rules:
- Only interact with elements listed in the latest observation, by their [ref].
- Only navigate within this application; do not attempt to leave the target site.
- Take exactly one tool call per turn. After each action you will be shown a fresh observation.
- If the page shows an error, unexpected dialog, or you cannot find a way forward,
  call `escalate` rather than guessing repeatedly.
- Never attempt destructive or payment-completing actions (e.g. a final "Place Order"
  / "Finish" / "Delete" button) without first calling `escalate` to get confirmation.
- When you can see the goal has been reached, call `finish` with success=true and a
  `checkpoint_text` that is short, exact, visible text proving it.
- Call `finish` with success=false if you determine the goal is truly not achievable.
- You must always respond by calling exactly one of the provided tools -- never with
  plain text.
"""

    def _call_model(self, cfg: DiscoveryConfig, messages: list[dict]) -> tuple[ToolUse | None, dict]:
        _compact_old_observations(messages)

        last_exc: Exception | None = None
        response = None
        for attempt in range(5):
            try:
                response = self.client.chat.completions.create(
                    model=cfg.model, max_tokens=1024, tools=_TOOLS, tool_choice="auto",
                    parallel_tool_calls=False, messages=messages,
                )
                break
            except RateLimitError as exc:
                last_exc = exc
                time.sleep(12 * (attempt + 1))
        if response is None:
            raise last_exc  # type: ignore[misc]

        message = response.choices[0].message
        assistant_msg: dict = {"role": "assistant", "content": message.content}
        if message.tool_calls:
            assistant_msg["tool_calls"] = [tc.model_dump() for tc in message.tool_calls]
            tc = message.tool_calls[0]
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            return ToolUse(id=tc.id, name=tc.function.name, input=args), assistant_msg
        return None, assistant_msg

    def run(self, cfg: DiscoveryConfig) -> tuple[Artifact | None, RunResult]:
        start = time.time()
        run_id = f"discovery_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        evidence = EvidenceWriter("discovery", self.guardrails, run_id=run_id)
        evidence.log("discovery_started", {"goal": cfg.goal, "model": cfg.model, "target": cfg.target_base_url})

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=cfg.headless, args=[f"--remote-debugging-port={self.cdp_port}"])
            context = browser.new_context(viewport={"width": 1280, "height": 800})
            page = context.new_page()
            escalator = Escalator(evidence, self.guardrails, f"http://127.0.0.1:{self.cdp_port}")

            try:
                artifact, result = self._loop(page, cfg, evidence, escalator, run_id)
            finally:
                browser.close()

        result.duration_ms = int((time.time() - start) * 1000)
        result.evidence_dir = str(evidence.dir)
        evidence.write_result(result.model_dump(mode="json"))
        if artifact:
            evidence.write_artifact_copy(artifact.model_dump(mode="json"))
        return artifact, result

    def _loop(self, page: Page, cfg: DiscoveryConfig, evidence: EvidenceWriter, escalator: Escalator, run_id: str):
        self.guardrails.check_domain(cfg.target_base_url)
        page.goto(cfg.target_base_url, timeout=self.guardrails.config.navigation_timeout_ms)
        evidence.screenshot(page, "start")

        messages: list[dict] = [{"role": "system", "content": self._system_prompt(cfg)}]
        trace: list[Step] = []
        consecutive_failures = 0
        sensitive_param_names = {p.name for p in cfg.params if p.sensitive}

        for turn in range(cfg.max_steps):
            snap = perception.snapshot(page)
            evidence.screenshot(page, f"turn{turn:02d}")
            image_b64 = base64.b64encode(page.screenshot()).decode()

            observation_text = snap.to_prompt_text()
            evidence.log("observation", {"turn": turn, "url": snap.url, "element_count": len(snap.elements)})

            messages.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": f"OBSERVATION (turn {turn}):\n{observation_text}"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}", "detail": "low"}},
                ],
            })

            tool_use, assistant_msg = self._call_model(cfg, messages)
            messages.append(assistant_msg)

            if tool_use is None:
                messages.append({"role": "user", "content": "Please respond by calling one of the provided tools."})
                continue

            logged_input = tool_use.input
            if logged_input.get("param_name") in sensitive_param_names and "value" in logged_input:
                logged_input = {**logged_input, "value": "[REDACTED:sensitive_param]"}
            evidence.log("model_action", {"turn": turn, "tool": tool_use.name, "input": logged_input})

            if tool_use.name == "finish":
                result = self._handle_finish(tool_use.input, page, evidence)
                if result.status == RunStatus.SUCCESS:
                    artifact = self._compile_artifact(cfg, trace, tool_use.input, run_id, page.url)
                    return artifact, result
                return None, result

            if tool_use.name == "escalate":
                req = escalator.request_and_wait(
                    page, run_id=run_id, goal=cfg.goal, capability_id=cfg.capability_id,
                    step_id=f"turn{turn}", reason=tool_use.input.get("reason", "model requested escalation"),
                )
                tool_result_text = f"A human took over and resolved this: {req.resolution_notes}. Actions taken: {req.human_actions}. Continue from the current state."
                messages.append({"role": "tool", "tool_call_id": tool_use.id, "content": tool_result_text})
                consecutive_failures = 0
                continue

            try:
                step, tool_result_text = self._handle_act_like(page, snap, tool_use, evidence, cfg)
                consecutive_failures = 0
                if step is not None:
                    trace.append(step)
            except (GuardrailViolation, ValueError, Exception) as exc:  # noqa: BLE001
                consecutive_failures += 1
                tool_result_text = f"Action failed: {exc}"
                evidence.log("action_failed", {"turn": turn, "error": str(exc)})
                if consecutive_failures >= self.guardrails.config.max_consecutive_failures_before_escalation:
                    req = escalator.request_and_wait(
                        page, run_id=run_id, goal=cfg.goal, capability_id=cfg.capability_id,
                        step_id=f"turn{turn}", reason=f"{consecutive_failures} consecutive action failures",
                    )
                    tool_result_text += f"\nA human intervened: {req.resolution_notes}."
                    consecutive_failures = 0

            messages.append({"role": "tool", "tool_call_id": tool_use.id, "content": tool_result_text})

        return None, RunResult(status=RunStatus.HARD_FAILURE, message=f"max_steps ({cfg.max_steps}) exceeded without finish")

    def _handle_act_like(self, page: Page, snap: perception.Snapshot, tool_use: ToolUse, evidence: EvidenceWriter, cfg: DiscoveryConfig):
        inp = tool_use.input
        el = snap.by_ref(inp["ref"]) if "ref" in inp else None
        if el is None:
            raise ValueError(f"unknown ref {inp.get('ref')!r}; use a ref from the latest OBSERVATION")

        if tool_use.name == "navigate":
            self.guardrails.check_domain(inp["url"])
            page.goto(inp["url"], timeout=self.guardrails.config.navigation_timeout_ms)
            step = Step(id=f"s{len(page.frames)}_{uuid.uuid4().hex[:4]}", action=ActionType.NAVIGATE,
                        value_template=inp["url"], notes=inp.get("reasoning"))
            return step, f"navigated to {inp['url']}"

        if tool_use.name == "extract":
            resolved = _resolve_for_perception(page, el)
            attr = inp.get("attr", "text")
            val = resolved.inner_text() if attr == "text" else (resolved.input_value() if attr == "value" else resolved.get_attribute("href"))
            step = Step(id=f"s_{uuid.uuid4().hex[:6]}", action=ActionType.EXTRACT, locator=el.locator,
                        extract_as=inp["output_name"], extract_attr=attr, notes=inp.get("reasoning"))
            return step, f"extracted {inp['output_name']!r} = {val!r}"

        # act: click / fill / select / press_key
        action = inp["action"]
        if action == "click":
            check = self.guardrails.classify_click(el.name)
            if not check.allowed:
                raise GuardrailViolation(f"blocked by policy ({check.reason}); choose a different action")
            risk = RiskLevel.RISKY if check.risk in ("require_confirmation", "flag") else RiskLevel.SAFE
            if check.risk == "require_confirmation":
                raise GuardrailViolation(
                    f"'{el.name}' requires human confirmation ({check.reason}); call escalate instead of clicking directly"
                )
        else:
            risk = RiskLevel.SAFE

        resolved = _resolve_for_perception(page, el)
        value = inp.get("value")
        if action == "click":
            resolved.click(timeout=8000)
        elif action == "fill":
            resolved.fill(value or "", timeout=8000)
        elif action == "select":
            resolved.select_option(value, timeout=8000)
        elif action == "press_key":
            resolved.press(value or "Enter", timeout=8000)
        else:
            raise ValueError(f"unknown action {action!r}")

        value_template = value
        step_locator = el.locator
        param_name = inp.get("param_name")
        if param_name:
            value_template = "{{" + param_name + "}}"
            if action == "click":
                # For a click, the param determines WHICH element gets targeted
                # (e.g. item_name picks one of several "Add to cart" buttons) --
                # the locator itself must be templated. See _parameterize_locator.
                # For fill/select/press_key the param is instead the VALUE typed
                # into an already-uniquely-identified field (e.g. the username
                # box); its locator must NOT be touched, since the field's
                # identity has nothing to do with what value gets typed into it
                # -- doing so would strip every tier that doesn't happen to
                # contain the typed value and leave nothing but COORDINATES.
                real_value = cfg.param_values.get(param_name, "")
                step_locator = _parameterize_locator(el.locator, param_name, real_value)

        action_type = {"click": ActionType.CLICK, "fill": ActionType.FILL,
                        "select": ActionType.SELECT, "press_key": ActionType.PRESS_KEY}[action]
        step = Step(id=f"s_{uuid.uuid4().hex[:6]}", action=action_type, locator=step_locator,
                    value_template=value_template, risk_level=risk, notes=inp.get("reasoning"))
        return step, f"{action} on {el.describe()} succeeded"

    def _handle_finish(self, inp: dict, page: Page, evidence: EvidenceWriter) -> RunResult:
        if not inp.get("success"):
            return RunResult(status=RunStatus.HARD_FAILURE, message=inp.get("summary", "model reported failure"))
        evidence.screenshot(page, "goal_reached")
        return RunResult(status=RunStatus.SUCCESS, message=inp.get("summary", "goal reached"),
                          outputs={}, observed=inp.get("checkpoint_text"))

    def _compile_artifact(self, cfg: DiscoveryConfig, trace: list[Step], finish_input: dict, run_id: str, final_url: str) -> Artifact:
        checkpoint_text = finish_input.get("checkpoint_text") or finish_input.get("summary", "")
        import re as _re
        from urllib.parse import urlparse
        # The checkpoint is keyed on the URL path, not the model's literal
        # `checkpoint_text` -- that text can itself be parameter-dependent (e.g.
        # a displayed total that varies with which item was added), which would
        # make the checkpoint fail on a legitimate success for a different
        # `item_name`. URL path is the more parameter-independent success signal
        # for a routed web app; `checkpoint_text` is kept only as a human-readable
        # description of what was actually seen on screen.
        path = urlparse(final_url).path
        checkpoint = Checkpoint(
            description=f"Reached {path!r} (observed: {checkpoint_text!r})",
            url_pattern=_re.escape(path) if path else None,
        )
        return Artifact(
            id=cfg.capability_id,
            version=1,
            name=cfg.capability_name,
            description=cfg.capability_description,
            target=TargetApp(
                base_url=cfg.target_base_url,
                allowed_domains=self.guardrails.config.allowed_domains,
                vendor_product=cfg.vendor_product,
                surface="web",
            ),
            params=cfg.params,
            outputs=cfg.declared_outputs,
            steps=trace,
            checkpoint=checkpoint,
            error_handlers=[],
            provenance=Provenance(discovery_run_id=run_id, model=cfg.model, goal=cfg.goal),
        )


def _resolve_for_perception(page: Page, el: perception.ElementInfo):
    from .locator import resolve as _resolve
    return _resolve(page, el.locator, timeout_ms=8000).playwright_locator
