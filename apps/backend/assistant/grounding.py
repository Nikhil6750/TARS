"""Builds the grounding text handed to every AssistantProvider alongside a
user query — the mechanism behind ARCHITECTURE.md's "trading facts always
come from deterministic state, never model invention". The provider is
told plainly that this block is the only source of trading facts and to
say so when asked about something outside it.
"""
from __future__ import annotations

import json
from typing import Any

SYSTEM_PROMPT_PREAMBLE = (
    "You are TARS, a trading companion assistant. You are not a trading "
    "system and you never place trades. You must never invent or estimate "
    "an entry price, stop loss, take profit, risk:reward ratio, strategy "
    "validation result, performance figure, or reason code. The only "
    "trading facts you may state are the ones given to you verbatim in the "
    "CURRENT STATE block below. Every retrieved memory note below carries a "
    "source_id — cite it (e.g. \"per notes/risk.md\") when you use that "
    "note's content. If the user asks about something not present there, "
    "say plainly that you don't have that information — do not guess or "
    "estimate."
)


def build_system_context(
    active_setups: list[dict[str, Any]],
    memory_notes: list[dict[str, Any]] | None = None,
) -> str:
    payload: dict[str, Any] = {"active_setups": active_setups}
    if memory_notes:
        payload["retrieved_memory_notes"] = [
            {
                "source": note.get("source"),
                "source_id": note.get("source_id"),
                "snippet": note.get("snippet", note.get("content", "")),
            }
            for note in memory_notes
        ]
    state_json = json.dumps(payload, indent=2, default=str)
    return f"{SYSTEM_PROMPT_PREAMBLE}\n\nCURRENT STATE:\n{state_json}"
