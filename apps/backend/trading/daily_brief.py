"""DailyMarketBriefService -- once-per-local-day automatic market brief
(mission: TARS Watcher Intelligence, sections 14-16).

Reuses AppStateStore (storage/app_state.py) for `last_daily_brief_date`
persistence, MonitorManager's already-maintained state (no new polling),
and the SAME single-synthesis-call pattern MarketExplainerOrchestrator uses
(trading/market_explainer.py) -- one AssistantProvider.respond() call after
TARS has already assembled/filtered the evidence, never a call per source.

Uses the machine's LOCAL calendar date (`datetime.now()`, no tzinfo) per the
mission's explicit instruction -- never UTC, never a hardcoded timezone.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime

from assistant.provider import AssistantRequest

logger = logging.getLogger("tars.trading.daily_brief")

_LAST_BRIEF_DATE_KEY = "last_daily_brief_date"

_SYSTEM_PROMPT = """You are TARS, writing today's automatic Daily Market Brief from evidence TARS itself already
gathered below -- you are not looking at anything yourself, only the structured facts already assembled.

Target length: about 30-60 seconds read aloud (roughly 80-150 words). Cover only what is genuinely significant:
overnight/major developments, important calendar items (passed or upcoming), notable moves in the watched
assets, current MT5 exposure if connected, and the nearest upcoming event risk. Do not dump every headline or
event -- pick what actually matters. If a source is unavailable or there is nothing significant, say so plainly
rather than padding the brief or inventing content. Never invent a price, event, headline or position not in the
evidence below.

Reply as natural flowing sentences for a voice assistant to read aloud -- no markdown, headers, bullets or
asterisks, and no greeting/sign-off filler; lead with the most useful fact."""


@dataclass
class DailyBriefResult:
    status: str  # "GENERATED" | "SKIPPED_ALREADY_SENT"
    date: str
    text: str = ""
    provider: str = ""
    degraded_sources: tuple[str, ...] = field(default_factory=tuple)


class DailyMarketBriefService:
    def __init__(
        self,
        app_state_store,
        watchlist_store,
        monitors,
        synthesis_provider,
        *,
        readiness_timeout_seconds: float = 20.0,
        readiness_poll_seconds: float = 1.0,
        clock=datetime.now,  # local time, deliberately no tz -- see module docstring
    ) -> None:
        self._app_state = app_state_store
        self._watchlist = watchlist_store
        self._monitors = monitors
        self._provider = synthesis_provider
        self._readiness_timeout = readiness_timeout_seconds
        self._readiness_poll = readiness_poll_seconds
        self._clock = clock

    def _today(self) -> str:
        return self._clock().date().isoformat()

    async def maybe_generate_on_startup(self) -> DailyBriefResult | None:
        """Called once per backend startup/session (mission section 14).
        Returns None (does nothing) if today's brief was already sent --
        a same-day restart must never auto-replay it."""
        last = await self._app_state.get(_LAST_BRIEF_DATE_KEY)
        today = self._today()
        if last == today:
            return None
        await self._wait_for_providers()
        result = await self._generate(today)
        await self._app_state.set(_LAST_BRIEF_DATE_KEY, today)
        return result

    async def generate(self, *, force: bool = True) -> DailyBriefResult:
        """Explicit "give me today's brief again" -- always regenerates
        regardless of whether one was already auto-sent today (mission:
        "must always regenerate/replay regardless of dedupe")."""
        today = self._today()
        if not force:
            last = await self._app_state.get(_LAST_BRIEF_DATE_KEY)
            if last == today:
                return DailyBriefResult(status="SKIPPED_ALREADY_SENT", date=today)
        result = await self._generate(today)
        await self._app_state.set(_LAST_BRIEF_DATE_KEY, today)
        return result

    async def _wait_for_providers(self) -> None:
        """Bounded wait (mission: "wait a bounded amount of time for
        providers to become READY") -- never blocks startup indefinitely;
        continues with truthful degradation if still not ready."""
        deadline = asyncio.get_event_loop().time() + self._readiness_timeout
        while asyncio.get_event_loop().time() < deadline:
            status = await self._status()
            if status is not None and status.get("mt5", {}).get("state") not in (None, "DISCONNECTED"):
                return
            if status is not None and status.get("calendar", {}).get("state") == "CONNECTED":
                return
            await asyncio.sleep(self._readiness_poll)

    async def _status(self) -> dict | None:
        try:
            return await self._monitors.status()
        except Exception:
            return None

    async def _generate(self, today: str) -> DailyBriefResult:
        status = await self._status()
        watched = await self._watchlist.load()
        degraded: list[str] = []
        evidence_lines: list[str] = []

        if status is None:
            degraded.append("monitors")
            evidence_lines.append("Monitoring is not available right now.")
        else:
            mt5 = status.get("mt5") or {}
            if mt5.get("state") == "CONNECTED":
                positions = mt5.get("positions") or []
                if positions:
                    evidence_lines.append(
                        "Open MT5 positions: " + ", ".join(f"{p.get('type')} {p.get('symbol')}" for p in positions)
                    )
                else:
                    evidence_lines.append("No open MT5 positions.")
            else:
                degraded.append("mt5")
                evidence_lines.append(f"MT5 is unavailable ({mt5.get('state', 'UNKNOWN')}); no position/quote data.")

            calendar = status.get("calendar") or {}
            next_event = calendar.get("next")
            if next_event:
                evidence_lines.append(
                    f"Next calendar event: {next_event.get('currency')} {next_event.get('event')} "
                    f"at {next_event.get('at')} ({next_event.get('importance')} importance)."
                )
            elif calendar.get("state") != "CONNECTED":
                degraded.append("calendar")
                evidence_lines.append("Economic calendar is unavailable right now.")
            else:
                evidence_lines.append("No high-impact calendar events in range.")

        news_monitor = getattr(self._monitors, "news", None)
        if news_monitor is not None and watched:
            headlines: list[str] = []
            for entry in watched:
                for item in news_monitor.latest(symbols=[entry.canonical_symbol], limit=2):
                    headlines.append(f"{entry.canonical_symbol}: {item['headline']}")
            if headlines:
                evidence_lines.append("Recent relevant headlines: " + "; ".join(headlines[:6]))
            else:
                evidence_lines.append("No notable recent headlines for watched assets.")
        elif news_monitor is None:
            degraded.append("news")

        if watched:
            evidence_lines.append("Currently watched: " + ", ".join(e.canonical_symbol for e in watched) + ".")
        else:
            evidence_lines.append("No assets are currently on the watchlist.")

        evidence_text = "\n".join(evidence_lines)
        request = AssistantRequest(
            text=f"Give today's ({today}) Daily Market Brief.",
            conversation_id=f"daily-brief:{today}",
            system_context=_SYSTEM_PROMPT + "\n\n[Evidence below]\n" + evidence_text,
        )
        try:
            reply = await self._provider.respond(request)
        except Exception:
            logger.exception("daily brief synthesis failed")
            return DailyBriefResult(
                status="GENERATED", date=today,
                text="I couldn't put together today's market brief right now -- the summary step failed.",
                degraded_sources=tuple(degraded),
            )
        return DailyBriefResult(
            status="GENERATED", date=today, text=reply.text, provider=reply.provider,
            degraded_sources=tuple(degraded),
        )
