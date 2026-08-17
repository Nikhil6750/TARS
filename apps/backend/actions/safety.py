from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Any


class SensitiveContext(str, Enum):
    PASSWORD_INPUT = "PASSWORD_INPUT"
    CREDENTIAL_DIALOG = "CREDENTIAL_DIALOG"
    SECURE_DESKTOP = "SECURE_DESKTOP"
    SYSTEM_CRITICAL = "SYSTEM_CRITICAL"


_SECRET_KEY = re.compile(
    r"(?:password|passwd|passphrase|secret|token|api[_-]?key|authorization|credential|"
    r"cookie|session[_-]?id|private[_-]?key)",
    re.IGNORECASE,
)
_INLINE_SECRET = re.compile(
    r"(?i)\b(password|passwd|passphrase|secret|token|api[_ -]?key|authorization)"
    r"\s*[:=]\s*([^\s,;]+)|\bbearer\s+[A-Za-z0-9._~+/=-]+"
)
_PASSWORD_ROLE = re.compile(r"password|credential|login|sign.?in", re.IGNORECASE)
_SECURE_DESKTOP = re.compile(r"winlogon|secure desktop|credentialui|consent\.exe", re.I)
_SYSTEM_CRITICAL = re.compile(
    r"registry editor|group policy|disk management|services|task scheduler|system32|"
    r"windows\\system32",
    re.IGNORECASE,
)


def redact_sensitive(value: Any, *, key: str = "") -> Any:
    """Return an audit-safe copy without retaining credential-shaped values."""

    if _SECRET_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(name): redact_sensitive(item, key=str(name))
            for name, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_sensitive(item, key=key) for item in value]
    if isinstance(value, str):
        return _INLINE_SECRET.sub(
            lambda match: (
                f"{match.group(1)}=[REDACTED]" if match.group(1) else "Bearer [REDACTED]"
            ),
            value,
        )
    return value


def contains_secret_field(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            _SECRET_KEY.search(str(name)) is not None or contains_secret_field(item)
            for name, item in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(contains_secret_field(item) for item in value)
    return False


def detect_sensitive_context(value: Any) -> set[SensitiveContext]:
    """Conservatively detect safety-relevant UI/browser/vision state."""

    found: set[SensitiveContext] = set()
    _inspect(value, found, key="")
    return found


def _inspect(value: Any, found: set[SensitiveContext], *, key: str) -> None:
    if isinstance(value, Mapping):
        lowered = {str(name).lower(): item for name, item in value.items()}
        if any(bool(lowered.get(name)) for name in ("password", "is_password", "password_input")):
            found.add(SensitiveContext.PASSWORD_INPUT)
        if bool(lowered.get("credential_dialog")):
            found.add(SensitiveContext.CREDENTIAL_DIALOG)
        if bool(lowered.get("secure_desktop")):
            found.add(SensitiveContext.SECURE_DESKTOP)
        if any(
            bool(lowered.get(name))
            for name in ("system_critical", "protected_operation", "requires_admin", "elevated")
        ):
            found.add(SensitiveContext.SYSTEM_CRITICAL)
        for name, item in value.items():
            _inspect(item, found, key=str(name))
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _inspect(item, found, key=key)
        return
    if not isinstance(value, str):
        return

    if key.lower() in {"role", "control_type", "input_type", "field_type", "autocomplete"}:
        if _PASSWORD_ROLE.search(value):
            found.add(SensitiveContext.PASSWORD_INPUT)
    if key.lower() in {"window_title", "desktop", "process", "executable", "dialog_type"}:
        if _SECURE_DESKTOP.search(value):
            found.add(SensitiveContext.SECURE_DESKTOP)
        if _PASSWORD_ROLE.search(value):
            found.add(SensitiveContext.CREDENTIAL_DIALOG)
        if _SYSTEM_CRITICAL.search(value):
            found.add(SensitiveContext.SYSTEM_CRITICAL)
