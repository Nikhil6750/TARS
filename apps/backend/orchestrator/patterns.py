"""Deterministic text-intent patterns the TARS Orchestrator checks before
falling through to `AssistantRouter` (which itself checks its own smaller
set of deterministic patterns before calling the configured
AssistantProvider). Kept separate from `orchestrator.py` so the pattern
list stays easy to audit/extend on its own.
"""
from __future__ import annotations

import re

REMEMBER = re.compile(
    r"^\s*(?:tars,?\s*)?remember(?:\s+that)?\s+(.+)$", re.IGNORECASE | re.DOTALL
)
TRADING_CONTEXT = re.compile(
    r"\btrading\s+context\b|\bwhat'?s\s+my\s+trading\s+context\b|\bstrategy\s+status\b",
    re.IGNORECASE,
)
EXPLAIN_SETUP = re.compile(
    r"\bexplain\s+(?:the\s+|my\s+)?setup\s+for\s+([A-Za-z0-9._-]+)\b", re.IGNORECASE
)
SEARCH_TRADING_MEMORY = re.compile(
    r"\bsearch\s+trading\s+(?:memory|observations?)\s+for\s+(.+)$"
    r"|\bfind\s+trading\s+observations?\s+(?:about|for)\s+(.+)$",
    re.IGNORECASE,
)
SAVE_TRADING_OBSERVATION = re.compile(
    r"^\s*(?:tars,?\s*)?(?:save|log|note)\s+(?:this\s+|a\s+)?trading\s+observation:?\s+(.+)$",
    re.IGNORECASE | re.DOTALL,
)
OPEN_TRADINGVIEW = re.compile(r"\bopen\s+trading\s*view\b", re.IGNORECASE)
FOCUS_TRADINGVIEW = re.compile(r"\bfocus\s+trading\s*view\b", re.IGNORECASE)
ANALYZE_CHART = re.compile(
    r"\banalyz(?:e|ing)\s+(?:the\s+|this\s+|my\s+)?(?:active\s+)?chart\b"
    r"|\blook\s+at\s+(?:the\s+|this\s+|my\s+)?chart\b",
    re.IGNORECASE,
)
SETUP_TRADING_WORKSPACE = re.compile(
    r"\b(?:set\s*up|prepare|open)\s+(?:my\s+)?trading\s+workspace\b", re.IGNORECASE
)

# ---- Skill registry (Hermes catalog + Obsidian-installed skills) --------
# Deterministic, no LLM: these are simple explicit commands per the task
# ("Simple explicit commands should NOT require an LLM"), and even the
# "best X skill" phrasing is resolved by the existing local ranking
# (skill_registry.db.search_catalog: exact match > trust > FTS relevance),
# not semantic interpretation -- so no provider call is needed here either.

SKILL_LIST_INSTALLED = re.compile(
    r"\b(?:show|list)\s+(?:my\s+)?installed\s+skills?\b"
    r"|\bwhat\s+skills?\s+(?:do\s+i\s+have\s+)?installed\b",
    re.IGNORECASE,
)
SKILL_UPDATE_ALL = re.compile(r"\bupdate\s+(?:my\s+)?skills\b", re.IGNORECASE)
SKILL_UPDATE_ONE = re.compile(
    r"\bupdate\s+(?:the\s+|my\s+)?(.+?)\s+skill\b|\bupdate\s+skill\s+(\S+)", re.IGNORECASE
)
SKILL_INSTALL_EXACT = re.compile(r"\binstall\s+skill\s+(\S+)", re.IGNORECASE)
SKILL_INSTALL_TOPIC = re.compile(
    r"\binstall\s+(?:the\s+)?(?:best\s+|top\s+)?(.+?)\s+skill\b", re.IGNORECASE
)
SKILL_UNINSTALL = re.compile(
    # Referential phrasing ("remove that skill") must be checked before the
    # named-target alternative -- otherwise "that"/"this"/"it" itself gets
    # captured as if it were a literal skill name (observed directly: a
    # backtracking optional prefix let "that" leak into the capture group).
    r"\b(?:remove|uninstall|delete)\s+(?:that|this|it)\s+skill\b"
    r"|\b(?:remove|uninstall|delete)\s+(?:the\s+)?(.+?)\s+skill\b",
    re.IGNORECASE,
)
SKILL_SEARCH = re.compile(
    r"\bfind\s+(?:me\s+)?(?:a\s+|the\s+best\s+)?skill(?:s)?\s+(?:for|about|to)\s+(.+)"
    r"|\bsearch\s+(?:the\s+entire\s+)?skill\s+catalog\s+for\s+(.+)"
    r"|\bwhat\s+(.+?)\s+skills?\s+do\s+(?:you|i)\s+have\b",
    re.IGNORECASE,
)
SKILL_USE = re.compile(
    r"\buse\s+(?:a\s+|the\s+)?(.+?)\s+skill\s+(?:for|to)\s+(.+)"
    r"|\buse\s+it\s+to\s+(.+)",
    re.IGNORECASE,
)
SKILL_CONFIRM = re.compile(
    r"^\s*(?:confirm(?:\s+install)?|yes(?:,)?\s*(?:install|do it|go ahead)?|approve|go ahead)\s*\.?\s*$",
    re.IGNORECASE,
)
SKILL_DENY = re.compile(r"^\s*(?:cancel|no|deny|don'?t)\s*\.?\s*$", re.IGNORECASE)
