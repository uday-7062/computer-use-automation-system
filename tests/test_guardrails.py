from pathlib import Path

import pytest

from src.guardrails import GuardrailConfig, Guardrails, GuardrailViolation

FIXTURE_CONFIG = Path(__file__).parent / "fixtures" / "test_guardrails.yaml"


@pytest.fixture
def guardrails() -> Guardrails:
    return Guardrails(GuardrailConfig.load(FIXTURE_CONFIG))


def test_check_domain_allows_listed_domain(guardrails):
    guardrails.check_domain("https://www.saucedemo.com/inventory.html")  # must not raise


def test_check_domain_rejects_unlisted_domain(guardrails):
    with pytest.raises(GuardrailViolation):
        guardrails.check_domain("https://evil.example.com/")


def test_check_action_type_rejects_unknown_action(guardrails):
    with pytest.raises(GuardrailViolation):
        guardrails.check_action_type("delete_everything")


def test_classify_click_blocks_delete(guardrails):
    check = guardrails.classify_click("Delete account")
    assert check.allowed is False
    assert check.risk == "block"


def test_classify_click_requires_confirmation_for_finish(guardrails):
    check = guardrails.classify_click("Finish")
    assert check.allowed is True
    assert check.risk == "require_confirmation"


def test_classify_click_safe_by_default(guardrails):
    check = guardrails.classify_click("Add to cart")
    assert check.allowed is True
    assert check.risk == "safe"


@pytest.mark.parametrize("raw,expected_marker", [
    ("call 555 123-45-6789 now", "[REDACTED:ssn]"),
    ("card 4111 1111 1111 1111 charged", "[REDACTED:credit_card]"),
    ("contact jane.doe@example.com", "[REDACTED:email]"),
    ("account 123456789012", "[REDACTED:long_digit_sequence]"),
])
def test_redact_masks_pii_patterns(guardrails, raw, expected_marker):
    assert expected_marker in guardrails.redact(raw)


def test_redact_value_for_field_masks_sensitive_field_names(guardrails):
    assert guardrails.redact_value_for_field("password", "hunter2") == "[REDACTED:sensitive_field]"
    assert guardrails.redact_value_for_field("item_name", "Sauce Labs Backpack") == "Sauce Labs Backpack"


def test_redact_dict_masks_only_sensitive_keys(guardrails):
    out = guardrails.redact_dict({"password": "hunter2", "username": "standard_user"})
    assert out["password"] == "[REDACTED:sensitive_field]"
    assert out["username"] == "standard_user"
