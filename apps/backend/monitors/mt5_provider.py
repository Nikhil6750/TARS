"""Read-only MetaTrader 5 provider.

Only the official ``MetaTrader5`` package's read calls are used: initialize,
terminal_info, account_info, symbol_select, symbol_info_tick, symbol_info,
positions_get, orders_get, shutdown. There is deliberately no order placement,
check or modification call anywhere in this module (asserted by tests).

Every failure degrades to a truthful state (NOT INSTALLED / DISCONNECTED /
ERROR) and never raises into the caller.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import Enum

from events.core import EventSource, NormalizedEvent

logger = logging.getLogger("tars.monitors.mt5")


class MT5State(str, Enum):
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    NOT_INSTALLED = "NOT INSTALLED"
    ERROR = "ERROR"


def mask_account(login: int | str | None) -> str | None:
    if login is None:
        return None
    text = str(login)
    return "*" * max(0, len(text) - 3) + text[-3:]


def load_mt5():
    """Return the MetaTrader5 module or None. Isolated so tests can inject a fake."""
    try:
        import MetaTrader5 as mt5  # type: ignore
        return mt5
    except Exception:
        return None


class MT5Provider:
    def __init__(self, symbols: list[str], publish, on_state, *, module_loader: Callable = load_mt5,
                 poll_seconds: float = 1.0, move_pct: float = 0.15, move_window: float = 60.0,
                 spread_factor: float = 3.0, clock: Callable[[], float] = time.monotonic):
        self.symbols = [s.strip().upper() for s in symbols if s.strip()]
        self.publish = publish            # async (NormalizedEvent) -> bool
        self.on_state = on_state          # async (MT5State) -> None
        self.module_loader = module_loader
        self.poll_seconds = poll_seconds
        self.move_pct, self.move_window, self.spread_factor = move_pct, move_window, spread_factor
        self.clock = clock
        self.state = MT5State.DISCONNECTED
        self.detail = "not started"
        self.account: str | None = None
        self.quotes: dict[str, dict] = {}
        self.positions: list[dict] = []
        self.orders: list[dict] = []
        self.floating_pnl: float | None = None
        self.updated_at: str | None = None
        self._mt5 = None
        self._history: dict[str, deque] = {s: deque() for s in self.symbols}
        self._spread_base: dict[str, float] = {}
        self._tickets: set[int] | None = None
        self._task: asyncio.Task | None = None
        self._last_state: MT5State | None = None

    # ---- lifecycle -------------------------------------------------------
    async def start(self):
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._mt5:
            try:
                await asyncio.to_thread(self._mt5.shutdown)
            except Exception:
                pass

    async def _run(self):
        delay = self.poll_seconds
        while True:
            try:
                snapshot = await asyncio.to_thread(self.poll_once)
                await self._apply(snapshot)
                delay = self.poll_seconds if self.state is MT5State.CONNECTED else min(15.0, max(delay, 3.0) * 1.5)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # MT5 must never crash TARS
                logger.warning("mt5 poll failed: %s", type(exc).__name__)
                self.state, self.detail = MT5State.ERROR, type(exc).__name__
                delay = 10.0
            if self.state is not self._last_state:
                self._last_state = self.state
                try:
                    await self.on_state(self.state)
                except Exception:
                    logger.warning("mt5 state callback failed", exc_info=True)
            await asyncio.sleep(delay)

    # ---- synchronous read-only poll (worker thread) ----------------------
    def poll_once(self) -> dict:
        mt5 = self._mt5 or self.module_loader()
        if mt5 is None:
            return {"state": MT5State.NOT_INSTALLED, "detail": "MetaTrader5 package is not installed"}
        self._mt5 = mt5
        try:
            if not mt5.terminal_info():
                if not mt5.initialize():
                    return {"state": MT5State.DISCONNECTED,
                            "detail": f"MT5 terminal not reachable: {mt5.last_error()}"}
            terminal = mt5.terminal_info()
            if terminal is not None and not getattr(terminal, "connected", True):
                return {"state": MT5State.DISCONNECTED, "detail": "MT5 terminal is not connected to a broker"}
            info = mt5.account_info()
            quotes = {}
            for symbol in self.symbols:
                mt5.symbol_select(symbol, True)
                tick = mt5.symbol_info_tick(symbol)
                sinfo = mt5.symbol_info(symbol)
                if tick is None:
                    continue
                point = getattr(sinfo, "point", 0) or 0
                quotes[symbol] = {"bid": tick.bid, "ask": tick.ask,
                                  "spread": round((tick.ask - tick.bid) / point, 1) if point else None,
                                  "time": getattr(tick, "time", None)}
            positions = [{"ticket": p.ticket, "symbol": p.symbol, "type": "BUY" if p.type == 0 else "SELL",
                          "volume": p.volume, "price_open": p.price_open, "profit": p.profit}
                         for p in (mt5.positions_get() or [])]
            orders = [{"ticket": o.ticket, "symbol": o.symbol, "volume": o.volume_current,
                       "price": o.price_open, "type": o.type} for o in (mt5.orders_get() or [])]
            return {"state": MT5State.CONNECTED, "detail": "ok",
                    "account": mask_account(getattr(info, "login", None)),
                    "quotes": quotes, "positions": positions, "orders": orders,
                    "pnl": round(sum(p["profit"] for p in positions), 2)}
        except Exception as exc:
            return {"state": MT5State.ERROR, "detail": type(exc).__name__}

    # ---- state + event derivation ---------------------------------------
    async def _apply(self, snap: dict):
        self.state, self.detail = snap["state"], snap["detail"]
        if self.state is not MT5State.CONNECTED:
            self.quotes, self.positions, self.orders, self.floating_pnl = {}, [], [], None
            return
        self.account = snap["account"]
        self.quotes, self.positions, self.orders = snap["quotes"], snap["positions"], snap["orders"]
        self.floating_pnl = snap["pnl"]
        self.updated_at = datetime.now(UTC).isoformat()
        for event in self.derive_events():
            await self.publish(event)

    def derive_events(self) -> list[NormalizedEvent]:
        """Only meaningful changes leave this method; raw ticks never do."""
        now, events = self.clock(), []
        for symbol, quote in self.quotes.items():
            mid = (quote["bid"] + quote["ask"]) / 2
            hist = self._history.setdefault(symbol, deque())
            hist.append((now, mid))
            while hist and now - hist[0][0] > self.move_window:
                hist.popleft()
            base = hist[0][1]
            move = (mid - base) / base * 100 if base else 0
            if abs(move) >= self.move_pct and now - hist[0][0] >= 5:
                events.append(self._event(
                    symbol, "price.move", 3,
                    f"{symbol} {'up' if move > 0 else 'down'} {abs(move):.2f}% in {int(now - hist[0][0])}s",
                    f"{symbol} moved from {base:.5f} to {mid:.5f}.",
                    f"{symbol}:move:{int(now // 120)}", {"move_pct": round(move, 3), "from": base, "to": mid}))
            spread = quote.get("spread")
            if spread:
                ref = self._spread_base.get(symbol)
                self._spread_base[symbol] = spread if ref is None else ref * 0.98 + spread * 0.02
                if ref and spread >= ref * self.spread_factor and spread - ref >= 5:
                    events.append(self._event(
                        symbol, "spread.spike", 2, f"{symbol} spread widened to {spread}",
                        f"Spread {spread} points vs normal about {ref:.1f}.",
                        f"{symbol}:spread:{int(now // 120)}", {"spread": spread, "normal": round(ref, 1)}))
        tickets = {p["ticket"] for p in self.positions}
        if self._tickets is not None:
            for opened in tickets - self._tickets:
                pos = next(p for p in self.positions if p["ticket"] == opened)
                events.append(self._event(
                    pos["symbol"], "position.opened", 2, f"Position opened: {pos['type']} {pos['symbol']}",
                    f"{pos['volume']} lots at {pos['price_open']}.", f"pos:{opened}:open", {"ticket": opened}))
            for closed in self._tickets - tickets:
                events.append(self._event(None, "position.closed", 2, "Position closed",
                                          f"Ticket {closed} is no longer open.", f"pos:{closed}:closed",
                                          {"ticket": closed}))
        self._tickets = tickets
        return events

    def _event(self, symbol, kind, severity, title, summary, dedupe, payload) -> NormalizedEvent:
        return NormalizedEvent(source=EventSource.MT5, kind=kind, severity=severity, symbol=symbol,
                               title=title, summary=summary, dedupe_key=dedupe, payload=payload,
                               expires_at=datetime.now(UTC) + timedelta(minutes=5))

    def snapshot(self) -> dict:
        return {"state": self.state.value, "detail": self.detail, "account": self.account,
                "symbols": self.symbols, "quotes": self.quotes, "positions": self.positions,
                "orders": self.orders, "floating_pnl": self.floating_pnl,
                "updated_at": self.updated_at, "read_only": True}
