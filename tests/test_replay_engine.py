from pathlib import Path

import pytest

from src.evidence import EvidenceWriter
from src.guardrails import GuardrailConfig, Guardrails
from src.locator import LocatorResolutionError
from src.replay_engine import ReplayEngine, render_template, verify_checkpoint
from src.schema import (
    ActionType, Artifact, Checkpoint, ErrorClass, ErrorHandler, Locator, LocatorKind,
    LocatorTier, Output, Param, ParamType, Provenance, RunStatus, Step, TargetApp,
)

FIXTURE_CONFIG = Path(__file__).parent / "fixtures" / "test_guardrails.yaml"
FIXTURE_HTML = Path(__file__).parent / "fixtures" / "legacy_app" / "index.html"
FIXTURE_URL = f"file://{FIXTURE_HTML.resolve()}"


def _member_lookup_artifact() -> Artifact:
    member_id_locator = Locator(tiers=[
        LocatorTier(kind=LocatorKind.CSS, value="#memberIdBox", confidence=0.9),
    ])
    lookup_locator = Locator(
        tiers=[LocatorTier(kind=LocatorKind.ROLE_NAME, value="button|Look Up", confidence=0.75)],
        element_label="Look Up",
    )
    balance_locator = Locator(tiers=[LocatorTier(kind=LocatorKind.CSS, value="#balanceText", confidence=0.9)])
    not_found_locator = Locator(tiers=[LocatorTier(kind=LocatorKind.CSS, value="#view-error", confidence=0.9)])

    return Artifact(
        id="member_lookup", version=1, name="Member lookup", description="Look up a member's balance.",
        target=TargetApp(base_url=FIXTURE_URL, allowed_domains=[""], vendor_product="legacy_member_system"),
        params=[Param(name="member_id", type=ParamType.STRING, description="Member ID to look up", example="12345")],
        outputs=[Output(name="balance", type=ParamType.STRING, description="Account balance")],
        steps=[
            Step(id="fill_id", action=ActionType.FILL, locator=member_id_locator, value_template="{{member_id}}"),
            Step(id="click_lookup", action=ActionType.CLICK, locator=lookup_locator),
            Step(id="extract_balance", action=ActionType.EXTRACT, locator=balance_locator, extract_as="balance"),
        ],
        checkpoint=Checkpoint(description="result view visible", text_pattern=r"Balance: \$"),
        error_handlers=[
            ErrorHandler(
                code="MEMBER_NOT_FOUND", description="No member exists with that ID.",
                detect=not_found_locator, error_class=ErrorClass.BUSINESS_OUTCOME,
            )
        ],
        provenance=Provenance(discovery_run_id="test", model="none", goal="look up member"),
    )


@pytest.fixture
def guardrails() -> Guardrails:
    return Guardrails(GuardrailConfig.load(FIXTURE_CONFIG))


def test_render_template_substitutes_known_params():
    assert render_template("id={{member_id}}", {"member_id": "12345"}) == "id=12345"


def test_render_template_missing_param_renders_empty():
    assert render_template("id={{missing}}", {}) == "id="


def test_replay_success_path_extracts_output(guardrails, tmp_path):
    artifact = _member_lookup_artifact()
    engine = ReplayEngine(artifact=artifact, guardrails=guardrails, headless=True)
    evidence = EvidenceWriter("replay", guardrails, root=tmp_path)

    result = engine.run({"member_id": "12345"}, evidence)

    assert result.status == RunStatus.SUCCESS
    assert result.outputs["balance"] == "$4,532.10"
    assert (evidence.dir / "result.json").exists()
    assert (evidence.dir / "run.log.jsonl").exists()
    assert list(evidence.screenshots_dir.glob("*.png"))


def test_replay_business_outcome_for_unknown_member(guardrails, tmp_path):
    artifact = _member_lookup_artifact()
    engine = ReplayEngine(artifact=artifact, guardrails=guardrails, headless=True)
    evidence = EvidenceWriter("replay", guardrails, root=tmp_path)

    result = engine.run({"member_id": "99999"}, evidence)

    assert result.status == RunStatus.BUSINESS_OUTCOME
    assert result.outcome_code == "MEMBER_NOT_FOUND"


def test_replay_missing_required_param_is_hard_failure(guardrails, tmp_path):
    artifact = _member_lookup_artifact()
    engine = ReplayEngine(artifact=artifact, guardrails=guardrails, headless=True)
    evidence = EvidenceWriter("replay", guardrails, root=tmp_path)

    result = engine.run({}, evidence)

    assert result.status == RunStatus.HARD_FAILURE
    assert "member_id" in result.message


def test_replay_never_writes_raw_password_to_evidence_log(guardrails, tmp_path):
    artifact = _member_lookup_artifact()
    artifact.params.append(Param(name="password", type=ParamType.STRING, required=False,
                                  sensitive=True, description="unused, present to test redaction"))
    engine = ReplayEngine(artifact=artifact, guardrails=guardrails, headless=True)
    evidence = EvidenceWriter("replay", guardrails, root=tmp_path)

    engine.run({"member_id": "12345", "password": "super-secret-value"}, evidence)

    log_contents = (evidence.dir / "run.log.jsonl").read_text()
    assert "super-secret-value" not in log_contents


def test_replay_bad_locator_is_hard_failure_not_a_crash(guardrails, tmp_path):
    artifact = _member_lookup_artifact()
    artifact.steps[0].locator = Locator(tiers=[LocatorTier(kind=LocatorKind.CSS, value="#does-not-exist", confidence=0.9)])
    engine = ReplayEngine(artifact=artifact, guardrails=guardrails, headless=True)
    evidence = EvidenceWriter("replay", guardrails, root=tmp_path)

    result = engine.run({"member_id": "12345"}, evidence)

    assert result.status == RunStatus.HARD_FAILURE
    assert result.failed_step_id == "fill_id"
