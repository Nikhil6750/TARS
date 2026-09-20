"""Gemini Live as the primary realtime voice provider (GEMINI_LIVE).

One persistent Live API session per voice conversation, opened lazily on the first real
speech and closed after an idle interval (free-tier safety). Gemini owns turn detection,
transcription and barge-in; TARS only bridges audio, exposes bounded READ-ONLY tools and
delegates deep reasoning to the existing Claude backend (`ask_claude`).

The class is duck-compatible with VoiceSessionController (the local streaming provider), so
the WebSocket router treats both the same. The local stack is untouched and is the fallback.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from app.schemas import InputMode
from voice.desktop_tools import DESKTOP_TOOL_NAMES, DesktopTools
from voice.session import LatencyMetrics, VoiceState

logger = logging.getLogger("tars.gemini_live")

DEFAULT_VOICE = "Sadaltager"
# Small, explicit misrecognition list -> symbol. Display/conversation context only: it never feeds
# trade execution (there is none), and it only fires on sentences that look like market questions.
_TICKER_FIXES = [
    (re.compile(r"\b(?:the\s+)?(?:rusty|eresty|oresti|orasty|eurasty|eurusty|ers\s*d|u\s*r\s*u\s*s\s*d|e\s*u\s*r\s*u\s*s\s*d|euro\s*u\.?\s*s\.?\s*d\.?|you\s+are\s+(?:you\s+)?(?:us|usd))\b", re.I), "EURUSD"),
    (re.compile(r"\b(?:x\s*a\s*u\s*u\s*s\s*d|gold\s+dollar)\b", re.I), "XAUUSD"),
    (re.compile(r"\b(?:pound\s+dollar|sterling\s+dollar)\b", re.I), "GBPUSD"),
    (re.compile(r"\bdollar\s+yen\b", re.I), "USDJPY"),
]
_MARKET_CUE = re.compile(r"\b(what|how|why|is|are|doing|happening|check|chart|price|look|show|switch|open|analy[sz]e|"
                         r"about|going|move|moving|trend|quote|spread)\b", re.I)


def resolve_trading_terms(text: str) -> str:
    """Replace a garbled ticker with the plausible symbol when the sentence is a market question."""
    if not _MARKET_CUE.search(text):
        return text
    for pattern, symbol in _TICKER_FIXES:
        text = pattern.sub(symbol, text)
    return text


INPUT_RATE = 16000
OUTPUT_RATE = 24000

SYSTEM_PROMPT = """You are TARS: a sharp, calm real-time trading assistant living on the user's desktop. You talk
like a trusted desk colleague, not a chatbot.

Style
- Answer the question immediately, in 1-4 short spoken sentences. Lead with the useful fact, then one line of context.
- Confident but honest: say "I can't see a live quote right now" instead of guessing. Never invent prices, spreads,
  positions, news or event results.
- Conversational and responsive. Do not re-introduce yourself. Never say "How can I assist you today?" or similar
  filler; after a greeting just answer naturally ("Yep, I hear you.").
- Ask one short clarifying question only when you truly cannot proceed.
- Point out relevant context unprompted when it matters (an imminent high-impact event, a disconnected data source,
  a replay label) but keep it to a clause.
- If the user interrupts or changes topic ("no, check gold"), drop what you were saying and answer the new request.
- For follow-ups like "why?" give a concise reason. For "go deeper", "detailed", "analysis", "outlook", "risks",
  "strategy" or anything long, say a brief lead-in ("Let me dig into that") and call ask_claude, then explain the
  result in plain natural speech. Never read markdown, bullets, asterisks or lists aloud: turn long analysis into
  two to four flowing sentences with the key takeaway first.

Trading vocabulary (spoken -> symbol; use for understanding only, never for placing trades)
EURUSD "euro dollar", "euro U.S. dollar"; GBPUSD "pound dollar", "sterling dollar", "cable"; USDJPY "dollar yen";
EURJPY "euro yen"; GBPJPY "pound yen"; XAUUSD "gold", "gold dollar"; XAGUSD "silver"; BTCUSD "bitcoin dollar";
ETHUSD "ether", "ethereum dollar"; SPX "S&P 500"; NDX/NASDAQ "Nasdaq"; DXY "U.S. Dollar Index".
Speech recognition can garble tickers ("the rusty", "you are you S D"). When the sentence is clearly a market question,
resolve the odd word to the most plausible symbol from this list and use the resolved symbol in tool calls. If two
symbols are equally plausible, ask which one.

Facts and tools
- For any market fact call a tool first: get_market_context, get_mt5_state, get_tradingview_state, get_recent_events,
  get_economic_calendar. If a source is disconnected, say so plainly. Data labelled DEMO REPLAY is a rehearsal, not
  live: always say it is a replay.
- "Look at the chart / what changed / what am I looking at" -> analyze_chart. "What is on my screen / which app is
  open" -> desktop_context. Never claim you looked at something you did not.

Desktop control (all through TARS's guarded action layer)
- You can open/switch apps (desktop_open_app, desktop_focus_window), inspect and list controls, scroll, open URLs,
  search the web, list/open files and run bounded terminal commands. Tell the user in a few words what you did and
  report the tool's real result: DONE, NOT_FOUND, NEEDS_CONFIRMATION, BLOCKED or FAILED. Never pretend it worked.
- If a click, typing or other state-changing action returns NEEDS_CONFIRMATION, ask the user plainly ("Click Save in
  Notepad, yes?"). Only after they clearly say yes call confirm_pending_action; if they say no call
  cancel_pending_action. Never confirm on your own.
- If a control cannot be found or you are unsure which one to click, say so and ask; do not guess.

Hard limits
- Live trading is read-only. You cannot and must never place, modify, close or cancel orders or positions, and must
  not click order buttons in MetaTrader. If asked, say trading stays in the user's hands.
- Only respond when the user is talking to you (usually "TARS") or a conversation is already active. Ignore
  background chatter and TV."""


def _tool_declarations():
    from google.genai import types

    def obj(**props):
        return types.Schema(type="OBJECT", properties={k: types.Schema(type=v[0], description=v[1])
                                                       for k, v in props.items()})

    decl = types.FunctionDeclaration
    return [types.Tool(function_declarations=[
        decl(name="get_market_context", description="Live quote (bid/ask/spread and source), next calendar event and chart-monitor state for a symbol. Use for any 'what is happening with X' question.",
             parameters=obj(symbol=("STRING", "Symbol such as EURUSD or XAUUSD"))),
        decl(name="get_recent_events", description="Most recent proactive market events/alerts TARS raised (price moves, calendar, position changes).",
             parameters=obj(limit=("INTEGER", "How many, default 5"))),
        decl(name="get_mt5_state", description="MetaTrader 5 connection state, masked account, quotes, open positions and floating P&L. Read-only."),
        decl(name="get_tradingview_state", description="Whether the TradingView chart window is being monitored and the latest chart state."),
        decl(name="get_economic_calendar", description="Upcoming economic events with currency, importance, previous, forecast and actual.",
             parameters=obj(hours_ahead=("INTEGER", "Look-ahead window in hours, default 24"))),
        decl(name="ask_claude", description="Delegate deep trading/chart analysis, strategy research or complex synthesis to Claude. Takes a few seconds.",
             parameters=obj(question=("STRING", "The full question to analyse"),
                            context=("STRING", "Optional extra context from the conversation"))),
        decl(name="desktop_context", description="What is on the desktop right now: active application/window and the recent desktop actions TARS took. Use for 'what am I looking at', 'which app is open'."),
        decl(name="desktop_open_app", description="Launch a Windows application by executable name (e.g. notepad, chrome, terminal64 for MetaTrader).",
             parameters=obj(target=("STRING", "Executable name or full .exe path"))),
        decl(name="desktop_focus_window", description="Bring an already running application/window to the front (switch to it).",
             parameters=obj(target=("STRING", "Application or window title, e.g. tradingview, chrome, metatrader"))),
        decl(name="desktop_list_controls", description="List clickable/typeable UI controls of the active or a named window (Windows UI Automation). Use before clicking.",
             parameters=obj(target=("STRING", "Optional window to inspect"))),
        decl(name="desktop_click_control", description="Click a control found via desktop_list_controls. Needs the user's confirmation. Never usable for MT5 order controls.",
             parameters=obj(control_id=("STRING", "control_id from desktop_list_controls"), label=("STRING", "Human-readable name of what is clicked"))),
        decl(name="desktop_type_text", description="Type text into a control found via desktop_list_controls. Needs the user's confirmation.",
             parameters=obj(control_id=("STRING", "control_id"), text=("STRING", "Text to type"), label=("STRING", "Name of the control"))),
        decl(name="desktop_scroll", description="Scroll a control up or down.",
             parameters=obj(control_id=("STRING", "control_id"), direction=("STRING", "up or down"))),
        decl(name="browser_open_url", description="Open an http(s) URL in the browser.", parameters=obj(url=("STRING", "Full URL"))),
        decl(name="browser_search", description="Search the web in the browser.", parameters=obj(query=("STRING", "Search query"))),
        decl(name="files_list", description="List or search files inside the user's permitted folders.",
             parameters=obj(path=("STRING", "Folder, default home"), query=("STRING", "Optional search text"))),
        decl(name="files_read_open", description="Open a file or folder in its default application (permitted folders only).",
             parameters=obj(path=("STRING", "Path to open"))),
        decl(name="run_terminal", description="Run a bounded PowerShell/terminal command through TARS's guarded executor (read-only commands run; anything state-changing needs confirmation; destructive ones are blocked).",
             parameters=obj(command=("STRING", "The command"))),
        decl(name="analyze_chart", description="Analyse the TradingView/visible chart with TARS's chart pipeline. Use for 'look at the chart', 'what changed'.",
             parameters=obj(question=("STRING", "What to look for, e.g. 'the 15 minute chart, what changed'"))),
        decl(name="confirm_pending_action", description="Run the action waiting for confirmation. ONLY after the user has clearly said yes."),
        decl(name="cancel_pending_action", description="Cancel the action waiting for confirmation (user said no)."),
    ])]


BASE_TOOL_NAMES = {"get_market_context", "get_recent_events", "get_mt5_state", "get_tradingview_state",
                   "get_economic_calendar", "ask_claude"}
TOOL_NAMES = BASE_TOOL_NAMES | DESKTOP_TOOL_NAMES


class TarsTools:
    """Bounded, read-only tools. Nothing here can trade or change state."""

    def __init__(self, app_state, session_id: str):
        self.state, self.session_id = app_state, session_id
        self._last_user = lambda: ("", 0.0)
        self.desktop = DesktopTools(app_state, lambda: self._last_user())

    def bind_last_user(self, getter):
        self._last_user = getter

    async def call(self, name: str, args: dict) -> dict:
        if name not in TOOL_NAMES:
            return {"error": f"unknown tool {name}"}
        try:
            if name in DESKTOP_TOOL_NAMES:
                allowed = {"target", "control_id", "label", "text", "direction", "url", "query", "path", "command", "question"}
                return await self.desktop.call(name, {k: v for k, v in (args or {}).items() if k in allowed})
            return await getattr(self, name)(**{k: v for k, v in (args or {}).items()
                                                if k in {"symbol", "limit", "hours_ahead", "question", "context"}})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("tool %s failed: %s", name, type(exc).__name__)
            return {"error": f"{name} failed: {type(exc).__name__}"}

    async def _status(self) -> dict | None:
        monitors = getattr(self.state, "monitors", None)
        return await monitors.status() if monitors is not None else None

    async def get_market_context(self, symbol: str = "EURUSD") -> dict:
        status = await self._status()
        if status is None:
            return {"available": False, "detail": "monitors are not running"}
        quote = status.get("quote")
        symbol = (symbol or "EURUSD").upper()
        mt5_quotes = status["mt5"].get("quotes", {})
        if quote and quote.get("symbol") == symbol:
            live = {**quote}
        elif symbol in mt5_quotes:
            live = {"symbol": symbol, **mt5_quotes[symbol], "source": "MT5"}
        else:
            live = None
        return {"symbol": symbol, "quote": live,
                "quote_note": None if live else f"No live quote for {symbol} (MT5 {status['mt5']['state']}).",
                "replay_active": bool(status.get("replay")),
                "next_calendar_event": status["calendar"].get("next"),
                "tradingview": status["tradingview"], "mt5_state": status["mt5"]["state"]}

    async def get_recent_events(self, limit: int = 5) -> dict:
        core = getattr(self.state, "realtime_events", None)
        if core is None:
            return {"events": [], "detail": "event core not running"}
        rows = [r for r in reversed(core.recent) if r["decision"] != "IGNORE"][: max(1, min(int(limit or 5), 10))]
        return {"events": [{"title": r["event"]["title"], "summary": r["event"]["summary"],
                            "source": r["event"]["source"], "time": r["event"]["timestamp"],
                            "replay": bool(r["event"]["payload"].get("replay"))} for r in rows]}

    async def get_mt5_state(self) -> dict:
        status = await self._status()
        return status["mt5"] if status else {"state": "UNAVAILABLE"}

    async def get_tradingview_state(self) -> dict:
        status = await self._status()
        return status["tradingview"] if status else {"state": "UNAVAILABLE"}

    async def get_economic_calendar(self, hours_ahead: int = 24) -> dict:
        monitors = getattr(self.state, "monitors", None)
        calendar = (getattr(monitors, "replay_calendar", None) or getattr(monitors, "calendar", None)) if monitors else None
        if calendar is None:
            return {"events": [], "detail": "calendar monitor not running"}
        now, horizon = datetime.now(UTC), max(1, min(int(hours_ahead or 24), 168)) * 3600
        events = [e for e in calendar.events if 0 <= (e.timestamp - now).total_seconds() <= horizon
                  and e.importance in ("High", "Medium")]
        events.sort(key=lambda e: e.timestamp)
        return {"source": calendar.provider.name, "state": calendar.state,
                "events": [{"currency": e.currency, "event": e.event, "at": e.timestamp.isoformat(),
                            "importance": e.importance, "previous": e.previous, "forecast": e.forecast,
                            "actual": e.actual, "replay": e.replay} for e in events[:12]]}

    async def ask_claude(self, question: str = "", context: str = "") -> dict:
        turns = getattr(self.state, "turn_controller", None)
        if turns is None or not question.strip():
            return {"error": "Claude backend unavailable or empty question"}
        live_context = ""
        status = await self._status()
        if status:
            quote = status.get("quote")
            live_context = (f"[TARS live context: quote={quote}; mt5={status['mt5']['state']}; "
                            f"tradingview={status['tradingview']['state']}; next_event={status['calendar'].get('next')}] ")
        text = (f"{live_context}{context.strip() + ' ' if context else ''}{question.strip()} "
                '(Reply for a voice assistant to read aloud: at most 5 short sentences, no markdown, no lists.)')
        answer = ""
        async with asyncio.timeout(90):
            async for event in turns.stream_text(text, turn_id=uuid4().hex, conversation_id=f"{self.session_id}:claude",
                                                 input_mode=InputMode.voice, speak=False):
                if event.type == "complete" and event.response:
                    answer = event.response.display_text
                    if event.response.status.value == "failed":
                        return {"error": "Claude could not answer right now"}
        return {"answer": answer[:3000], "source": "claude"}


class GeminiLiveVoiceSession:
    """Persistent Gemini Live conversation with lazy open and idle close."""

    voice_provider = "GEMINI_LIVE"

    def __init__(self, tools, emit, vad, *, model="gemini-3.8-live", voice=DEFAULT_VOICE, idle_seconds=25.0,
                 connect: Callable[[], Awaitable] | None = None, metrics: LatencyMetrics | None = None,
                 speech_open_frames=8):
        self.tools, self.emit, self.vad = tools, emit, vad
        self.model, self.idle_seconds = model, idle_seconds
        self.voice = voice or DEFAULT_VOICE
        self._last_final_user = ("", 0.0)
        if hasattr(tools, "bind_last_user"):
            tools.bind_last_user(lambda: self._last_final_user)
        self._connect = connect
        self.metrics = metrics or LatencyMetrics()
        self.speech_open_frames = speech_open_frames
        self.session_id = uuid4().hex
        self.generation, self.turn_id = 0, f"{self.session_id}:0"
        self.state = VoiceState.IDLE
        self.closed, self.seq = False, 0
        self.history: deque[dict] = deque(maxlen=300)
        self.stt = SimpleNamespace(active=False, name="gemini_live")
        self.provider_status = {"microphone": "DISCONNECTED", "gemini_live": "IDLE"}
        self.fatal: str | None = None
        self.utterance = 0
        self._stack: contextlib.AsyncExitStack | None = None
        self._live = None
        self._open_task: asyncio.Task | None = None
        self._rx_task: asyncio.Task | None = None
        self._idle_task: asyncio.Task | None = None
        self._tool_tasks: dict[str, asyncio.Task] = {}
        self._pending = bytearray()
        self._preroll: deque[bytes] = deque(maxlen=100)
        self._speech_frames = 0
        self._last_activity = time.monotonic()
        self._user_text = ""
        self._user_open = False
        self._assistant_text = ""
        self._muted = False       # drop stale audio after a local (UI) interrupt until the turn ends
        self._first_audio = False
        self._last_user_at = 0.0
        self.connects = 0

    # ---- events -----------------------------------------------------------
    async def send(self, type_: str, **payload):
        self.seq += 1
        event = {"type": type_, "turn_id": self.turn_id, "seq": self.seq, "ts": time.time(),
                 "generation": self.generation, "voice_provider": self.voice_provider, **payload}
        if type_ not in {"delta", "audio_pcm", "metrics"}:
            self.history.append({k: v for k, v in event.items() if k not in {"audio", "response"}})
        await self.emit(event)

    async def transition(self, state: VoiceState):
        if self.closed or self.state == state:
            return
        self.state = state
        await self.send("state", state=state.value)

    async def _status(self, **detail):
        await self.send("provider_status", providers=self.provider_status.copy(),
                        voice_provider=self.voice_provider, **detail)

    # ---- lifecycle --------------------------------------------------------
    async def start(self):
        await self.transition(VoiceState.LISTENING)
        await self._status()
        self._idle_task = asyncio.create_task(self._idle_watch())

    async def close(self):
        self.closed = True
        for task in (self._open_task, self._rx_task, self._idle_task, *self._tool_tasks.values()):
            if task:
                task.cancel()
        await asyncio.gather(*(t for t in (self._open_task, self._rx_task, self._idle_task,
                                           *self._tool_tasks.values()) if t), return_exceptions=True)
        await self._close_live()
        self.state = VoiceState.IDLE

    async def _close_live(self):
        stack, self._live, self._stack = self._stack, None, None
        self.stt.active = False
        if stack:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(stack.aclose(), 3)

    def _default_connect(self):
        from google import genai
        from google.genai import types
        from app.config import get_settings

        key = get_settings().gemini_api_key
        client = genai.Client(api_key=key)
        config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=SYSTEM_PROMPT,
            speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self.voice))),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            tools=_tool_declarations(),
        )
        return client.aio.live.connect(model=self.model, config=config)

    async def _open(self):
        await self._status(detail="Connecting to Gemini Live")
        self.provider_status["gemini_live"] = "CONNECTING"
        try:
            stack = contextlib.AsyncExitStack()
            cm = self._connect() if self._connect else self._default_connect()
            live = await asyncio.wait_for(stack.enter_async_context(cm), 10)
        except Exception as exc:
            reason = f"{type(exc).__name__}: {str(exc)[:160]}"
            logger.error("gemini live connect failed: %s", reason)
            self.provider_status["gemini_live"] = "ERROR"
            self.fatal = reason
            await self._status(detail=f"Gemini Live unavailable ({reason}); switching to local voice")
            await self.transition(VoiceState.ERROR)
            return
        self._stack, self._live = stack, live
        self.connects += 1
        self.provider_status["gemini_live"] = "CONNECTED"
        self._last_activity = time.monotonic()
        self._rx_task = asyncio.create_task(self._receive())
        # Flush the audio captured while connecting so the first words are not lost.
        preroll, self._preroll = list(self._preroll), deque(maxlen=100)
        for chunk in preroll:
            await self._send_audio(chunk)
        await self._status(detail="Gemini Live connected")

    async def _idle_watch(self):
        while True:
            await asyncio.sleep(1.0)
            if (self._live and not self._tool_tasks and self.state is VoiceState.LISTENING
                    and time.monotonic() - self._last_activity > self.idle_seconds):
                logger.info("gemini live idle for %.0fs: closing session", self.idle_seconds)
                await self._teardown_live("idle")

    async def _teardown_live(self, why: str):
        if self._rx_task:
            self._rx_task.cancel()
            await asyncio.gather(self._rx_task, return_exceptions=True)
            self._rx_task = None
        await self._close_live()
        self._user_open, self._user_text, self._assistant_text = False, "", ""
        self.provider_status["gemini_live"] = "IDLE"
        await self._status(detail=f"Gemini Live session closed ({why}); reconnects on next speech")

    # ---- audio in ---------------------------------------------------------
    async def push_audio(self, frame: bytes):
        if self.closed:
            return
        if self.provider_status["microphone"] != "CONNECTED":
            self.provider_status["microphone"] = "CONNECTED"
            await self._status()
        self._pending.extend(frame)
        while len(self._pending) >= 1024:
            chunk = bytes(self._pending[:1024])
            del self._pending[:1024]
            speech = self.vad(chunk)
            if speech:
                self._last_activity = time.monotonic()
            if self._live:
                await self._send_audio(chunk)
                continue
            self._preroll.append(chunk)
            self._speech_frames = self._speech_frames + 1 if speech else 0
            if self._speech_frames >= self.speech_open_frames and not (self._open_task and not self._open_task.done()):
                self._speech_frames = 0
                self._open_task = asyncio.create_task(self._open())

    async def _send_audio(self, chunk: bytes):
        live = self._live
        if live is None:
            return
        from google.genai import types
        try:
            await live.send_realtime_input(audio=types.Blob(data=chunk, mime_type=f"audio/pcm;rate={INPUT_RATE}"))
        except Exception as exc:
            logger.warning("gemini send failed: %s", type(exc).__name__)
            await self._teardown_live("connection lost")

    # ---- events from Gemini ----------------------------------------------
    async def _receive(self):
        try:
            while self._live is not None:
                async for message in self._live.receive():
                    await self._handle(message)
                    if self._live is None:
                        return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("gemini receive ended: %s %s", type(exc).__name__, str(exc)[:160])
            if not self.closed and self._live is not None:
                self.provider_status["gemini_live"] = "ERROR"
                await self._status(detail=f"Gemini Live connection lost ({type(exc).__name__})")
                await self._teardown_live("connection lost")
                await self.transition(VoiceState.LISTENING)

    async def _handle(self, message):
        content = getattr(message, "server_content", None)
        if getattr(message, "tool_call", None):
            for call in message.tool_call.function_calls or []:
                self._tool_tasks[call.id] = asyncio.create_task(self._run_tool(call))
        if getattr(message, "tool_call_cancellation", None):
            for call_id in message.tool_call_cancellation.ids or []:
                task = self._tool_tasks.pop(call_id, None)
                if task:
                    task.cancel()
        if getattr(message, "go_away", None):
            logger.info("gemini live go_away; session will reopen on next speech")
            self._last_activity = 0
        if content is None:
            return
        self._last_activity = time.monotonic()
        if getattr(content, "interrupted", None):
            await self._on_interrupted()
        if content.input_transcription and content.input_transcription.text:
            await self._on_user_text(content.input_transcription.text)
        if content.model_turn:
            for part in content.model_turn.parts or []:
                if part.inline_data and part.inline_data.data:
                    await self._on_audio(part.inline_data.data)
        if content.output_transcription and content.output_transcription.text and not self._muted:
            self._assistant_text += content.output_transcription.text
            await self.send("delta", text=content.output_transcription.text)
        if content.turn_complete:
            await self._on_turn_complete()

    async def _on_user_text(self, text: str):
        if not self._user_open:
            self._user_open, self._user_text = True, ""
            self.utterance += 1
            self.stt.active = True
            await self.send("speech_started")
            await self.transition(VoiceState.USER_SPEAKING)
        self._user_text += text
        self._last_user_at = time.perf_counter()
        await self.send("partial_transcript", text=resolve_trading_terms(self._user_text.strip()))

    async def _finalize_user(self):
        if self._user_open:
            self._user_open = False
            self.stt.active = False
            final = resolve_trading_terms(self._user_text.strip())
            self._last_final_user = (final, time.monotonic())
            await self.send("final_transcript", text=final)
            self._user_text = ""

    async def _on_audio(self, data: bytes):
        if self._muted:
            return
        await self._finalize_user()
        if not self._first_audio:
            self._first_audio = True
            if self._last_user_at:
                self.metrics.latest["last_user_text_to_first_audio"] = round((time.perf_counter() - self._last_user_at) * 1000, 1)
            await self.transition(VoiceState.ASSISTANT_SPEAKING)
        await self.send("audio_pcm", sample_rate=OUTPUT_RATE, audio=base64.b64encode(data).decode("ascii"))

    async def _on_interrupted(self):
        # Gemini detected the user talking over the model: it has already stopped generating.
        previous = self.turn_id
        self.generation += 1
        self.turn_id = f"{self.session_id}:{self.generation}"
        self._first_audio, self._muted, self._assistant_text = False, False, ""
        self.metrics.latest["interrupts"] = self.metrics.latest.get("interrupts", 0) + 1
        await self.transition(VoiceState.INTERRUPTING)
        await self.send("interrupt", previous_turn_id=previous, status="interrupted")
        await self.transition(VoiceState.USER_SPEAKING)

    async def _on_turn_complete(self):
        await self._finalize_user()
        text = self._assistant_text.strip()
        self._assistant_text, self._first_audio, self._muted = "", False, False
        if text:
            await self.send("response_complete", response={
                "display_text": text, "speech_text": text, "status": "completed",
                "provider": "gemini_live", "turn_id": self.turn_id})
        await self.transition(VoiceState.LISTENING)

    # ---- tools --------------------------------------------------------------
    async def _run_tool(self, call):
        from google.genai import types
        name, args = call.name, dict(call.args or {})
        await self.send("tool_call", name=name)
        if self.state in (VoiceState.LISTENING, VoiceState.USER_SPEAKING):
            await self.transition(VoiceState.THINKING)
        started = time.perf_counter()
        try:
            result = await self.tools.call(name, args)
        except asyncio.CancelledError:
            raise
        finally:
            self._tool_tasks.pop(call.id, None)
        self.metrics.latest[f"tool_{name}_ms"] = round((time.perf_counter() - started) * 1000, 1)
        self._last_activity = time.monotonic()
        live = self._live
        if live is None:
            return
        try:
            await live.send_tool_response(function_responses=[
                types.FunctionResponse(id=call.id, name=name, response=result)])
        except Exception as exc:
            logger.warning("send_tool_response failed: %s", type(exc).__name__)

    # ---- controls used by the router / UI ------------------------------------
    async def interrupt(self):
        """UI-initiated stop: drop audio locally now; the model's stale audio is muted until it ends."""
        previous = self.turn_id
        self.generation += 1
        self.turn_id = f"{self.session_id}:{self.generation}"
        self._muted = self._first_audio or self.state is VoiceState.ASSISTANT_SPEAKING
        self._first_audio, self._assistant_text = False, ""
        await self.send("interrupt", previous_turn_id=previous, status="interrupted")

    async def playback(self, message: dict):
        return None  # Gemini output plays client-side without acks; nothing to reconcile

    async def speak_alert(self, text: str):
        if self.closed or not self._live or self.state is not VoiceState.LISTENING:
            return
        with contextlib.suppress(Exception):
            await self._live.send_realtime_input(text=f"Briefly tell the user this alert: {text}")
