"""`calculator` skill -- mission: P0 regression section 11-13. Strict
parser tests run on every platform (pure Python, no win32); UIA-interaction
tests are Windows-only and mock the UIA layer directly (no real Calculator
needed), matching test_skill_windows_app.py's pattern.
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from app.action_contracts import ActionRequest, ActionSource, ActionStatus, RiskLevel, SkillValidationError
from skills.calculator import CalculatorSkill, compute_expected, validate_expression


def _request(expression: str) -> ActionRequest:
    return ActionRequest(skill="calculator", action="calculate", arguments={"expression": expression},
                         source=ActionSource.voice_wake_word)


# ---- strict parser (runs everywhere, no win32 needed) ----------------------

def test_validate_expression_accepts_plain_arithmetic():
    assert validate_expression("2345*17") == "2345*17"
    assert validate_expression("(1250 + 750) / 4") == "(1250 + 750) / 4"
    assert validate_expression("10 % 3") == "10 % 3"
    assert validate_expression("3.5 + 2.25") == "3.5 + 2.25"


@pytest.mark.parametrize("bad", [
    "2345*17; import os",
    "__import__('os').system('dir')",
    "os.system('dir')",
    "open('x')",
    "2345*17 and True",
    "[1,2,3]",
    "{'a': 1}",
    "lambda: 1",
    "2345**17",  # ** (power) deliberately not in the allowed-char whitelist
])
def test_validate_expression_rejects_anything_not_plain_arithmetic(bad):
    with pytest.raises(SkillValidationError):
        validate_expression(bad)


def test_validate_expression_rejects_empty():
    with pytest.raises(SkillValidationError):
        validate_expression("")
    with pytest.raises(SkillValidationError):
        validate_expression("   ")


def test_validate_expression_rejects_leading_unary_minus():
    """Calculator's own button model has no direct "type a leading minus"
    action -- deliberately not supported, matching what the mission's own
    examples need (plain binary expressions only)."""
    with pytest.raises(SkillValidationError):
        validate_expression("-5 + 3")


def test_validate_expression_rejects_syntactically_invalid_text():
    with pytest.raises(SkillValidationError):
        validate_expression("2345 * * 17")
    with pytest.raises(SkillValidationError):
        validate_expression("(1250 + 750")  # unbalanced


def test_compute_expected_matches_the_missions_own_examples():
    assert compute_expected("2345*17") == 39865
    assert compute_expected("(1250 + 750) / 4") == 500


def test_calculator_never_executes_arbitrary_code_via_eval():
    """Security invariant: the only eval() call in this module operates on
    an ast tree that _SafeArithmeticVisitor has already walked and proven
    contains nothing but numeric Constants and the five permitted BinOp
    operators -- never raw text, never a Name/Call/Attribute node."""
    from pathlib import Path

    source = Path(__import__("skills.calculator", fromlist=["x"]).__file__).read_text(encoding="utf-8")
    eval_calls = [line for line in source.splitlines() if "eval(" in line and "# noqa" in line]
    assert len(eval_calls) == 1  # exactly the one documented, AST-gated call
    assert "compile(tree" in eval_calls[0]


def test_classify_risk_is_low_risk_never_confirm_required():
    """Mission section 11/13: deliberately LOW_RISK, not a route around
    desktop_control's confirmation requirement for everything else."""
    skill = CalculatorSkill()
    assert skill.classify_risk("calculate", {"expression": "1+1"}) == RiskLevel.LOW_RISK
    assert skill.classify_risk("delete_everything", {}) == RiskLevel.BLOCKED


async def test_validate_rejects_bad_expression_before_any_ui_interaction():
    skill = CalculatorSkill()
    with pytest.raises(SkillValidationError):
        await skill.validate("calculate", {"expression": "import os"})
    with pytest.raises(SkillValidationError):
        await skill.validate("calculate", {"expression": 12345})  # not even a string


# ---- UIA interaction (mocked -- no real Calculator needed) -----------------

class _FakeButton:
    def __init__(self, automation_id, exists=True):
        self.automation_id = automation_id
        self._exists = exists

    def Exists(self, *_args):
        return self._exists



@pytest.mark.skipif(sys.platform != "win32", reason="UI Automation is Windows-only")
async def test_execute_calculate_matches_mission_example_2345_times_17():
    invoked = []

    def fake_find_button(root, automation_id):
        return _FakeButton(automation_id)

    def fake_read_result(root):
        return 39865.0

    with patch("skills.calculator.resolve_window", return_value=(555, "ApplicationFrameHost.exe", "Calculator")), \
         patch("skills.calculator._activate_and_verify_foreground", return_value=True), \
         patch("skills.calculator.control_from_hwnd", return_value=MagicMock()), \
         patch("skills.calculator._clear", side_effect=lambda root: invoked.append("clear")), \
         patch("skills.calculator._find_button", side_effect=fake_find_button), \
         patch("skills.calculator.do_invoke", side_effect=lambda c: invoked.append(c.automation_id)), \
         patch("skills.calculator._read_result", side_effect=fake_read_result), \
         patch("skills.calculator.time.sleep"):
        skill = CalculatorSkill()
        result = await skill.execute(_request("2345*17"))

    assert result.status == ActionStatus.SUCCEEDED
    assert result.data["outcome"] == "SUCCESS"
    assert result.data["result"] == 39865.0
    assert invoked == ["clear", "num2Button", "num3Button", "num4Button", "num5Button",
                       "multiplyButton", "num1Button", "num7Button", "equalButton"]


@pytest.mark.skipif(sys.platform != "win32", reason="UI Automation is Windows-only")
def test_clear_falls_back_to_clear_entry_button_when_clear_button_is_absent():
    """Reproduced live: Windows Calculator swaps 'Clear' (clearButton) for
    'Clear entry' (clearEntryButton) once a result/entry is showing --
    e.g. right after a previous calculation. A hardcoded single id
    intermittently failed with "button not found" depending on which one
    was currently present."""
    from skills.calculator import _clear

    invoked = []

    def fake_control(*, searchFromControl, AutomationId, searchDepth):
        # clearButton absent (simulates Calculator mid-entry), clearEntryButton present
        exists = AutomationId == "clearEntryButton"
        return _FakeButton(AutomationId, exists=exists)

    with patch("skills.calculator.auto.Control", side_effect=fake_control), \
         patch("skills.calculator.do_invoke", side_effect=lambda c: invoked.append(c.automation_id)):
        _clear(MagicMock())

    assert invoked == ["clearEntryButton"]


@pytest.mark.skipif(sys.platform != "win32", reason="UI Automation is Windows-only")
def test_clear_prefers_clear_button_when_both_are_present():
    from skills.calculator import _clear

    invoked = []

    def fake_control(*, searchFromControl, AutomationId, searchDepth):
        return _FakeButton(AutomationId, exists=True)

    with patch("skills.calculator.auto.Control", side_effect=fake_control), \
         patch("skills.calculator.do_invoke", side_effect=lambda c: invoked.append(c.automation_id)):
        _clear(MagicMock())

    assert invoked == ["clearButton"]


@pytest.mark.skipif(sys.platform != "win32", reason="UI Automation is Windows-only")
def test_clear_raises_truthfully_when_neither_button_exists():
    from skills.calculator import _clear

    with patch("skills.calculator.auto.Control", return_value=_FakeButton("x", exists=False)):
        with pytest.raises(Exception, match="clearButton.*clearEntryButton"):
            _clear(MagicMock())


@pytest.mark.skipif(sys.platform != "win32", reason="UI Automation is Windows-only")
async def test_execute_calculate_second_mission_example_with_parentheses():
    def fake_find_button(root, automation_id):
        return _FakeButton(automation_id)

    with patch("skills.calculator.resolve_window", return_value=(555, "ApplicationFrameHost.exe", "Calculator")), \
         patch("skills.calculator._activate_and_verify_foreground", return_value=True), \
         patch("skills.calculator.control_from_hwnd", return_value=MagicMock()), \
         patch("skills.calculator._clear"), \
         patch("skills.calculator._find_button", side_effect=fake_find_button), \
         patch("skills.calculator.do_invoke"), \
         patch("skills.calculator._read_result", return_value=500.0), \
         patch("skills.calculator.time.sleep"):
        skill = CalculatorSkill()
        result = await skill.execute(_request("(1250 + 750) / 4"))

    assert result.status == ActionStatus.SUCCEEDED
    assert result.data["result"] == 500.0


@pytest.mark.skipif(sys.platform != "win32", reason="UI Automation is Windows-only")
async def test_execute_fails_truthfully_when_foreground_cannot_be_verified():
    with patch("skills.calculator.resolve_window", return_value=(555, "ApplicationFrameHost.exe", "Calculator")), \
         patch("skills.calculator._activate_and_verify_foreground", return_value=False):
        skill = CalculatorSkill()
        result = await skill.execute(_request("2+2"))

    assert result.status == ActionStatus.FAILED
    assert result.data["outcome"] == "FOREGROUND_VERIFICATION_FAILED"


@pytest.mark.skipif(sys.platform != "win32", reason="UI Automation is Windows-only")
async def test_execute_never_claims_success_on_a_mismatched_displayed_result():
    """Mission section 12: never report completion merely because buttons
    were clicked -- the displayed result must match the computed one."""
    def fake_find_button(root, automation_id):
        return _FakeButton(automation_id)

    with patch("skills.calculator.resolve_window", return_value=(555, "ApplicationFrameHost.exe", "Calculator")), \
         patch("skills.calculator._activate_and_verify_foreground", return_value=True), \
         patch("skills.calculator.control_from_hwnd", return_value=MagicMock()), \
         patch("skills.calculator._clear"), \
         patch("skills.calculator._find_button", side_effect=fake_find_button), \
         patch("skills.calculator.do_invoke"), \
         patch("skills.calculator._read_result", return_value=999999.0), \
         patch("skills.calculator.time.sleep"):
        skill = CalculatorSkill()
        result = await skill.execute(_request("2+2"))

    assert result.status == ActionStatus.FAILED
    assert result.data["outcome"] == "RESULT_MISMATCH"
    assert result.data["expected"] == 4


# ---- security regression: the Calculator exception stays narrowly scoped ---

def test_calculator_skill_can_never_target_an_arbitrary_app_or_control():
    """Mission section 13: the Calculator exception must remain narrowly
    scoped -- this skill has no concept of a 'target app' or 'control_id'
    argument at all, unlike desktop_control.type_into_control/
    invoke_control, so there is no way to redirect it at anything other
    than the one hardcoded 'calculator' window it resolves itself."""
    import inspect

    from skills.calculator import CalculatorSkill as _Skill

    source = inspect.getsource(_Skill)
    assert "control_id" not in source
    assert 'resolve_window("calculator")' in source
    assert "arguments[" not in source or "expression" in source  # only ever reads 'expression'


def test_calculator_capabilities_are_exactly_one_action():
    skill = CalculatorSkill()
    assert skill.capabilities == ("calculate",)
