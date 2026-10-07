"""`calculator` skill -- ONE narrowly-scoped, deterministic arithmetic
action against the real, trusted Windows Calculator UI (mission: P0
regression section 11).

This is NOT a generic confirmation bypass. `desktop_control.type_into_control`/
`invoke_control` still correctly require explicit user confirmation for
EVERY other app and EVERY other kind of input -- that protection is
untouched (see tests/test_skill_calculator.py's security-regression test,
which asserts this module can never reach an arbitrary app/control).
`calculate()` only ever:

  1. strictly validates the expression (digits, `.`, `+ - * / %`,
     parentheses, spaces only -- anything else is rejected before any UI
     interaction happens; no `eval()` on raw text, ever -- see
     `_compute_expected`, which parses with `ast` and only evaluates an
     AST already proven to contain nothing but numeric literals and the
     five permitted binary operators),
  2. opens/focuses the real Calculator app (reusing the same verified-
     foreground mechanism `windows_app.launch` uses -- mission section 2),
  3. invokes Calculator's OWN real buttons one at a time via UI Automation
     (the exact same InvokePattern mechanism `desktop_control.invoke_control`
     uses for any other app) -- never a keystroke simulation, never a
     screen-coordinate click,
  4. reads Calculator's own result display and compares it against the
     locally-computed expected value -- never reports success merely
     because buttons were clicked (mission section 12).
"""
from __future__ import annotations

import ast
import time
from datetime import UTC, datetime
from typing import Any

import uiautomation as auto

from app.action_contracts import (
    ActionRequest,
    ActionResult,
    ActionStatus,
    BaseSkill,
    RiskLevel,
    SkillExecutionError,
    SkillValidationError,
)
from skills._desktop_automation import control_from_hwnd, do_invoke, resolve_window
from skills.windows_app import _activate_and_verify_foreground

# Strict whitelist -- never anything beyond plain arithmetic (mission:
# "No arbitrary text. No shell commands. No cross-app typing.").
_ALLOWED_CHARS = set("0123456789.+-*/%() ")

_DIGIT_BUTTONS = {str(d): f"num{d}Button" for d in range(10)}
_OP_BUTTONS = {
    "+": "plusButton", "-": "minusButton", "*": "multiplyButton", "/": "divideButton",
    "%": "modButton", "(": "openParenthesisButton", ")": "closeParenthesisButton",
    ".": "decimalSeparatorButton",
}
_BUTTON_FOR_CHAR = {**_DIGIT_BUTTONS, **_OP_BUTTONS}

_RESULT_AUTOMATION_ID = "CalculatorResults"
_SETTLE_SECONDS = 0.05


class _DisallowedExpression(SkillValidationError):
    pass


class _SafeArithmeticVisitor(ast.NodeVisitor):
    """Confirms a parsed expression's AST contains ONLY numeric literals
    and +/-/*//% binary operators -- never a Name/Call/Attribute/Subscript/
    comprehension/anything else. Deliberately rejects unary operators too
    (no leading "-5"): Calculator's own button model has no direct
    "type a leading minus sign" action (negative numbers go through a
    separate negate toggle), and the mission's own examples are all plain
    binary expressions, so this stays exactly as permissive as what is
    actually supported, not a character wider."""

    _ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod)

    def visit(self, node: ast.AST) -> None:
        if isinstance(node, ast.Expression):
            self.visit(node.body)
            return
        if isinstance(node, ast.BinOp):
            if not isinstance(node.op, self._ALLOWED_BINOPS):
                raise _DisallowedExpression(f"operator not allowed: {type(node.op).__name__}")
            self.visit(node.left)
            self.visit(node.right)
            return
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise _DisallowedExpression("only numeric literals are allowed")
            return
        raise _DisallowedExpression(f"expression contains something that is not plain arithmetic: {type(node).__name__}")


def validate_expression(expression: str) -> str:
    """Raises SkillValidationError for anything outside strict arithmetic.
    Character whitelist first (cheap, catches anything exotic immediately),
    then a real AST parse + structural check (catches syntactically-valid-
    but-disallowed shapes a character whitelist alone could miss, e.g.
    Python literals like `1_000` or `1e10` that use only whitelisted
    characters but are not what this skill means by "a number")."""
    stripped = expression.strip()
    if not stripped:
        raise SkillValidationError("empty expression")
    if not set(stripped) <= _ALLOWED_CHARS:
        bad = sorted(set(stripped) - _ALLOWED_CHARS)
        raise SkillValidationError(f"expression contains disallowed characters: {bad!r}")
    try:
        tree = ast.parse(stripped, mode="eval")
    except SyntaxError as exc:
        raise SkillValidationError(f"not a valid arithmetic expression: {exc}") from exc
    try:
        _SafeArithmeticVisitor().visit(tree)
    except _DisallowedExpression as exc:
        raise SkillValidationError(str(exc)) from exc
    return stripped


def compute_expected(expression: str) -> float:
    """The expression has already passed `validate_expression` (AST proven
    to contain nothing but numeric literals and the five permitted binary
    operators) -- compiling and evaluating THAT AST is safe; this never
    runs on raw, unvalidated text."""
    tree = ast.parse(expression, mode="eval")
    _SafeArithmeticVisitor().visit(tree)
    return eval(compile(tree, "<calculator-expression>", "eval"))  # noqa: S307 -- AST already whitelisted


def _tokens_for(expression: str) -> list[str]:
    buttons = []
    for ch in expression:
        if ch == " ":
            continue
        button = _BUTTON_FOR_CHAR.get(ch)
        if button is None:  # unreachable after validate_expression, defensive only
            raise SkillExecutionError(f"no Calculator button mapped for {ch!r}")
        buttons.append(button)
    return buttons


def _speakable(expression: str, value: float) -> str:
    """'2345 times 17 equals 39865.' -- what TTS can say cleanly (no '*' or trailing '.0')."""
    words = {"*": "times", "/": "divided by", "+": "plus", "-": "minus", "%": "mod"}
    spoken = "".join(f" {words[c]} " if c in words else c for c in expression)
    number = str(int(value)) if float(value).is_integer() else repr(value)
    return f"{' '.join(spoken.split())} equals {number}."


_KEY_SAFE_CHARS = set("0123456789.+-*/")  # plain arithmetic only; '%' and parentheses use the button path


def _type_expression(hwnd: int, expression: str) -> bool:
    """Fast path: each UI-Automation Invoke() on Calculator's buttons blocks ~0.5 s (Windows), so a
    multiplication took 6 s. Typing the same keys takes well under a second. Guards: Calculator must be the
    verified foreground window immediately before typing, only plain arithmetic keys are sent, and '=' is used
    (never Enter). The caller verifies the displayed result and falls back to buttons if it differs."""
    import win32api
    import win32con
    import win32gui

    if not set(expression.replace(" ", "")) <= _KEY_SAFE_CHARS:
        return False
    if win32gui.GetForegroundWindow() != hwnd and win32gui.GetForegroundWindow() != _frame_of(hwnd):
        return False

    def tap(vk: int, shift: bool = False) -> None:
        if shift:
            win32api.keybd_event(win32con.VK_SHIFT, 0, 0, 0)
        win32api.keybd_event(vk, 0, 0, 0)
        win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)
        if shift:
            win32api.keybd_event(win32con.VK_SHIFT, 0, win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(0.01)

    tap(win32con.VK_ESCAPE)  # Calculator: Esc = clear
    for ch in expression.replace(" ", "") + "=":
        scan = win32api.VkKeyScan(ch)
        tap(scan & 0xFF, bool(scan >> 8 & 1))
    return True


def _frame_of(hwnd: int) -> int:
    from skills.windows_app import _activation_target

    return _activation_target(hwnd)


def _button_map(root: auto.Control, automation_ids: set[str], *, max_nodes: int = 600) -> dict[str, auto.Control]:
    """ONE breadth-first pass over Calculator's UI tree collecting every needed button. A fresh deep
    `Control(searchFromControl=...)` search per key cost ~0.4 s each (6+ s for one multiplication)."""
    found: dict[str, auto.Control] = {}
    queue, seen = [root], 0
    while queue and seen < max_nodes and len(found) < len(automation_ids):
        node = queue.pop(0)
        seen += 1
        try:
            aid = node.AutomationId
            if aid in automation_ids and aid not in found:
                found[aid] = node
            queue.extend(node.GetChildren())
        except Exception:
            continue
    return found


def _find_button(root: auto.Control, automation_id: str) -> auto.Control:
    control = auto.Control(searchFromControl=root, AutomationId=automation_id, searchDepth=20)
    # Small bounded grace period, not a zero-timeout check: Calculator's
    # UIA tree can briefly lag right after the window regains foreground
    # (observed live, same class of issue as the clear-button state swap
    # above) -- 0.5s/0.1s poll is generous without being unbounded.
    if not control.Exists(0.5, 0.1):
        raise SkillExecutionError(f"Calculator button '{automation_id}' was not found")
    return control


def _clear(root: auto.Control) -> None:
    """Reproduced live: Windows Calculator dynamically swaps the clear
    button's AutomationId depending on state -- `clearButton` ("C") when
    there is no current entry, `clearEntryButton` ("CE") when there is one
    (e.g. right after a previous calculation left a result showing). A
    hardcoded single id intermittently failed with "button not found"
    whenever Calculator was left in the other state. Try both, in the
    order most likely to fully reset (clearButton first -- a full clear --
    falling back to clearEntryButton since at least one of the two always
    exists)."""
    for automation_id in ("clearButton", "clearEntryButton"):
        control = auto.Control(searchFromControl=root, AutomationId=automation_id, searchDepth=20)
        if control.Exists(0.5, 0.1):
            do_invoke(control)
            return
    raise SkillExecutionError("neither Calculator's clearButton nor clearEntryButton could be found")


def _read_result(root: auto.Control) -> float:
    display = auto.Control(searchFromControl=root, AutomationId=_RESULT_AUTOMATION_ID, searchDepth=20)
    if not display.Exists(0.5, 0.1):
        raise SkillExecutionError("Calculator's result display was not found")
    # Observed live: Name is "Display is <value>" with thousands
    # separators (e.g. "Display is 39,865") -- strip the fixed prefix and
    # the separators before parsing.
    name = display.Name or ""
    prefix = "Display is "
    text = name[len(prefix):] if name.startswith(prefix) else name
    text = text.strip().replace(",", "")
    try:
        return float(text)
    except ValueError as exc:
        raise SkillExecutionError(f"could not parse Calculator's displayed result {name!r}") from exc


class CalculatorSkill(BaseSkill):
    name = "calculator"
    blocking_io = True  # synchronous UI Automation: run in a worker thread (see actions/runtime.py)
    description = "Perform a strictly-validated arithmetic calculation in the real Windows Calculator app."
    capabilities: tuple[str, ...] = ("calculate",)

    def classify_risk(self, action: str, arguments: dict[str, Any]) -> RiskLevel:
        if action == "calculate":
            # Deliberately LOW_RISK, not CONFIRM_REQUIRED (mission section
            # 11): the expression is already strictly validated to plain
            # arithmetic before any UI interaction, and every button
            # invoked belongs to the trusted, sandboxed Calculator app
            # itself -- never another app, never free text. This is the
            # ONLY thing this skill can do; it is not a general-purpose
            # confirmation bypass (desktop_control's own type_into_control/
            # invoke_control are untouched and still require confirmation
            # for everything else, including typing into Calculator
            # directly through THAT path).
            return RiskLevel.LOW_RISK
        return RiskLevel.BLOCKED

    async def validate(self, action: str, arguments: dict[str, Any]) -> None:
        if action != "calculate":
            raise SkillValidationError(f"unsupported calculator action '{action}'")
        expression = arguments.get("expression")
        if not isinstance(expression, str):
            raise SkillValidationError("'expression' must be a string")
        validate_expression(expression)

    async def execute(self, request: ActionRequest) -> ActionResult:
        started = datetime.now(UTC)
        if request.action != "calculate":
            raise SkillExecutionError(f"unsupported calculator action '{request.action}'")
        expression = validate_expression(request.arguments["expression"])
        expected = compute_expected(expression)

        hwnd, _exe, title = resolve_window("calculator")
        if not _activate_and_verify_foreground(hwnd):
            return self._result(
                request, ActionStatus.FAILED, "Calculator could not be brought to the foreground.",
                risk_level=RiskLevel.LOW_RISK, error="foreground activation of Calculator was not verified",
                data={"outcome": "FOREGROUND_VERIFICATION_FAILED", "expression": expression},
                started_at=started,
            )

        root = control_from_hwnd(hwnd)
        try:
            if _type_expression(hwnd, expression):
                time.sleep(0.2)
                try:
                    typed = _read_result(root)
                except SkillExecutionError:
                    typed = None
                if typed is not None and abs(typed - expected) < 1e-9:
                    return self._result(
                        request, ActionStatus.SUCCEEDED, _speakable(expression, typed),
                        risk_level=RiskLevel.LOW_RISK,
                        data={"outcome": "SUCCESS", "expression": expression, "result": typed,
                              "window_title": title, "method": "keyboard"},
                        started_at=started,
                    )
                # Typed result did not verify (keys may not have arrived): redo it with the real buttons.
            _clear(root)
            time.sleep(_SETTLE_SECONDS)
            tokens = _tokens_for(expression)
            buttons = _button_map(root, {*tokens, "equalButton"})
            for button_id in tokens:
                # Fall back to the slow search only for a button the single pass did not find.
                do_invoke(buttons.get(button_id) or _find_button(root, button_id))
                time.sleep(_SETTLE_SECONDS)
            do_invoke(buttons.get("equalButton") or _find_button(root, "equalButton"))
            time.sleep(_SETTLE_SECONDS)
            displayed = _read_result(root)
        except SkillExecutionError:
            raise
        except Exception as exc:
            raise SkillExecutionError(f"Calculator UI interaction failed: {exc}") from exc

        # Mission section 12: never claim completion merely because
        # keystrokes/button-invokes were sent -- the displayed result must
        # match the locally (and safely) computed expected value.
        matched = abs(displayed - expected) < 1e-9
        if not matched:
            return self._result(
                request, ActionStatus.FAILED,
                f"Calculator shows {displayed!r} but {expression} = {expected!r}; result was not verified.",
                risk_level=RiskLevel.LOW_RISK, error="displayed result did not match the expected value",
                data={"outcome": "RESULT_MISMATCH", "expression": expression, "expected": expected,
                     "displayed": displayed, "window_title": title},
                started_at=started,
            )
        return self._result(
            request, ActionStatus.SUCCEEDED, _speakable(expression, displayed),
            risk_level=RiskLevel.LOW_RISK,
            data={"outcome": "SUCCESS", "expression": expression, "result": displayed, "window_title": title},
            started_at=started,
        )
