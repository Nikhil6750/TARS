"""Maps a catalog record's tags/description to one of the fixed Obsidian
category folders (Phase 4). Deliberately a small, fixed set -- "do not make
thousands of empty category folders" -- with everything that doesn't
clearly match going to "Other" rather than guessing.
"""
from __future__ import annotations

import re
from typing import Any

CATEGORIES: tuple[str, ...] = (
    "Coding",
    "Trading",
    "Research",
    "Windows",
    "Productivity",
    "Data-Engineering",
    "AI-Agents",
    "Automation",
    "Other",
)

# Ordered so the first matching category wins -- a skill tagged both
# "python" and "excel" lands under Coding before Productivity, etc. Keys
# are lower-cased tag/keyword substrings checked against each record's
# tags plus name/description.
_CATEGORY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Trading", ("trading", "finance", "stock", "crypto", "forex", "investment", "portfolio", "options", "equity")),
    ("Data-Engineering", ("data-engineering", "etl", "pipeline", "sql", "database", "warehouse", "spark", "airflow", "dbt")),
    ("AI-Agents", ("agent", "llm", "rag", "embedding", "prompt", "langchain", "mcp", "multi-agent")),
    ("Windows", ("windows", "powershell", "win32", "wsl", ".net", "dotnet")),
    ("Automation", ("automation", "workflow", "cron", "scheduler", "ci/cd", "devops", "deploy")),
    ("Coding", ("python", "javascript", "typescript", "java", "rust", "golang", "code", "programming", "git", "api", "testing", "debug")),
    ("Research", ("research", "search", "web", "scrape", "browse", "academic", "paper")),
    ("Productivity", ("productivity", "email", "calendar", "note", "document", "excel", "office", "pdf")),
)


def categorize(record: dict[str, Any]) -> str:
    tags = record.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    haystack = " ".join(
        [
            str(record.get("name", "")),
            str(record.get("description", "")),
            " ".join(str(t) for t in tags),
        ]
    ).lower()

    for category, keywords in _CATEGORY_KEYWORDS:
        if any(kw in haystack for kw in keywords):
            return category
    return "Other"


_DASH_RUN = re.compile(r"-{2,}")


def safe_folder_slug(name: str) -> str:
    """A skill identifier/name turned into a filesystem-safe folder name --
    no path separators, no Windows-reserved characters, no leading/trailing
    dots or spaces (Windows silently mishandles both)."""
    reserved = '<>:"/\\|?*'
    cleaned = "".join(c if c not in reserved and ord(c) >= 32 else "-" for c in name)
    cleaned = cleaned.strip(" .")
    cleaned = "-".join(part for part in cleaned.split() if part)
    cleaned = _DASH_RUN.sub("-", cleaned).strip("-")
    return cleaned or "unnamed-skill"
