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
