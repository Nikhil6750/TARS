"""Mission: P0 Trusted Desktop Control -- the deterministic authority
(PermissionEngine) is the ONE place that decides whether an action needs
confirmation; these tests pin its exact boundary so a future change can't
silently widen or narrow it without a test noticing.
"""
from __future__ import annotations

from app.action_contracts import RiskLevel
from actions.permissions import PermissionEngine
from skills.desktop_control import DesktopControlSkill
from skills.windows_app import WindowsAppSkill


def test_trusted_desktop_control_defaults_off():
    engine = PermissionEngine()
    assert engine.trusted_desktop_control is False


def test_invoke_control_requires_confirmation_when_trusted_control_is_off():
    engine = PermissionEngine(trusted_desktop_control=False)
    skill = DesktopControlSkill()
    risk = engine.classify(skill, "invoke_control", {"control_id": "c1", "label": "Start button"})
    assert risk == RiskLevel.CONFIRM_REQUIRED


def test_invoke_control_auto_allowed_for_an_ordinary_click_when_trusted_control_is_on():
    """'Click that' / 'switch to Chrome' -- an ordinary, reversible local
    click must not keep asking for confirmation once the user has granted
    trusted desktop control."""
    engine = PermissionEngine(trusted_desktop_control=True)
    skill = DesktopControlSkill()
    for label in ("Start button", "Chrome taskbar icon", "Settings icon", "the Bluetooth tab", "Scroll to right"):
        risk = engine.classify(skill, "invoke_control", {"control_id": "c1", "label": label})
        assert risk == RiskLevel.LOW_RISK, f"label={label!r} should auto-allow, got {risk}"


def test_select_control_also_auto_allowed_when_trusted_control_is_on():
    engine = PermissionEngine(trusted_desktop_control=True)
    skill = DesktopControlSkill()
    risk = engine.classify(skill, "select_control", {"control_id": "c1", "label": "the Apps tab"})
    assert risk == RiskLevel.LOW_RISK


def test_consequential_labels_still_require_confirmation_even_when_trusted_control_is_on():
    """Mission section 5/6: Trusted Desktop Control must NEVER bypass a
    consequential click just because it is technically local -- Tier 3
    stays Tier 3 regardless of the setting."""
    engine = PermissionEngine(trusted_desktop_control=True)
    skill = DesktopControlSkill()
    consequential_labels = [
        "Buy Now", "Place order", "Send", "Submit", "Pay now", "Delete account",
        "Confirm purchase", "Install", "Uninstall", "Sign in", "Log in", "Checkout",
        "Transfer funds", "Share this document", "Upload",
    ]
    for label in consequential_labels:
        risk = engine.classify(skill, "invoke_control", {"control_id": "c1", "label": label})
        assert risk == RiskLevel.CONFIRM_REQUIRED, f"label={label!r} must still require confirmation, got {risk}"


def test_type_into_control_is_never_downgraded_by_trusted_control():
    """Mission: 'Do NOT weaken arbitrary typing protection' -- typing stays
    CONFIRM_REQUIRED unconditionally, trusted mode or not, benign-looking
    label or not. This is the exact boundary that keeps Trusted Desktop
    Control from becoming a generic keyboard-injection bypass."""
    engine = PermissionEngine(trusted_desktop_control=True)
    skill = DesktopControlSkill()
    risk = engine.classify(skill, "type_into_control", {"control_id": "c1", "text": "hello", "label": "Notepad text box"})
    assert risk == RiskLevel.CONFIRM_REQUIRED


def test_trusted_control_never_downgrades_a_blocked_action():
    """A BLOCKED action (destructive keywords, terminal danger patterns,
    etc.) must stay BLOCKED regardless of the trusted-control setting --
    the override only ever applies to an action that was CONFIRM_REQUIRED,
    never to one the policy already refuses outright."""
    engine = PermissionEngine(trusted_desktop_control=True)
    skill = DesktopControlSkill()
    risk = engine.classify(skill, "invoke_control", {"control_id": "c1", "label": "Start button", "elevated": True})
    assert risk == RiskLevel.BLOCKED


def test_tier0_read_only_actions_are_unaffected_by_trusted_control_either_way():
    skill = DesktopControlSkill()
    for trusted in (False, True):
        engine = PermissionEngine(trusted_desktop_control=trusted)
        for action in ("inspect_current_window", "inspect_screen", "list_controls", "read_selected_text", "read_clipboard"):
            assert engine.classify(skill, action, {}) == RiskLevel.READ_ONLY


def test_tier1_open_focus_close_are_already_auto_allow_without_trusted_control():
    """Opening/focusing/closing an application was already LOW_RISK before
    this mission -- Trusted Desktop Control did not need to touch this
    path, only the generic click/select path."""
    engine = PermissionEngine(trusted_desktop_control=False)
    skill = WindowsAppSkill()
    for action in ("launch", "focus", "close", "open_start"):
        assert engine.classify(skill, action, {"target": "chrome"}) == RiskLevel.LOW_RISK


def test_terminal_commands_still_require_confirmation_regardless_of_trusted_control():
    """Mission Test P6: Trusted Desktop Control must not leak into
    terminal command classification at all -- that path doesn't even look
    at the flag."""
    from skills.terminal import TerminalSkill

    engine = PermissionEngine(trusted_desktop_control=True)
    skill = TerminalSkill()
    risk = engine.classify(skill, "run_command", {"command": "del important_file.txt"})
    assert risk == RiskLevel.BLOCKED
    risk = engine.classify(skill, "run_command", {"command": "notepad.exe"})
    assert risk == RiskLevel.CONFIRM_REQUIRED
