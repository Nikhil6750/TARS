from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from app.action_contracts import RiskLevel, Skill

_RISK_ORDER = {
    RiskLevel.READ_ONLY: 0,
    RiskLevel.LOW_RISK: 1,
    RiskLevel.CONFIRM_REQUIRED: 2,
    RiskLevel.BLOCKED: 3,
}

_KNOWN_ACTION_POLICY: dict[str, dict[str, RiskLevel]] = {
    "windows_app": {
        "launch": RiskLevel.LOW_RISK,
        "focus": RiskLevel.LOW_RISK,
        "list_running": RiskLevel.READ_ONLY,
    },
    "browser": {
        "open_url": RiskLevel.LOW_RISK,
        "search": RiskLevel.LOW_RISK,
    },
    "filesystem": {
        "list": RiskLevel.READ_ONLY,
        "list_files": RiskLevel.READ_ONLY,
        "search": RiskLevel.READ_ONLY,
        "search_files": RiskLevel.READ_ONLY,
        "open": RiskLevel.LOW_RISK,
        "open_file": RiskLevel.LOW_RISK,
        "open_folder": RiskLevel.LOW_RISK,
    },
    "obsidian": {
        "search": RiskLevel.READ_ONLY,
        "read": RiskLevel.READ_ONLY,
        "read_note": RiskLevel.READ_ONLY,
    },
}

_BLOCKED_ACTION = re.compile(
    r"(?:^|_)(?:delete|remove|erase|destroy|format|wipe|elevate|shutdown|reboot|"
    r"kill|terminate|write_registry|change_permissions)(?:$|_)",
    re.IGNORECASE,
)
_STATE_CHANGING_ACTION = re.compile(
    r"(?:^|_)(?:run|execute|write|create|update|install|uninstall|move|rename|copy|"
    r"send|post|upload|download)(?:$|_)",
    re.IGNORECASE,
)
_BLOCKED_TERMINAL = re.compile(
    r"(?:\b(?:format|diskpart|bcdedit|shutdown|restart-computer|stop-computer|runas|"
    r"takeown|set-executionpolicy|stop-process|taskkill)\b|"
    r"\b(?:reg|sc)\s+(?:delete|stop)\b|\bnet\s+user\b|"
    r"\b(?:del|erase|rmdir|rd|rm|ri|remove-item)\b|"
    r"\bgit\s+(?:clean\b|reset\s+--hard\b)|"
    r"\bwmic\b[^\r\n]*\bterminate\b|"
    r"\bpowershell(?:\.exe)?\b[^\r\n]*(?:-encodedcommand|-enc\b))",
    re.IGNORECASE,
)
_READ_ONLY_TERMINAL = re.compile(
    r"^\s*(?:dir|ls|pwd|whoami|hostname|where|where\.exe|"
    r"get-childitem|get-location|get-process|get-service|get-content|"
    r"select-string|resolve-path|test-path)(?:\s|$)",
    re.IGNORECASE,
)


class PermissionEngine:
    """Derive an authoritative risk without accepting caller-supplied permission flags."""

    def classify(self, skill: Skill, action: str, arguments: dict[str, Any]) -> RiskLevel:
        try:
            declared = skill.classify_risk(action, arguments)
            if not isinstance(declared, RiskLevel):
                declared = RiskLevel(str(declared))
        except Exception:
            return RiskLevel.BLOCKED

        policy = self._runtime_policy(skill.name, action, arguments)
        return max((declared, policy), key=_RISK_ORDER.__getitem__)

    def _runtime_policy(
        self, skill_name: str, action: str, arguments: dict[str, Any]
    ) -> RiskLevel:
        # These fields are never evidence of authorization. Ignore their values and
        # continue deriving risk solely from the requested operation.
        if _contains_blocked_content(arguments):
            return RiskLevel.BLOCKED

        if skill_name == "terminal":
            if action != "run_command":
                return RiskLevel.BLOCKED
            return _classify_terminal(arguments.get("command"))

        known_skill = _KNOWN_ACTION_POLICY.get(skill_name)
        if known_skill is not None:
            return known_skill.get(action, RiskLevel.BLOCKED)

        if _BLOCKED_ACTION.search(action):
            return RiskLevel.BLOCKED
        if _STATE_CHANGING_ACTION.search(action):
            return RiskLevel.CONFIRM_REQUIRED
        return RiskLevel.READ_ONLY


def _classify_terminal(command: Any) -> RiskLevel:
    if not isinstance(command, str) or not command.strip() or "\x00" in command:
        return RiskLevel.BLOCKED
    if _BLOCKED_TERMINAL.search(command):
        return RiskLevel.BLOCKED
    if _READ_ONLY_TERMINAL.search(command) and not re.search(r">|\btee-object\b", command, re.I):
        return RiskLevel.READ_ONLY
    return RiskLevel.CONFIRM_REQUIRED


def _contains_blocked_content(value: Any, *, key: str = "") -> bool:
    if isinstance(value, Mapping):
        lowered = {str(name).lower(): item for name, item in value.items()}
        if any(
            bool(lowered.get(name))
            for name in ("elevated", "run_as_admin", "requires_admin", "system_critical")
        ):
            return True
        return any(_contains_blocked_content(item, key=str(name)) for name, item in value.items())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_blocked_content(item, key=key) for item in value)
    if not isinstance(value, str):
        return False

    # Targets and filenames may legitimately contain words such as "delete".
    # Interpret text as an operation only when it occupies an operation-bearing key.
    if key.lower() not in {"command", "operation", "verb", "action", "script"}:
        return False
    return bool(
        _BLOCKED_ACTION.search(value)
        or _BLOCKED_TERMINAL.search(value)
        or re.search(
            r"\b(?:delete|remove|erase|destroy|format|wipe|elevate|shutdown|reboot|kill)\b",
            value,
            re.IGNORECASE,
        )
    )
