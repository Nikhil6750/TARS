"""`terminal` skill -- runs a shell command and captures real
stdout/stderr/exit code. `classify_risk()` is the conservative, deterministic
classifier the action runtime uses to decide whether to block outright or
require confirmation; it is pure (no I/O) per BaseSkill's contract.

Ordering matters: BLOCKED is checked before READ_ONLY, and any chaining
operator (`&`, `&&`, `|`, `||`, `;`, redirection) disqualifies a command
from the READ_ONLY allowlist even if it starts with a read-only verb --
`dir & del /s` must never classify as READ_ONLY just because it starts with
`dir`.
"""
from __future__ import annotations

import re
import subprocess
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

DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_TIMEOUT_SECONDS = 120.0

# Matched anywhere in the (lowercased) command string, not just as the
# leading verb -- a compound/chained command containing any of these is
# blocked regardless of what it starts with. Conservative by design: err
# toward BLOCKED for anything ambiguous-and-dangerous-looking.
_BLOCKED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bformat\b"),
    re.compile(r"\bdel\s+/s\b"),
    re.compile(r"\berase\s+/s\b"),
    re.compile(r"\brd\s+/s\b"),
    re.compile(r"\brmdir\s+/s\b"),
    re.compile(r"\bremove-item\b(?=.*-recurse)(?=.*-force)"),
    re.compile(r"\bshutdown\b"),
    re.compile(r"\bstop-computer\b"),
    re.compile(r"\brestart-computer\b"),
    re.compile(r"\bdiskpart\b"),
    re.compile(r"\breg(?:\.exe)?\s+delete\b"),
    re.compile(r"\bnet(?:\.exe)?\s+user\b"),
    re.compile(r"\bbcdedit\b"),
    re.compile(r"\bvssadmin\b"),
    re.compile(r"\bcipher\s*/w\b"),
    re.compile(r"\brunas\b"),
    re.compile(r"\bsudo\b"),
    re.compile(r"\btakeown\b"),
    re.compile(r"\bformat-volume\b"),
    re.compile(r"\bclear-disk\b"),
    re.compile(r"\binitialize-disk\b"),
    re.compile(r"\bremove-partition\b"),
)

# "piping to Invoke-Expression/iex from a remote source" -- block only when
# both a remote-exec construct AND a remote-fetch construct are present, so
# `iex $someLocalScript` alone (unusual but not itself a remote-download
# attack) is not blanket-blocked, while the actual dangerous combo is.
_REMOTE_EXEC_PATTERN = re.compile(r"invoke-expression|\biex\b")
_REMOTE_FETCH_PATTERN = re.compile(
    r"https?://|downloadstring|webclient|invoke-restmethod|\birm\b|invoke-webrequest|\biwr\b"
)

# Writing into system-critical directories -- block if the command names a
# system directory AND contains a write/delete-shaped construct.
_SYSTEM_DIR_PATTERN = re.compile(r"c:\\windows\\system32|%windir%\\system32")
_WRITE_INDICATOR_PATTERN = re.compile(
    r">|del\s|erase\s|remove-item|rm\s|copy\s|copy-item|move\s|move-item|"
    r"set-content|out-file|ren\s|rename-item|mkdir|new-item|attrib\s"
)

# Chaining/redirection operators -- any presence disqualifies a command from
# the READ_ONLY allowlist, even if it starts with a read-only verb.
_CHAIN_OPERATOR_PATTERN = re.compile(r"&|\||;|>|<")

_READ_ONLY_SINGLE_VERBS = {
    "dir",
    "ls",
    "type",
    "cat",
    "echo",
    "whoami",
    "pwd",
    "cd",
    "get-childitem",
    "get-process",
    "get-content",
}
_READ_ONLY_TWO_WORD_VERBS = {
    ("git", "status"),
    ("git", "log"),
    ("git", "diff"),
}


def _normalize(command: str) -> str:
    return " ".join(command.strip().split())


def classify_command(command: str) -> RiskLevel:
    """Pure classification: BLOCKED > READ_ONLY > CONFIRM_REQUIRED."""
    normalized = _normalize(command)
    if not normalized:
        return RiskLevel.BLOCKED
    lowered = normalized.lower()

    for pattern in _BLOCKED_PATTERNS:
        if pattern.search(lowered):
            return RiskLevel.BLOCKED
    if _REMOTE_EXEC_PATTERN.search(lowered) and _REMOTE_FETCH_PATTERN.search(lowered):
        return RiskLevel.BLOCKED
    if _SYSTEM_DIR_PATTERN.search(lowered) and _WRITE_INDICATOR_PATTERN.search(lowered):
        return RiskLevel.BLOCKED

    if _is_read_only(lowered):
        return RiskLevel.READ_ONLY

    return RiskLevel.CONFIRM_REQUIRED


def _is_read_only(lowered_command: str) -> bool:
    if _CHAIN_OPERATOR_PATTERN.search(lowered_command):
        return False
    tokens = lowered_command.split()
    if not tokens:
        return False
    if tuple(tokens[:2]) in _READ_ONLY_TWO_WORD_VERBS:
        return True
    return tokens[0] in _READ_ONLY_SINGLE_VERBS


class TerminalSkill(BaseSkill):
    name = "terminal"
    description = "Run an allow-listed shell command and return its output."
    capabilities: tuple[str, ...] = ("run_command",)

    def classify_risk(self, action: str, arguments: dict[str, Any]) -> RiskLevel:
        if action != "run_command":
            return RiskLevel.BLOCKED
        command = arguments.get("command")
        if not isinstance(command, str):
            return RiskLevel.BLOCKED
        return classify_command(command)

    async def validate(self, action: str, arguments: dict[str, Any]) -> None:
        if action != "run_command":
            raise SkillValidationError(f"unsupported terminal action '{action}'")

        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            raise SkillValidationError("run_command requires a non-empty 'command'")

        timeout = arguments.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise SkillValidationError("timeout_seconds must be a number")
        if timeout <= 0:
            raise SkillValidationError("timeout_seconds must be positive")
        if timeout > MAX_TIMEOUT_SECONDS:
            raise SkillValidationError(
                f"timeout_seconds {timeout} exceeds hard max of {MAX_TIMEOUT_SECONDS}"
            )

        # Defense in depth: validate() also refuses BLOCKED commands rather
        # than relying solely on the runtime enforcing classify_risk()'s
        # output.
        if classify_command(command) is RiskLevel.BLOCKED:
            raise SkillValidationError(f"command is blocked by policy: {command!r}")

    async def execute(self, request: ActionRequest) -> ActionResult:
        if request.action != "run_command":
            raise SkillExecutionError(f"unsupported terminal action '{request.action}'")

        command = request.arguments["command"]
        risk = classify_command(command)
        if risk is RiskLevel.BLOCKED:
            # execute() is safe to call directly in tests, but a BLOCKED
            # command is never actually run -- defense in depth even if a
            # caller bypasses validate().
            return self._result(
                request,
                ActionStatus.BLOCKED,
                f"Refused to run blocked command: {command}",
                risk_level=risk,
                data={"command": command},
                error="command matched the destructive/elevated denylist",
            )

        timeout = min(float(request.arguments.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)), MAX_TIMEOUT_SECONDS)
        started = datetime.now(UTC)

        try:
            completed = subprocess.run(  # noqa: S602 - intentional: this skill's entire
                command,                # purpose is running the user-supplied command,
                shell=True,             # already gated by classify_risk()/validate() above.
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            return self._result(
                request,
                ActionStatus.FAILED,
                f"Command timed out after {timeout}s: {command}",
                risk_level=risk,
                data={"command": command, "timeout_seconds": timeout},
                error=str(exc),
                started_at=started,
            )
        except OSError as exc:
            raise SkillExecutionError(f"failed to run command {command!r}: {exc}") from exc

        status = ActionStatus.SUCCEEDED if completed.returncode == 0 else ActionStatus.FAILED
        error = (completed.stderr.strip() or None) if status is ActionStatus.FAILED else None
        return self._result(
            request,
            status,
            f"Ran command: {command} (exit code {completed.returncode})",
            risk_level=risk,
            data={
                "command": command,
                "exit_code": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            },
            error=error,
            started_at=started,
        )
