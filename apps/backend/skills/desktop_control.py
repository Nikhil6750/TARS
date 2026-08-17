"""`desktop_control` skill -- Windows UI Automation-backed desktop control.

Wave 2B extension of the Wave 2A skill layer (see `skills/windows_app.py`
for the sibling window-level skill this complements: `windows_app` launches
/focuses/lists whole windows, `desktop_control` reaches *inside* a window to
its controls).

Every state-changing action (`invoke_control`, `type_into_control`,
`select_control`) drives a real UI Automation pattern -- there is no
keystroke or blind-coordinate-click simulation, and no fallback to screen
coordinates when a semantic UIA target exists. Read actions
(`read_selected_text`, `read_clipboard`) are explicit, individually-audited
actions, never data silently attached to unrelated requests -- see
`app.action_contracts.ActiveWindowContext`'s docstring for why selected
text/clipboard content are not fields on that struct.

All UI Automation/win32 mechanics live in `skills/_desktop_automation.py`;
this module is argument validation + risk classification + ActionResult
shaping, matching the other skills' shape.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.action_contracts import (
    ActionRequest,
    ActionResult,
    ActionStatus,
    BaseSkill,
    RiskLevel,
    SkillExecutionError,
    SkillValidationError,
)
from skills._desktop_automation import (
    ControlHandleCache,
    build_active_window_context,
    control_from_hwnd,
    do_focus,
    do_invoke,
    do_scroll,
    do_select,
    do_type,
    read_clipboard_text,
    read_selected_text,
    resolve_window,
    serialize_control,
    walk_actionable_controls,
)

try:
    import uiautomation as auto
except ImportError as exc:  # pragma: no cover - exercised only off-Windows
    raise ImportError(
        "desktop_control skill requires uiautomation + comtypes. Install via "
        "apps/backend/requirements.txt (`pip install uiautomation comtypes`)."
    ) from exc

_RISK_BY_ACTION: dict[str, RiskLevel] = {
    "inspect_current_window": RiskLevel.READ_ONLY,
    "list_controls": RiskLevel.READ_ONLY,
    "read_selected_text": RiskLevel.READ_ONLY,
    "read_clipboard": RiskLevel.READ_ONLY,
    "focus_control": RiskLevel.LOW_RISK,
    "scroll_control": RiskLevel.LOW_RISK,
    "invoke_control": RiskLevel.CONFIRM_REQUIRED,
    "type_into_control": RiskLevel.CONFIRM_REQUIRED,
    "select_control": RiskLevel.CONFIRM_REQUIRED,
}

_DIRECTIONS = ("up", "down", "left", "right")
_SCROLL_AMOUNTS = ("small", "large")
_TYPE_MODES = ("replace", "append")
_MAX_TYPE_TEXT_CHARS = 20_000


class DesktopControlSkill(BaseSkill):
    name = "desktop_control"
    capabilities: tuple[str, ...] = tuple(_RISK_BY_ACTION)

    def __init__(self) -> None:
        self._cache = ControlHandleCache()

    def classify_risk(self, action: str, arguments: dict[str, Any]) -> RiskLevel:
        return _RISK_BY_ACTION.get(action, RiskLevel.BLOCKED)

    async def validate(self, action: str, arguments: dict[str, Any]) -> None:
        if action == "inspect_current_window":
            _validate_optional_str(arguments, "target")
            _validate_bool(arguments, "include_controls")
            _validate_bool(arguments, "include_clipboard")
            _validate_int(arguments, "max_controls", minimum=1, maximum=50)
        elif action == "list_controls":
            _validate_optional_str(arguments, "target")
            _validate_int(arguments, "max_controls", minimum=1, maximum=300)
            _validate_int(arguments, "max_depth", minimum=1, maximum=10)
        elif action in ("focus_control", "invoke_control", "select_control"):
            _require_control_id(arguments)
        elif action == "type_into_control":
            _require_control_id(arguments)
            text = arguments.get("text")
            if not isinstance(text, str):
                raise SkillValidationError("type_into_control requires string 'text'")
            if len(text) > _MAX_TYPE_TEXT_CHARS:
                raise SkillValidationError(
                    f"'text' exceeds maximum length of {_MAX_TYPE_TEXT_CHARS} characters"
                )
            mode = arguments.get("mode", "replace")
            if mode not in _TYPE_MODES:
                raise SkillValidationError("'mode' must be 'replace' or 'append'")
        elif action == "scroll_control":
            _require_control_id(arguments)
            if arguments.get("direction") not in _DIRECTIONS:
                raise SkillValidationError("'direction' must be one of up/down/left/right")
            amount = arguments.get("amount", "small")
            if amount not in _SCROLL_AMOUNTS:
                raise SkillValidationError("'amount' must be 'small' or 'large'")
        elif action == "read_selected_text":
            _validate_optional_str(arguments, "control_id")
        elif action == "read_clipboard":
            return
        else:
            raise SkillValidationError(f"unsupported desktop_control action '{action}'")

    async def execute(self, request: ActionRequest) -> ActionResult:
        started = datetime.now(UTC)
        action = request.action
        args = request.arguments

        if action == "inspect_current_window":
            return self._execute_inspect_current_window(request, args, started)
        if action == "list_controls":
            return self._execute_list_controls(request, args, started)
        if action == "focus_control":
            return self._execute_focus_control(request, args, started)
        if action == "invoke_control":
            return self._execute_invoke_control(request, args, started)
        if action == "type_into_control":
            return self._execute_type_into_control(request, args, started)
        if action == "select_control":
            return self._execute_select_control(request, args, started)
        if action == "scroll_control":
            return self._execute_scroll_control(request, args, started)
        if action == "read_selected_text":
            return self._execute_read_selected_text(request, args, started)
        if action == "read_clipboard":
            return self._execute_read_clipboard(request, started)
        raise SkillExecutionError(f"unsupported desktop_control action '{action}'")

    # -- read actions ----------------------------------------------------

    def _execute_inspect_current_window(
        self, request: ActionRequest, args: dict[str, Any], started: datetime
    ) -> ActionResult:
        target = args.get("target")
        include_controls = args.get("include_controls", True)
        include_clipboard = args.get("include_clipboard", False)
        max_controls = int(args.get("max_controls", 20))

        context = build_active_window_context(target)

        selected_text: str | None = None
        try:
            focused = auto.GetFocusedControl()
            if focused is not None:
                selected_text = read_selected_text(focused)
        except SkillExecutionError:
            # Focused control refused (e.g. a password field) -- omit rather
            # than fail the whole snapshot.
            selected_text = None

        controls: list[dict[str, Any]] = []
        truncated = False
        if include_controls:
            hwnd, _, _ = resolve_window(target)
            root = control_from_hwnd(hwnd)
            found, truncated = walk_actionable_controls(root, max_controls=max_controls)
            controls = [
                {**serialize_control(control), "control_id": self._cache.put(control)}
                for control in found
            ]

        clipboard_text: str | None = None
        if include_clipboard:
            clipboard_text = read_clipboard_text()

        data: dict[str, Any] = {
            "active_window": context.model_dump(mode="json"),
            "selected_text": selected_text,
            "controls": controls,
            "controls_truncated": truncated,
        }
        if include_clipboard:
            data["clipboard_text"] = clipboard_text

        return self._result(
            request,
            ActionStatus.SUCCEEDED,
            f"Captured context snapshot for '{context.window_title or context.executable}'.",
            risk_level=RiskLevel.READ_ONLY,
            data=data,
            started_at=started,
        )

    def _execute_list_controls(
        self, request: ActionRequest, args: dict[str, Any], started: datetime
    ) -> ActionResult:
        target = args.get("target")
        max_controls = int(args.get("max_controls", 100))
        max_depth = int(args.get("max_depth", 6))

        hwnd, exe, title = resolve_window(target)
        root = control_from_hwnd(hwnd)
        found, truncated = walk_actionable_controls(
            root, max_depth=max_depth, max_controls=max_controls
        )
        controls = [
            {**serialize_control(control), "control_id": self._cache.put(control)}
            for control in found
        ]
        return self._result(
            request,
            ActionStatus.SUCCEEDED,
            f"Found {len(controls)} actionable control(s) in '{title or exe}'.",
            risk_level=RiskLevel.READ_ONLY,
            data={
                "window": {"executable": exe, "window_title": title},
                "controls": controls,
                "truncated": truncated,
            },
            started_at=started,
        )

    def _execute_read_selected_text(
        self, request: ActionRequest, args: dict[str, Any], started: datetime
    ) -> ActionResult:
        control_id = args.get("control_id")
        resolved: auto.Control | None
        if control_id:
            resolved = self._resolve_control(control_id)
        else:
            resolved = auto.GetFocusedControl()
        if resolved is None:
            raise SkillExecutionError("no control is currently focused")

        text = read_selected_text(resolved)
        return self._result(
            request,
            ActionStatus.SUCCEEDED,
            "Read selected text." if text else "No text is currently selected.",
            risk_level=RiskLevel.READ_ONLY,
            data={"selected_text": text or "", "has_selection": bool(text)},
            started_at=started,
        )

    def _execute_read_clipboard(
        self, request: ActionRequest, started: datetime
    ) -> ActionResult:
        text = read_clipboard_text()
        return self._result(
            request,
            ActionStatus.SUCCEEDED,
            "Read clipboard text." if text is not None else "Clipboard has no text content.",
            risk_level=RiskLevel.READ_ONLY,
            data={"clipboard_text": text, "has_text": text is not None},
            started_at=started,
        )

    # -- control actions ---------------------------------------------------

    def _execute_focus_control(
        self, request: ActionRequest, args: dict[str, Any], started: datetime
    ) -> ActionResult:
        control = self._resolve_control(args["control_id"])
        if not do_focus(control):
            raise SkillExecutionError("SetFocus() reported failure")
        return self._result(
            request,
            ActionStatus.SUCCEEDED,
            "Focused control.",
            risk_level=RiskLevel.LOW_RISK,
            data=serialize_control(control),
            started_at=started,
        )

    def _execute_invoke_control(
        self, request: ActionRequest, args: dict[str, Any], started: datetime
    ) -> ActionResult:
        control = self._resolve_control(args["control_id"])
        info = serialize_control(control)
        do_invoke(control)
        return self._result(
            request,
            ActionStatus.SUCCEEDED,
            f"Invoked {info.get('control_type', 'control')} "
            f"'{info.get('name') or info.get('automation_id') or args['control_id']}'.",
            risk_level=RiskLevel.CONFIRM_REQUIRED,
            data=info,
            started_at=started,
        )

    def _execute_type_into_control(
        self, request: ActionRequest, args: dict[str, Any], started: datetime
    ) -> ActionResult:
        control = self._resolve_control(args["control_id"])
        mode = args.get("mode", "replace")
        do_type(control, args["text"], mode=mode)
        info = serialize_control(control)
        return self._result(
            request,
            ActionStatus.SUCCEEDED,
            f"Typed text into {info.get('control_type', 'control')} ({mode}).",
            risk_level=RiskLevel.CONFIRM_REQUIRED,
            data={**info, "characters_written": len(args["text"]), "mode": mode},
            started_at=started,
        )

    def _execute_select_control(
        self, request: ActionRequest, args: dict[str, Any], started: datetime
    ) -> ActionResult:
        control = self._resolve_control(args["control_id"])
        do_select(control)
        return self._result(
            request,
            ActionStatus.SUCCEEDED,
            "Selected control.",
            risk_level=RiskLevel.CONFIRM_REQUIRED,
            data=serialize_control(control),
            started_at=started,
        )

    def _execute_scroll_control(
        self, request: ActionRequest, args: dict[str, Any], started: datetime
    ) -> ActionResult:
        control = self._resolve_control(args["control_id"])
        direction = args["direction"]
        amount = args.get("amount", "small")
        do_scroll(control, direction=direction, amount=amount)
        return self._result(
            request,
            ActionStatus.SUCCEEDED,
            f"Scrolled {direction} ({amount}).",
            risk_level=RiskLevel.LOW_RISK,
            data={"direction": direction, "amount": amount},
            started_at=started,
        )

    def _resolve_control(self, control_id: str) -> auto.Control:
        control = self._cache.get(control_id)
        if control is None:
            raise SkillExecutionError(
                "control_id is unknown or has expired; call list_controls or "
                "inspect_current_window again to get a fresh one"
            )
        return control


def _validate_optional_str(arguments: dict[str, Any], key: str) -> None:
    value = arguments.get(key)
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise SkillValidationError(f"'{key}' must be a non-empty string or omitted")


def _validate_bool(arguments: dict[str, Any], key: str) -> None:
    if key in arguments and not isinstance(arguments[key], bool):
        raise SkillValidationError(f"'{key}' must be a boolean")


def _validate_int(arguments: dict[str, Any], key: str, *, minimum: int, maximum: int) -> None:
    if key not in arguments:
        return
    value = arguments[key]
    if isinstance(value, bool) or not isinstance(value, int) or not (minimum <= value <= maximum):
        raise SkillValidationError(f"'{key}' must be an integer between {minimum} and {maximum}")


def _require_control_id(arguments: dict[str, Any]) -> None:
    control_id = arguments.get("control_id")
    if not isinstance(control_id, str) or not control_id.strip():
        raise SkillValidationError("requires non-empty string 'control_id'")
