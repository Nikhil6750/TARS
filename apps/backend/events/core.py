"""Normalized monitor events, separate from the frozen trading-facts contract.

No connector or strategy implementation. Local monitors publish here; only
significant/relevant events may enter the existing bounded AgentRuntime.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from enum import Enum
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from agents.base import Agent
from agents.models import AgentConfig, AgentMode, AgentRunResult, AgentRunStatus

logger = logging.getLogger("tars.events.core")


class EventSource(str, Enum):
    MT5 = "MT5"
    TRADINGVIEW = "TRADINGVIEW"
    NEWS = "NEWS"
    ECONOMIC_CALENDAR = "ECONOMIC_CALENDAR"
    STRATEGY = "STRATEGY"
    SYSTEM = "SYSTEM"


class ProviderStatus(str, Enum):
    CONNECTED = "CONNECTED"
    DEGRADED = "DEGRADED"
    DISCONNECTED = "DISCONNECTED"
    ERROR = "ERROR"


class NormalizedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=lambda: str(uuid4()), max_length=200)
    timestamp: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    source: EventSource
    kind: str = Field(min_length=1, max_length=100)
    severity: int = Field(ge=0, le=4, description="0 debug, 1 info, 2 warning, 3 high, 4 critical")
    symbol: str | None = Field(default=None, max_length=30)
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(default="", max_length=2000)
    payload: dict = Field(default_factory=dict)
    dedupe_key: str = Field(min_length=1, max_length=250)
    expires_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC) + timedelta(minutes=5))


class EventDecision(str, Enum):
    IGNORE = "IGNORE"
    NOTIFY = "NOTIFY"
    ANALYZE = "ANALYZE"
    SPEAK = "SPEAK"


class SignificanceGate:
    def __init__(self, *, symbols=(), analyze=False, speak=False):
        self.symbols = {s.upper() for s in symbols}
        self.analyze, self.speak = analyze, speak

    def decide(self, event: NormalizedEvent) -> EventDecision:
        if event.severity < 2 or (self.symbols and event.symbol and event.symbol.upper() not in self.symbols):
            return EventDecision.IGNORE
        if event.severity == 4 and self.speak:
            return EventDecision.SPEAK
        if event.severity >= 3 and self.analyze:
            return EventDecision.ANALYZE
        return EventDecision.NOTIFY


class EventAnalysisAgent(Agent):
    name = "significant_event_analysis"

    def __init__(self, orchestrator, event, recent):
        super().__init__(AgentConfig(mode=AgentMode.ON_DEMAND, timeout_seconds=45))
        self.orchestrator, self.event, self.recent = orchestrator, event, recent

    async def run_once(self):
        context = json.dumps({"event": self.event.model_dump(mode="json"),
                              "recent": self.recent}, ensure_ascii=True)
        answer = await self.orchestrator.analyze_monitor_event(context, str(uuid4()))
        if not answer:
            return AgentRunResult(status=AgentRunStatus.FAILED, summary="No analysis available")
        return AgentRunResult(status=AgentRunStatus.SUCCEEDED, summary=answer)


class RealtimeEventCore:
    def __init__(self, conn, runtime, orchestrator, *, gate=None, cooldown=30, clock=None):
        self.conn, self.runtime, self.orchestrator = conn, runtime, orchestrator
        self.gate = gate or SignificanceGate()
        self.cooldown = cooldown
        self.clock = clock or (lambda: datetime.now(UTC))
        self.recent: deque[dict] = deque(maxlen=200)
        self.providers = {source.value: ProviderStatus.DISCONNECTED.value for source in EventSource}
        self.subscribers: list[Callable[[dict], Awaitable[None]]] = []
        self._lock = asyncio.Lock()
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue(maxsize=128)
        self._counter = 0
        self._worker: asyncio.Task | None = None

    async def start(self):
        await self.conn.execute("""CREATE TABLE IF NOT EXISTS realtime_events (
            id TEXT PRIMARY KEY, dedupe_key TEXT NOT NULL, accepted_at TEXT NOT NULL,
            severity INTEGER NOT NULL, event TEXT NOT NULL, decision TEXT NOT NULL)""")
        await self.conn.execute("CREATE INDEX IF NOT EXISTS realtime_event_key ON realtime_events(dedupe_key, accepted_at)")
        await self.conn.commit()
        cursor = await self.conn.execute("SELECT event, decision FROM realtime_events ORDER BY accepted_at DESC LIMIT 200")
        for row in reversed(await cursor.fetchall()):
            self.recent.append({"event": json.loads(row[0]), "decision": row[1]})
        self._worker = asyncio.create_task(self._dispatch())

    def subscribe(self, callback):
        self.subscribers.append(callback)
        return lambda: self.subscribers.remove(callback) if callback in self.subscribers else None

    async def publish(self, event: NormalizedEvent) -> bool:
        now = self.clock()
        if event.expires_at <= now or event.timestamp > now + timedelta(seconds=30):
            return False
        if len(event.model_dump_json()) > 16000:
            raise ValueError("event payload exceeds 16KB")
        async with self._lock:
            cursor = await self.conn.execute("SELECT 1 FROM realtime_events WHERE id=?", (event.id,))
            if await cursor.fetchone():
                return False
            cursor = await self.conn.execute(
                "SELECT accepted_at, severity FROM realtime_events WHERE dedupe_key=? ORDER BY accepted_at DESC LIMIT 1",
                (f"{event.source.value}:{event.dedupe_key}",))
            prior = await cursor.fetchone()
            if prior and (now - datetime.fromisoformat(prior[0])).total_seconds() < self.cooldown and event.severity <= prior[1]:
                return False
            decision = self.gate.decide(event)
            if decision is not EventDecision.IGNORE and self._queue.full():
                self.providers[event.source.value] = ProviderStatus.DEGRADED.value
                return False
            await self.conn.execute("INSERT INTO realtime_events VALUES (?, ?, ?, ?, ?, ?)",
                (event.id, f"{event.source.value}:{event.dedupe_key}", now.isoformat(),
                 event.severity, event.model_dump_json(), decision.value))
            # Bounded durable history; admission uses receiver time, not source clocks.
            await self.conn.execute("DELETE FROM realtime_events WHERE id NOT IN (SELECT id FROM realtime_events ORDER BY accepted_at DESC LIMIT 2000)")
            await self.conn.commit()
            self.recent.append({"event": event.model_dump(mode="json"), "decision": decision.value})
            if decision is not EventDecision.IGNORE:
                self._counter += 1
                self._queue.put_nowait((-event.severity, self._counter, event, decision))
            return True

    async def set_provider_status(self, source: EventSource, status: ProviderStatus):
        self.providers[source.value] = status.value
        await self._broadcast({"type": "provider_status", "source": source.value, "status": status.value})

    async def _broadcast(self, payload):
        async def deliver(callback):
            try:
                await asyncio.wait_for(callback(payload), 2)
            except Exception:
                logger.warning("event subscriber failed", exc_info=True)
        await asyncio.gather(*(deliver(callback) for callback in tuple(self.subscribers)))

    async def _dispatch(self):
        while True:
            _, _, event, decision = await self._queue.get()
            try:
                if event.expires_at <= self.clock():
                    continue
                payload = {"type": "proactive_event", "event": event.model_dump(mode="json"),
                           "decision": decision.value, "analysis": None}
                # Notify before optional reasoning; provider outage cannot suppress alerts.
                await self._broadcast(payload)
                if decision is EventDecision.ANALYZE:
                    result = await self.runtime.run_on_demand(EventAnalysisAgent(
                        self.orchestrator, event, list(self.recent)[-5:]), trigger="significant_event")
                    if result.status is AgentRunStatus.SUCCEEDED:
                        await self._broadcast({**payload, "type": "event_analysis", "analysis": result.summary})
            except Exception:
                logger.exception("event dispatch failed; monitor remains alive")
            finally:
                self._queue.task_done()

    async def close(self):
        if self._worker:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
