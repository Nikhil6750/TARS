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


_NUM = r"(\d{1,3})(?:\s*(?:percent|%))?"


def _system_intent(low: str) -> LocalIntent | None:
    """Native laptop control phrases -> windows_system (no LLM, no UI automation)."""
    def sc(name, label, **args):
        return LocalIntent(name, [("system_control", {"action": label, **args})])

    mic = r"(?:the\s+|my\s+)?(?:microphone|mic)"
    if re.fullmatch(rf"(?:mute|turn off)\s+{mic}", low):
        return sc("mic_mute", "mute_microphone")
    if re.fullmatch(rf"(?:unmute|turn on)\s+{mic}", low):
        return sc("mic_unmute", "unmute_microphone")
    if re.fullmatch(r"(?:mute|silence)(?:\s+(?:the\s+)?(?:audio|sound|volume|speakers|laptop|computer|it))?", low):
        return sc("mute", "mute_audio")
    if re.fullmatch(r"unmute(?:\s+(?:the\s+)?(?:audio|sound|volume|speakers|laptop|computer|it))?", low):
        return sc("unmute", "unmute_audio")
    if m := re.fullmatch(rf"(?:set|put|change|make)\s+(?:the\s+)?volume\s+(?:to|at)\s+{_NUM}", low):
        return sc("set_volume", "set_volume", percent=int(m.group(1)))
    if m := re.fullmatch(rf"(?:set|put|change|make)\s+(?:the\s+)?brightness\s+(?:to|at)\s+{_NUM}", low):
        return sc("set_brightness", "set_brightness", percent=int(m.group(1)))
    for noun, prefix in (("volume", "volume"), ("brightness", "brightness")):
        if m := re.fullmatch(rf"(?:turn\s+(?:the\s+)?{noun}\s+(up|down)|(?:increase|raise|lower|decrease|reduce)\s+"
                             rf"(?:the\s+)?{noun}|{noun}\s+(up|down)|(?:increase|raise|lower|decrease|reduce)\s+"
                             rf"(?:the\s+)?{noun}\s+by\s+(\d{{1,3}})(?:\s*(?:percent|%))?)", low):
            word = low
            down = bool(re.search(r"\b(down|lower|decrease|reduce)\b", word))
            by = re.search(r"by\s+(\d{1,3})", word)
            args = {"step": int(by.group(1))} if by else {}
            return sc(f"{prefix}_{'down' if down else 'up'}", f"{prefix}_{'down' if down else 'up'}", **args)
        if m := re.fullmatch(rf"(?:increase|raise|lower|decrease|reduce)\s+(?:the\s+)?{noun}\s+by\s+{_NUM}", low):
            down = bool(re.match(r"(?:lower|decrease|reduce)", low))
            return sc(f"{prefix}_{'down' if down else 'up'}", f"{prefix}_{'down' if down else 'up'}",
                      step=int(m.group(1)))
    media = (
        (r"(?:pause|pause\s+(?:the\s+)?(?:music|song|media|video|playback))", "media_play_pause"),
        (r"(?:play|resume|play\s+(?:the\s+)?(?:music|song|media|video)|resume\s+(?:the\s+)?(?:music|playback))",
         "media_play_pause"),
        (r"(?:play\s*/?\s*pause|toggle\s+(?:play|playback))", "media_play_pause"),
        (r"(?:next|skip)(?:\s+(?:track|song))?|next\s+song|skip\s+(?:this\s+)?(?:track|song)", "media_next"),
        (r"(?:previous|last)\s+(?:track|song)|go\s+back\s+a\s+(?:track|song)|previous", "media_previous"),
        (r"stop(?:\s+(?:the\s+)?(?:music|song|media|playback))", "media_stop"),
    )
    for pattern, action in media:
        if re.fullmatch(pattern, low):
            return sc(action, action)
    if re.fullmatch(r"(?:what(?:'s| is)\s+(?:my\s+|the\s+)?battery(?:\s+(?:level|status|at))?|battery(?:\s+status)?|how\s+much\s+battery.*)", low):
        return sc("battery", "get_battery_status")
    if m := re.fullmatch(r"(minimi[sz]e|maximi[sz]e|restore)(?:\s+(?:this|the|current))?(?:\s+window)?", low):
        return sc("window", f"window_{m.group(1)[:3].replace('res', 'restore').replace('min', 'minimize').replace('max', 'maximize')}")
    if re.fullmatch(r"(?:switch|next)\s+window|alt\s*tab", low):
        return sc("window_switch", "window_switch")
    if m := re.fullmatch(r"(?:can you\s+)?close\s+(?:the\s+)?([a-z0-9 ]{2,30}?)(?:\s+(?:app|window|application))?", low):
        name = m.group(1).strip()
        if name not in {"this", "current", "it", "that", "window"}:
            return LocalIntent("close_app", [("desktop_close_app", {"target": name})])
    if re.fullmatch(r"(?:restart|reboot)(?:\s+(?:the|my))?(?:\s+(?:laptop|computer|pc|machine))?", low):
        return sc("restart", "restart")
    if re.fullmatch(r"(?:shut\s*down|power\s+off|turn\s+off)(?:\s+(?:the|my))?(?:\s+(?:laptop|computer|pc|machine))?", low):
        return sc("shutdown", "shutdown")
    if re.fullmatch(r"(?:put\s+(?:the|my)\s+(?:laptop|computer|pc)\s+to\s+sleep|sleep(?:\s+(?:the\s+)?(?:laptop|computer|pc))?|go\s+to\s+sleep)", low):
        return sc("sleep", "sleep")
    return None


class LocalIntentRouter:
    """route(text) -> LocalIntent | None. Never performs I/O."""

    def route(self, text: str) -> LocalIntent | None:
        """Whole phrase first; then 'A and B' / 'A then B' compounds where EVERY clause routes locally."""
        whole = self._route_one(text)
        if whole is not None:
            return whole
        value = re.sub(r"[.!?,]+$", "", text.strip()).strip()
        parts = [p.strip() for p in re.split(r"\s*(?:,\s*)?(?:\band then\b|\bthen\b|\band\b)\s+", value, flags=re.I) if p.strip()]
        if len(parts) < 2:
            return None
        intents = [self._route_one(p) for p in parts]
        if any(i is None or i.needs_cloud for i in intents):
            return None
        steps: list[tuple[str, dict]] = []
        for i, intent in enumerate(intents):
            # "open calculator" immediately followed by "calculate ..." -- calculate opens it itself.
            if (intent.name == "open_app" and i + 1 < len(intents) and intents[i + 1].name == "calculate"
                    and intent.steps[0][1]["target"] == "calculator"):
                continue
            steps.extend(intent.steps)
        return LocalIntent("compound", steps, success_text=None)

    def _route_one(self, text: str) -> LocalIntent | None:
        value = re.sub(r"[.!?,]+$", "", text.strip()).strip()
        if not value:
            return None
        low = value.lower()

        if system := _system_intent(low):
            return system
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
        if status == "NEEDS_CONFIRMATION":
            parts.append("That needs your confirmation. Say yes or no.")
            continue
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
