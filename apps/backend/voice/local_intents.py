"""Deterministic local intent router for the offline-capable voice path.

A transcript that matches here is executed by the same `TarsTools` functions the
Gemini voice session calls (so safety/permission/confirmation behaviour is
identical) and never reaches any LLM. Anything else returns None and the caller
decides between online reasoning and a truthful "needs internet" reply.

This is deliberately a small phrase grammar, not NLU.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_ALIASES = {"gold": "XAUUSD", "bitcoin": "BTCUSD", "silver": "XAGUSD", "ethereum": "ETHUSD",
            "euro": "EURUSD", "pound": "GBPUSD", "cable": "GBPUSD"}
_TICKER = re.compile(r"\b([A-Za-z]{6})\b")
_KNOWN = {"XAUUSD", "EURUSD", "GBPUSD", "BTCUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD",
          "XAGUSD", "ETHUSD"}
_TF = re.compile(
    r"\b(\d+)\s*[- ]?\s*(minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|w)\b(?:\s+(?:time\s*frame|chart))?",
    re.I)
_WORDS = {"one": "1", "five": "5", "fifteen": "15", "thirty": "30"}


def _asset(text: str) -> str | None:
    for token in _TICKER.findall(text):
        if token.upper() in _KNOWN:
            return token.upper()
    lowered = text.lower()
    for word, symbol in _ALIASES.items():
        if re.search(rf"\b{word}\b", lowered):
            return symbol
    return None


def _timeframe(text: str) -> str | None:
    t = text.lower()
    for word, digit in _WORDS.items():
        t = re.sub(rf"\b{word}\b(?=\s*[- ]?(?:minutes?|mins?|hours?))", digit, t)
    t = re.sub(r"\ban\s+hour\b|\bone\s+hour\b|\bhourly\b", "1 hour", t)
    match = _TF.search(t)
    if not match:
        return None
    n, unit = match.group(1), match.group(2).lower()
    suffix = "m" if unit.startswith("m") else "h" if unit.startswith("h") else "d" if unit.startswith("d") else "w"
    return f"{n}{suffix}"


@dataclass
class LocalIntent:
    name: str
    steps: list[tuple[str, dict]]
    needs_cloud: bool = False
    detail: dict = field(default_factory=dict)
    success_text: str | None = None


_ARITH = re.compile(
    r"^(?:calculate|compute|what(?:'s| is)|work out)\s+(?P<expr>[\d.,\s]+(?:plus|minus|times|multiplied by|"
    r"divided by|over|x|\+|\-|\*|/)[\w\s.,+\-*/x]*)$", re.I)
_OPS = (("multiplied by", "*"), ("divided by", "/"), ("times", "*"), ("plus", "+"), ("minus", "-"),
        ("over", "/"))


def _expression(raw: str) -> str | None:
    expr = raw.lower().replace(",", "")
    for word, op in _OPS:
        expr = expr.replace(word, f" {op} ")
    expr = re.sub(r"(?<=\d)\s*x\s*(?=\d)", " * ", expr)
    expr = re.sub(r"\s+", " ", expr).strip()
    return expr if re.fullmatch(r"[\d.+\-*/ ]+", expr) and re.search(r"\d\s*[+\-*/]\s*\d", expr) else None


class LocalIntentRouter:
    """route(text) -> LocalIntent | None. Never performs I/O."""

    def route(self, text: str) -> LocalIntent | None:
        value = re.sub(r"[.!?,]+$", "", text.strip()).strip()
        if not value:
            return None
        low = value.lower()

        if match := _ARITH.match(value):
            expr = _expression(match.group("expr"))
            if expr:
                return LocalIntent("calculate", [("calculator_calculate", {"expression": expr})])

        if re.fullmatch(r"(?:pause|stop)\s+(?:the\s+)?(?:market\s+)?(?:monitoring|watching)", low):
            return LocalIntent("pause_monitoring", [("pause_market_watching", {})])
        if re.fullmatch(r"(?:resume|restart|continue)\s+(?:the\s+)?(?:market\s+)?(?:monitoring|watching)", low):
            return LocalIntent("resume_monitoring", [("resume_market_watching", {})])
        if re.fullmatch(r"what(?:'s| is)\s+(?:on|visible on)\s+my\s+screen|what\s+am\s+i\s+looking\s+at", low):
            return LocalIntent("screen_context", [("desktop_context", {})])
        if match := re.fullmatch(r"(?:watch|monitor)\s+(?:the\s+)?(.+)", low):
            asset = _asset(match.group(1))
            if asset:
                return LocalIntent("watch", [("watch_market", {"asset": asset})])

        steps: list[tuple[str, dict]] = []
        # "open start [and ...]" then optional scroll
        if re.match(r"^(?:open|show)\s+(?:the\s+)?start(?:\s+menu)?\b", low):
            steps.append(("desktop_open_start", {}))
            rest = re.sub(r"^(?:open|show)\s+(?:the\s+)?start(?:\s+menu)?", "", low).strip()
            rest = re.sub(r"^(?:and|then)\s+", "", rest)
            if not rest:
                return LocalIntent("open_start", steps)
            if scroll := re.fullmatch(r"scroll\s+(up|down)(?:\s+(?:a\s+bit|a\s+little))?", rest):
                steps.append(("desktop_scroll", {"direction": scroll.group(1)}))
                return LocalIntent("open_start_scroll", steps)
            return None
        if scroll := re.fullmatch(r"scroll\s+(up|down)(?:\s+(?:a\s+bit|a\s+little|more))?", low):
            return LocalIntent("scroll", [("desktop_scroll", {"direction": scroll.group(1)})])

        # TradingView navigation (symbol/timeframe only -- analysis requires online reasoning)
        analysis = re.search(
            r"\b(?:analy[sz]e|observe|report|tell\s+me|what\s+do\s+you|explain|why|what(?:'s|\s+is)\s+happening)\b", low)
        tv = re.search(r"\btrading\s*view\b", low)
        asset = _asset(value)
        tf = _timeframe(value)
        if analysis and (asset or tv):
            return LocalIntent("market_analysis", [], needs_cloud=True,
                               detail={"asset": asset, "timeframe": tf})
        if tv or (asset and re.match(r"^(?:show|display|switch\s+to|go\s+to|set|put)\b", low)):
            steps.append(("desktop_open_app", {"target": "TradingView"}))  # opens or focuses
            if asset:
                steps.append(("tradingview_set_symbol", {"symbol": asset}))
            if tf:
                steps.append(("tradingview_set_timeframe", {"timeframe": tf}))
            if tv and not asset and not tf and not re.fullmatch(r"(?:open|launch|start)\s+trading\s*view", low):
                return None
            shown = " ".join(x for x in (asset, f"on {tf}" if tf else None) if x)
            return LocalIntent("tradingview_navigate", steps,
                               success_text=f"TradingView is showing {shown}." if shown else "TradingView is open.")
        if match := re.fullmatch(r"(?:open|launch|start)\s+(?!start\b)([a-z0-9 ]{2,40})", low):
            if " and " not in match.group(1):
                return LocalIntent("open_app", [("desktop_open_app", {"target": match.group(1).strip()})])
        return None


def summarize(results: list[tuple[str, dict]]) -> str:
    """Truthful one-line summary built only from the tool results."""
    parts = []
    for name, out in results:
        status = out.get("status")
        text = out.get("summary") or out.get("error") or ""
        if name == "calculator_calculate" and out.get("data"):
            text = out.get("summary") or text
        if name == "desktop_context":
            win = (out.get("primary_visible_window") or {})
            text = win.get("summary") or "I could not read the screen."
        if name in {"pause_market_watching", "resume_market_watching"}:
            text = "Market monitoring paused." if status == "PAUSED" else (
                "Market monitoring resumed." if status == "RESUMED" else text or "Market monitoring is unavailable.")
        if name == "watch_market" and status == "WATCHING":
            text = f"Watching {out.get('symbol')}."
        if status and status not in {"DONE", "PAUSED", "RESUMED", "WATCHING"} and name not in {"desktop_context"}:
            text = text or f"That did not complete ({status})."
        parts.append(text.strip())
    return " ".join(p for p in parts if p) or "Done."
