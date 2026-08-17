from __future__ import annotations

from actions.safety import audit_safe_arguments


def test_redacts_content_field_when_sibling_selector_is_password_shaped():
    result = audit_safe_arguments({"selector": "#password", "text": "hunter2SuperSecret"})
    assert result == {"selector": "#password", "text": "[REDACTED]"}


def test_redacts_content_field_when_control_id_names_a_credential():
    result = audit_safe_arguments({"control_id": "login-card-number", "text": "4111 1111 1111 1111"})
    assert result["text"] == "[REDACTED]"


def test_leaves_ordinary_typed_text_untouched():
    result = audit_safe_arguments({"control_id": "notepad-editor", "text": "Hello from TARS"})
    assert result == {"control_id": "notepad-editor", "text": "Hello from TARS"}


def test_key_based_redaction_still_applies():
    result = audit_safe_arguments({"password": "hunter2", "username": "nikhi"})
    assert result["password"] == "[REDACTED]"
    assert result["username"] == "nikhi"


def test_does_not_mutate_the_original_arguments():
    original = {"selector": "#password", "text": "hunter2SuperSecret"}
    audit_safe_arguments(original)
    assert original == {"selector": "#password", "text": "hunter2SuperSecret"}


def test_passes_through_non_mapping_values():
    assert audit_safe_arguments("not a dict") == "not a dict"
