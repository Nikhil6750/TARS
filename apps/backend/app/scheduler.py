"""APScheduler wiring for periodic, non-live-event housekeeping only, per
ARCHITECTURE.md § Scheduling / ADR-016 — never the mechanism for detecting
live trade setups (that stays event-driven, via events/generator.py +
app/event_bus.py). The concrete job wired here is vault reindexing;
morning/EOD summary and journal-housekeeping jobs are product features not
yet specified and are intentionally not stubbed out with placeholder
content — see the handoff for follow-up.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from memory.service import MemoryService

logger = logging.getLogger("tars.scheduler")

VAULT_REINDEX_INTERVAL_MINUTES = 30


def build_scheduler(memory_service: MemoryService, timezone: str) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=timezone)

    async def _reindex_job() -> None:
        try:
            result = await memory_service.reindex_vault()
            if not result.vault_missing:
                logger.info(
                    "vault reindex: %d indexed, %d unchanged, %d removed",
                    result.indexed,
                    result.unchanged,
                    result.removed,
                )
        except Exception:
            logger.exception("scheduled vault reindex failed")

    scheduler.add_job(
        _reindex_job,
        "interval",
        minutes=VAULT_REINDEX_INTERVAL_MINUTES,
        id="vault_reindex",
    )
    return scheduler
