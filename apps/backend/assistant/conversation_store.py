"""Persistence for conversation memory — the SQLite layer of
ARCHITECTURE.md § Memory architecture's "Conversation memory" row. Chat/
voice turn history, context for the assistant, never evidence of trading
performance.
"""
from __future__ import annotations

from typing import Any

import aiosqlite

from app.contracts import validate_assistant_message
from app.schemas import AssistantMessage


class ConversationStore:
    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn

    async def save(self, message: AssistantMessage) -> None:
        validate_assistant_message(message.to_contract_dict())
        await self._conn.execute(
            """
            INSERT INTO conversation_messages (
                message_id, schema_version, conversation_id, timestamp, role,
                content, input_mode, audio_ref, related_event_id, intent,
                provider_stt, provider_assistant, provider_tts, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(message.message_id),
                message.schema_version,
                str(message.conversation_id),
                message.timestamp.isoformat(),
                message.role.value,
                message.content,
                message.input_mode.value,
                message.audio_ref,
                message.related_event_id,
                message.intent,
                message.providers.stt,
                message.providers.assistant,
                message.providers.tts,
                message.error,
            ),
        )
        await self._conn.commit()

    async def get_recent(self, conversation_id: str, limit: int = 10) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 100))
        cursor = await self._conn.execute(
            """
            SELECT * FROM conversation_messages
            WHERE conversation_id = ?
            ORDER BY timestamp DESC LIMIT ?
            """,
            (conversation_id, limit),
        )
        rows = list(await cursor.fetchall())
        # Oldest-first, matching turn order for provider history.
        return [_row_to_dict(row) for row in reversed(rows)]


def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "message_id": row["message_id"],
        "conversation_id": row["conversation_id"],
        "timestamp": row["timestamp"],
        "role": row["role"],
        "content": row["content"],
        "input_mode": row["input_mode"],
        "audio_ref": row["audio_ref"],
        "related_event_id": row["related_event_id"],
        "intent": row["intent"],
        "providers": {
            "stt": row["provider_stt"],
            "assistant": row["provider_assistant"],
            "tts": row["provider_tts"],
        },
        "error": row["error"],
    }
