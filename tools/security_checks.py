"""Pure helpers used by black-box security acceptance checks."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

FORBIDDEN_EXECUTION_TERMS = (
    "execute",
    "execution",
    "broker",
    "order",
    "position/close",
)
MUTATING_METHODS = {"post", "put", "patch", "delete"}
FABRICATED_METRIC_PATTERN = re.compile(
    r"(?:"
    r"(?:sharpe|\bdsr\b|expectancy|win\s+rate|drawdown|profitability|"
    r"strategy\s+performance).{0,120}?(?:[<>]=?\s*)?-?\d+(?:\.\d+)?%?"
    r"|"
    r"\b(?:honesty|cue|confidence(?:\s*score)?|win\s+probability|probability|"
    r"validation\s+score|quality\s+score)\b\s*:\s*(?:<[^>]{0,160}>\s*)*"
    r"-?\d+(?:\.\d+)?%?"
    r")",
    re.IGNORECASE | re.DOTALL,
)
REAL_MODE_SOURCE_SUFFIXES = {".ts", ".tsx", ".js", ".jsx"}
ISOLATED_SOURCE_PARTS = {"test", "tests", "__tests__", "fixtures", "mocks"}
ISOLATED_FILENAMES = {"mock-generator.ts"}


def find_live_execution_operations(openapi: dict[str, Any]) -> list[str]:
    """Return mutating operations whose paths resemble trade execution."""

    paths = openapi.get("paths", {})
    if not isinstance(paths, dict):
        return ["<invalid OpenAPI paths object>"]
    return sorted(
        f"{method.upper()} {path}"
        for path, operations in paths.items()
        if isinstance(path, str) and isinstance(operations, dict)
        for method in operations
        if method.casefold() in MUTATING_METHODS
        and any(term in path.casefold() for term in FORBIDDEN_EXECUTION_TERMS)
    )


def find_fabricated_metric_claims(source_root: Path) -> list[str]:
    """Find numeric performance claims exposed by normal frontend source."""

    findings: list[str] = []
    for path in sorted(source_root.rglob("*")):
        if not path.is_file() or path.suffix.casefold() not in REAL_MODE_SOURCE_SUFFIXES:
            continue
        relative = path.relative_to(source_root)
        if path.name.casefold() in ISOLATED_FILENAMES or any(
            part.casefold() in ISOLATED_SOURCE_PARTS for part in relative.parts
        ):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in FABRICATED_METRIC_PATTERN.finditer(text):
            excerpt = " ".join(match.group(0).split())
            findings.append(f"{relative.as_posix()}: {excerpt[:160]}")
    return findings
