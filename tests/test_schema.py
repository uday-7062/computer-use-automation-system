import json

from src.schema import (
    ActionType, Artifact, Checkpoint, Locator, LocatorKind, LocatorTier, Output,
    Param, ParamType, Provenance, RiskLevel, Step, TargetApp,
)


def _sample_artifact() -> Artifact:
    loc = Locator(tiers=[
        LocatorTier(kind=LocatorKind.TEST_ID, value='[data-test="login-button"]', confidence=0.95),
        LocatorTier(kind=LocatorKind.ROLE_NAME, value="button|Login", confidence=0.7),
    ])
    step = Step(id="s1", action=ActionType.CLICK, locator=loc, risk_level=RiskLevel.SAFE, notes="stable test id")
    return Artifact(
        id="demo_capability", version=1, name="Demo", description="A demo capability.",
        target=TargetApp(base_url="https://example.com", allowed_domains=["example.com"], vendor_product="demo"),
        params=[Param(name="username", type=ParamType.STRING, description="user")],
        outputs=[Output(name="balance", type=ParamType.STRING, description="balance")],
        steps=[step],
        checkpoint=Checkpoint(description="on dashboard", url_pattern=r"/dashboard"),
        provenance=Provenance(discovery_run_id="run1", model="test-model", goal="do the thing"),
    )


def test_artifact_round_trips_through_json():
    artifact = _sample_artifact()
    raw = artifact.model_dump_json()
    restored = Artifact.model_validate_json(raw)
    assert restored == artifact


def test_artifact_key_includes_version():
    artifact = _sample_artifact()
    assert artifact.artifact_key() == "demo_capability.v1"


def test_locator_reasoning_summary_orders_tiers_as_declared():
    loc = Locator(tiers=[
        LocatorTier(kind=LocatorKind.TEST_ID, value="#a", confidence=0.9),
        LocatorTier(kind=LocatorKind.TEXT, value="Submit", confidence=0.5),
    ])
    assert loc.reasoning_summary() == "test_id:'#a' > text:'Submit'"


def test_schema_version_is_pinned():
    artifact = _sample_artifact()
    assert artifact.schema_version == "1.0"
    assert json.loads(artifact.model_dump_json())["schema_version"] == "1.0"
