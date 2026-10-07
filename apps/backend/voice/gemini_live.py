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
from datetime import UTC, datetime, timedelta
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
# A working microphone path, even in a quiet room, is far above this; drivers that gate silence sit at -100 dB.
MIC_SILENT_DB = -85.0
REPORT_WAIT_S = 2.2  # if Gemini has not started speaking this long after a quick tool finished, TARS reports it
REPORT_TOOLS = {"desktop_open_app", "desktop_focus_window", "desktop_close_app", "desktop_open_start",
                "desktop_scroll", "calculator_calculate", "tradingview_set_symbol", "tradingview_set_timeframe",
                "system_control", "browser_open_url", "browser_search", "web_navigate", "web_new_tab",
                "pause_market_watching", "resume_market_watching", "watch_market", "unwatch_market"}
FORCE_END_SILENCE_S = 1.2  # local-VAD silence after which an unanswered turn is ended by us, not the server
ACK_ECHO_TAIL_S = 0.6  # room/speaker tail after the local confirmation ends
END_SILENCE_MS = 400  # user-silence before Gemini commits the turn
MIC_SILENT_AFTER_S = 90.0
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
- Simple, structured questions never need Claude: "what's EURUSD bid/ask", "do I have a position", "what's my
  floating P&L", "what's my equity", "when is CPI", "any major events today", "latest gold news" -- answer those
  directly from get_mt5_state/get_market_context/get_economic_calendar/get_news. Call ask_claude only when the
  question genuinely needs synthesis across sources (an "analyze"/"outlook"/"what's happening with" question),
  and when it names or clearly implies one instrument, pass that as ask_claude's `symbol` so it gets the real
  MT5+TradingView+Calendar+News evidence in that one call instead of you gathering it turn by turn. `symbol` is
  NOT limited to the FX majors below -- pass the instrument exactly as the user said it (a company name like
  "Nvidia", "gold", "bitcoin", "the Nasdaq", a ticker, or a cross you don't recognize); TARS's own AssetResolver
  does the real resolution against what this instance actually supports, and reports back if it's ambiguous (ask
  the user which one) or not a real instrument (say so plainly) -- never guess a ticker yourself for an asset
  you don't recognize, just pass the name through as-is and let ask_claude's result tell you.

Trading vocabulary (spoken -> symbol; the common/fast cases -- understand these yourself, but ask_claude's
AssetResolver also covers far more: other FX crosses, silver, crypto, major indices, oil, and large-cap equities)
EURUSD "euro dollar", "euro U.S. dollar"; GBPUSD "pound dollar", "sterling dollar", "cable"; USDJPY "dollar yen";
EURJPY "euro yen"; GBPJPY "pound yen"; XAUUSD "gold", "gold dollar"; XAGUSD "silver"; BTCUSD "bitcoin dollar";
ETHUSD "ether", "ethereum dollar"; SPX "S&P 500"; NDX/NASDAQ "Nasdaq"; DXY "U.S. Dollar Index".
Speech recognition can garble tickers ("the rusty", "you are you S D"). When the sentence is clearly a market question,
resolve the odd word to the most plausible symbol from this list and use the resolved symbol in tool calls. If two
symbols are equally plausible, ask which one.

Facts and tools
- Fast path stays fast -- never route a simple fact through analyze_market/ask_claude: "EURUSD bid/ask" ->
  get_mt5_state/get_market_context; "what timeframe am I on" -> get_tradingview_state; "next USD event" ->
  get_economic_calendar; "latest Nvidia news" -> get_news. If a source is disconnected, say so plainly. Data
  labelled DEMO REPLAY is a rehearsal, not live: always say it is a replay.
- "What's happening with X today" / "analyze gold" / "explain oil today" / "analyze it again" -> analyze_market.
  This is the Universal Market Explainer: it resolves ANY instrument (not just the shortlist above), takes over
  TradingView for you (switching symbol/timeframe is fine for an explicit analysis request), and returns one
  integrated answer. Omit `asset` for a bare follow-up ("what about 5 minutes", "go to one hour") -- it reuses
  the current instrument; pass `timeframe` when the user names one, omit it for a broad "today" question. After
  a successful call, "any news coming" -> get_today_market_news and "what caused that move/candle/spike" ->
  explain_move both automatically reuse that same instrument -- never ask the user to repeat it. If
  analyze_market reports AMBIGUOUS or NOT_FOUND, say exactly that and ask which instrument -- never guess a
  ticker yourself.
- Compound command rule: a single sentence that both opens/names TradingView (or an asset) AND asks you to
  analyze/observe/report on it is ONE market-analysis intent, even though it starts with "open" -- e.g. "Open
  TradingView and open the asset XAUUSD in 15 minute timeframe and observe and report what do you observe",
  "Open TradingView and analyze gold on 15 minutes", "Show me EURUSD on 5m and tell me what's happening", "Pull
  up Bitcoin and tell me what you see", "Go to TradingView, open EURUSD 15 minute and observe the chart". Call
  analyze_market ONCE with the named asset/timeframe and a `question` built from the observe/report language --
  do not call desktop_open_app first and do not manually chain tradingview_set_symbol/tradingview_set_timeframe/
  analyze_chart yourself. analyze_market already focuses/opens TradingView, verifies the symbol and timeframe
  switch, captures a fresh chart and returns the synthesis answer as one atomic call; duplicating that sequence
  yourself only risks stopping partway and reporting state that was never verified. The giveaway is
  analyze/observe/report/"what do you see"/"what's happening"/"tell me what's happening" language anywhere in
  the sentence, even after an "open" clause. Do not say the command is done after merely opening the app or
  switching the chart -- it is only complete once analyze_market's answer comes back (or it reports a concrete
  failure: AMBIGUOUS, NOT_FOUND, CHART_UNAVAILABLE, SYMBOL_NOT_VERIFIED -- say exactly that, never go silent).
- A compound command that names an asset/timeframe but has NO analyze/observe/report language ("Open
  TradingView and show XAUUSD on 15m", "open TradingView and open EURUSD on 5 minutes") is navigation only:
  desktop_open_app (or rely on tradingview_set_symbol/tradingview_set_timeframe to focus it if already running),
  then tradingview_set_symbol and tradingview_set_timeframe as usual -- do not call analyze_market or
  analyze_chart for these, and do not launch a deep synthesis the user did not ask for. A bare "Open
  TradingView"/"Open Calculator" with no asset, timeframe or analysis language stays a single desktop_open_app
  call -- nothing else.
- "Look at the chart / what changed / analyze this chart" (deictic, about whatever is literally visible right
  now, not a named asset) -> analyze_chart. This always captures the chart FRESH and
  gives you a real answer in this same turn -- it never depends on or waits for the background watcher, so never tell
  the user to "keep it visible and ask again"; if it fails, say exactly what it reported (e.g. that what's in front
  isn't a supported chart). "Watch this for me" / "monitor EURUSD" / "keep an eye on this" -> watch_this_chart
  (acknowledges TARS's background chart watcher immediately; it does not run an analysis and must not be used for
  "analyze"/"what do you see"). "What is on my screen / which app is open" -> desktop_context, and trust its
  primary_visible_window over anything you remember from earlier in the conversation -- an app you opened a while
  ago may no longer be what's in front now. Never claim you looked at something you did not.

Desktop control (all through TARS's guarded action layer)
- TARS resolves applications itself from what is actually installed on this machine -- you never know or
  guess an executable name or file path. For desktop_open_app/desktop_focus_window, always pass the app's
  plain spoken name exactly as the user said it (e.g. "clock", "calculator", "tradingview", "mt5"), never an
  invented .exe name. If you are unsure whether something is installed, call desktop_resolve_app or
  desktop_list_installed_apps first.
- "Open X" (an application) -> desktop_open_app. "Open x.com" or "open the X website" -> browser_open_url.
  "Search for X" / "search the web for X" -> browser_search. Never call browser_open_url or browser_search as a
  substitute for desktop_open_app -- if it returns NOT_INSTALLED (not found) or is ambiguous (matches more than
  one installed app), tell the user exactly that and ask what they want, or ask which one they meant. Do not
  silently fall back to the browser, and do not open a browser unless the user asked for a website or a search.
- You can open/switch apps, inspect and list controls, scroll, open URLs, search the web, list/open files and run
  bounded terminal commands. Tell the user in a few words what you did and report the tool's real result: DONE,
  NOT_FOUND, NEEDS_CONFIRMATION, BLOCKED or FAILED. Never pretend it worked.
- "Open Start" / "Open the Start menu" / "Open Start and search for Bluetooth" / "Open Start and open Calculator"
  -> desktop_open_start, never desktop_open_app (Start is not an installed application). Pass `query` only when
  the user names something to search for, and `activate_top_result` only when they want that result opened/
  activated, not just shown.
- A deterministic backend policy -- never you -- decides whether an action needs confirmation; you only report
  the real result it gives back. Ordinary local navigation (opening/focusing an app, the Start menu, scrolling,
  an ordinary click like "click that"/"switch to Chrome") normally just runs, unless the user has never granted
  "trusted desktop control," in which case an ordinary click may still ask for confirmation once -- that is the
  real policy answering, never refuse a harmless request yourself or say "I don't have permission" on your own
  guess. Anything consequential -- sending a message, buying, deleting, installing, signing in, or a terminal
  command -- always asks regardless. "Enable/disable trusted desktop control" -> desktop_enable_trusted_control/
  desktop_disable_trusted_control (only on an explicit request). "What permissions do you have?" ->
  desktop_get_permissions -- never guess the answer yourself.
- TradingView: once it is open, "switch to EURUSD" / "show gold" -> tradingview_set_symbol; "make it fifteen
  minutes" / "go to the one-hour chart" -> tradingview_set_timeframe; "what am I looking at" (when TradingView is
  the app in view) -> tradingview_status first (fast, no vision call) before falling back to analyze_chart. These
  always target the currently open TradingView window -- you do not need to re-open or re-name it for a follow-up
  in the same conversation. If a symbol/timeframe change reports NOT_VERIFIED, say you asked for the change but
  couldn't confirm it took effect; do not claim it worked.
- "Close File Explorer" / "close Notepad" / "close the calculator" -> desktop_close_app with that app's plain name (it
  closes that named app right away; no confirmation is needed unless the tool itself returns NEEDS_CONFIRMATION).
  Never call confirm_pending_action unless a confirmation is actually waiting and the user has just said yes.
  Closing an app window is unrelated to the trading rule about orders and positions below.
- If a click, typing or other state-changing action returns NEEDS_CONFIRMATION, ask the user plainly ("Click Save in
  Notepad, yes?"). Only after they clearly say yes call confirm_pending_action; if they say no call
  cancel_pending_action. Never confirm on your own.
- If a control cannot be found or you are unsure which one to click, say so and ask; do not guess.

Browser agent (web_* tools -- a real, separate Chrome, not TARS's own panel)
- "Open Chrome" is an application -> desktop_open_app. Once a page is open (or to go straight to a site), use
  web_navigate/web_find/web_click/web_type/web_select/web_scroll/web_extract_text/web_get_links/web_extract_table --
  these control a real, separate Chrome window over its DevTools Protocol, resolving your plain description (e.g.
  "the search box", "the first result", "the login button") against the live page every time. Never invent a CSS
  selector, an element id or a screen coordinate -- you do not have those, only plain descriptions.
  "Search Google/YouTube for X" -> web_navigate to the site (or a search URL) then web_find/web_type into the search
  box, or web_navigate straight to a search results URL when that is simpler; "open the first result" -> web_click
  with a plain description, using web_get_context/web_list_tabs to know what's currently open if unsure. These calls
  target the tab TARS itself last acted on -- you do not need to re-say the URL or re-open the tab for a same-session
  follow-up.
- Report web_* results the same honest way: DONE/SUCCESS, NOT_FOUND ("that wasn't on the page"), AMBIGUOUS
  (multiple things matched -- ask which one), PARTIAL (it navigated/went back but the page was still loading when
  checked), or FAILED. Never say a click or navigation worked when the tool reported anything else.
- Before a consequential web action (purchase, payment, sending a message/email, submitting an important form,
  deleting data, changing credentials) the same confirmation rule applies -- these report NEEDS_CONFIRMATION/
  CONFIRM_REQUIRED and must not be assumed pre-approved.

Background watching and daily brief
- "Watch gold" / "watch Bitcoin" / "watch this for me" -> watch_market. Omit `asset` for "watch this" to reuse
  the current instrument in context (same follow-up logic as analyze_market) -- never guess an instrument if
  there is no current one, ask instead. This is different from watch_this_chart: watch_market adds the
  instrument to TARS's persistent background watchlist (proactive alerts later, across restarts), it never
  touches TradingView. "Stop watching gold" -> unwatch_market. "What are you watching?" -> list_watched_markets.
  "Pause market monitoring" / "resume monitoring" -> pause_market_watching / resume_market_watching (pausing
  keeps the watchlist, it just stops proactive alerts). A watched instrument may later raise a proactive alert
  on its own (get_recent_events shows these) -- TARS only escalates to deep reasoning for a genuinely
  correlated, multi-factor story, never for an ordinary single price tick or a routine calendar reminder.
- "Give me today's market brief" / "give me today's briefing again" -> get_daily_market_brief. Works any time,
  regardless of whether TARS already sent one automatically today.

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
        decl(name="get_news", description="Recent financial news headlines from TARS's live news monitor, optionally filtered to one symbol. Use for 'check the latest news about X' -- prefer this over browser_search for financial headlines.",
             parameters=obj(symbol=("STRING", "Optional symbol/asset to filter by, e.g. XAUUSD or gold"))),
        decl(name="ask_claude", description="Delegate deep trading/chart analysis, strategy research or complex synthesis to Claude. Pass `symbol` for trading questions (e.g. 'analyze gold considering the chart, my position, upcoming events and news') -- this builds the full real MT5+TradingView+Calendar+News evidence package for Claude in the same single call, rather than a separate call per source. Takes a few seconds.",
             parameters=obj(question=("STRING", "The full question to analyse"),
                            context=("STRING", "Optional extra context from the conversation"),
                            symbol=("STRING", "Symbol to analyze, e.g. EURUSD or XAUUSD -- only for trading analysis questions"))),
        decl(name="analyze_market", description="THE Universal Market Explainer -- use for 'what's happening with X today', 'analyze gold', 'explain oil today', a bare follow-up like 'analyze it again' (omit `asset` to reuse the current one), AND any compound sentence that both opens/names TradingView (or an asset) and asks to observe/analyze/report on it, even if it starts with 'open' -- e.g. 'Open TradingView and open the asset XAUUSD in 15 minute timeframe and observe and report what do you observe', 'Open TradingView and analyze gold on 15 minutes', 'show me EURUSD on 5m and tell me what's happening'. This already opens/focuses TradingView itself -- never call desktop_open_app first and never manually chain tradingview_set_symbol/tradingview_set_timeframe/analyze_chart for one of these sentences; call this ONE tool instead. Resolves the asset (any instrument, not just majors), takes over TradingView (switches symbol/timeframe, each verified -- acceptable for an explicit analysis request, unlike a simple quote question), collects one or more fresh chart reads, and returns ONE integrated answer covering current state, context, catalysts, scenarios and upcoming risk. Takes up to a couple of minutes for a broad multi-timeframe request -- say a brief lead-in first ('Let me pull that up'). Reports AMBIGUOUS/NOT_FOUND/CHART_UNAVAILABLE/SYMBOL_NOT_VERIFIED honestly instead of guessing a ticker or claiming it worked.",
             parameters=obj(asset=("STRING", "The instrument as the user said it -- a company name, 'gold', 'bitcoin', a ticker, a cross. Omit to mean 'the current asset' for a follow-up."),
                            timeframe=("STRING", "Optional: an explicit timeframe like '5m'/'1h'/'4h'. Omit for a broad 'what's happening today' question -- a small default multi-timeframe sequence is used instead."),
                            question=("STRING", "Optional: the specific question, e.g. 'what about 5 minutes' or 'what's the outlook into New York'"))),
        decl(name="get_today_market_news", description="'What's on the news today' / 'any news for X' -- financial/market news and high-impact calendar only, never general news. Uses the current asset in context if none is named.",
             parameters=obj(asset=("STRING", "Optional instrument to filter to; omit to use the current asset in context, or for general market news if there is none"))),
        decl(name="explain_move", description="'Why did that move?' / 'what caused that drop/spike?' / 'what caused that candle?' -- bounded historical search (+/-15 then 30 then 60 minutes around now) across persisted calendar/news/price events for the current asset, with an honest uncertainty-aware answer (never a bare causal claim the evidence doesn't support).",
             parameters=obj(question=("STRING", "Optional: the specific move/question being asked about"))),
        decl(name="watch_market", description="'Watch gold.' / 'watch this for me.' / 'watch Bitcoin.' Adds an instrument to TARS's persistent background watchlist, which raises a proactive alert later if something significant happens -- never touches TradingView itself. Omit `asset` for 'watch this' to reuse the current instrument in context.",
             parameters=obj(asset=("STRING", "The instrument as the user said it -- company name, 'gold', 'bitcoin', a ticker. Omit to mean 'the current asset'."))),
        decl(name="unwatch_market", description="'Stop watching gold.' / 'stop watching Bitcoin.' Removes an instrument from the background watchlist.",
             parameters=obj(asset=("STRING", "The instrument as the user said it, or omit to mean 'the current asset'."))),
        decl(name="list_watched_markets", description="'What are you watching?' -- the current persistent watchlist and whether background watching is paused."),
        decl(name="pause_market_watching", description="'Pause market monitoring.' Stops the background watcher's proactive alerts without deleting the watchlist."),
        decl(name="resume_market_watching", description="'Resume monitoring.' Resumes background watching after a pause."),
        decl(name="get_watcher_status", description="Full background watcher status: paused or active, which instruments are watched, basic activity counters."),
        decl(name="get_daily_market_brief", description="'Give me today's market brief.' / 'give me today's briefing again.' Always returns a fresh brief when explicitly asked, regardless of whether one was already sent automatically today."),
        decl(name="desktop_context", description="What is on the desktop right now: active application/window and the recent desktop actions TARS took. Use for 'what am I looking at', 'which app is open'."),
        decl(name="desktop_resolve_app", description="Look up whether an application name resolves to a specific installed Windows application, without opening it. Use this if you are unsure an app is installed, or to check before telling the user something is or isn't available.",
             parameters=obj(target=("STRING", "The app's plain spoken name, e.g. clock, calculator, tradingview, mt5"))),
        decl(name="desktop_list_installed_apps", description="List installed Windows applications TARS can open, optionally filtered by a search term. Use this to answer 'what apps do you see' or to find the right name before opening.",
             parameters=obj(query=("STRING", "Optional filter, e.g. 'chrome' or leave empty to list common ones"))),
        decl(name="desktop_open_app", description="Open/launch a Windows application by its plain spoken name (e.g. clock, calculator, tradingview, mt5, vs code). TARS resolves the real installed application itself -- NEVER pass a guessed .exe filename or path, and NEVER call browser_open_url or browser_search as a substitute if this returns NOT_INSTALLED or is ambiguous; tell the user what happened and ask instead.",
             parameters=obj(target=("STRING", "The app's plain spoken name, exactly as the user said it"))),
        decl(name="desktop_focus_window", description="Bring an already running application/window to the front (switch to it). Use the plain spoken app name here too.",
             parameters=obj(target=("STRING", "Application name, e.g. tradingview, chrome, metatrader"))),
        decl(name="desktop_close_app", description="'Close File Explorer' / 'close Notepad' / 'close the calculator': closes that NAMED application's window immediately (the app itself still prompts about unsaved work). Use this, never system_control, to close anything by name. If it ever returns NEEDS_CONFIRMATION, ask yes/no and only then call confirm_pending_action.",
             parameters=obj(target=("STRING", "Application name, e.g. calculator, notepad"))),
        decl(name="desktop_open_start", description="'Open Start.' / 'Open the Start menu.' / 'Open Start and search for Bluetooth.' / 'Open Start and open Calculator.' Opens the real Windows Start menu (never a guess -- the real taskbar Start button), optionally types a query into its own built-in search, and optionally activates the matching search result. Ordinary Tier-1/2 local navigation -- never needs confirmation.",
             parameters=obj(query=("STRING", "Optional text to search for in Start, e.g. 'Bluetooth' or 'Calculator'. Leave empty to just open Start."),
                            activate_top_result=("BOOLEAN", "True to open/activate the matching search result after typing the query (e.g. 'open Start and open Calculator'); false to just leave the search showing."))),
        decl(name="desktop_enable_trusted_control", description="'Enable trusted desktop control.' Grants TARS broad, explicit authority to perform ordinary, reversible local desktop navigation (opening/focusing apps, Start menu, scrolling, ordinary clicks) without asking for confirmation each time. Anything consequential (sending something, buying, deleting, installing, signing in, trading, terminal commands) still asks regardless. Only call this when the user explicitly asks to enable/grant this."),
        decl(name="desktop_disable_trusted_control", description="'Disable trusted desktop control.' Reverts to the conservative default where every state-changing desktop click/selection asks for confirmation."),
        decl(name="desktop_get_permissions", description="'What permissions do you have?' Reports whether trusted desktop control is currently on or off and what that means. Use this instead of guessing when the user asks about TARS's own permissions."),
        decl(name="calculator_calculate", description="'Calculate 2345 times 17' / 'what's (1250+750)/4' -- opens the real Windows Calculator (verified foreground) and performs ONE strictly-validated arithmetic expression using Calculator's own buttons, then verifies the displayed result. Never asks for confirmation -- it can only ever touch the sandboxed Calculator app with a pre-validated numeric expression, nothing else. Use this instead of desktop_open_app+desktop_click_control for any arithmetic request.",
             parameters=obj(expression=("STRING", "Plain arithmetic only: digits, + - * / %, parentheses, e.g. '2345*17' or '(1250+750)/4'. Never anything else."))),
        decl(name="desktop_list_controls", description="List clickable/typeable UI controls of the active or a named window (Windows UI Automation). Use before clicking.",
             parameters=obj(target=("STRING", "Optional window to inspect"))),
        decl(name="desktop_click_control", description="Click a control found via desktop_list_controls. Needs the user's confirmation. Never usable for MT5 order controls.",
             parameters=obj(control_id=("STRING", "control_id from desktop_list_controls"), label=("STRING", "Human-readable name of what is clicked"))),
        decl(name="desktop_type_text", description="Type text into a control found via desktop_list_controls. Needs the user's confirmation.",
             parameters=obj(control_id=("STRING", "control_id"), text=("STRING", "Text to type"), label=("STRING", "Name of the control"))),
        decl(name="system_control", description="Native laptop control, executed locally in well under a second with the result read back from Windows -- never use screenshots or Settings for these. Actions: set_volume(percent), volume_up/volume_down(step, default 10), mute_audio, unmute_audio, get_volume; mute_microphone, unmute_microphone, get_microphone_mute; set_brightness(percent), brightness_up/brightness_down(step), get_brightness (reports UNSUPPORTED if the display has no software brightness); media_play_pause ('play'/'pause'/'resume'), media_next, media_previous, media_stop (controls whatever media session is active -- do not assume an app); get_battery_status; window_minimize, window_maximize, window_restore, window_switch (these act on the window currently in front); and the CONFIRMATION-REQUIRED actions shutdown, restart, sleep -- for those, ask the user to say yes or no, then call confirm_pending_action; never run them without it. Ordinary actions need no confirmation. Report only what the tool returned.",
             parameters=obj(action=("STRING", "One of the action names above"), percent=("NUMBER", "0-100, for set_volume/set_brightness"), step=("NUMBER", "Amount for *_up/*_down, default 10"))),
        decl(name="desktop_scroll", description="Scroll up/down/left/right. For 'scroll down'/'scroll up' with no specific target named, omit control_id entirely -- it scrolls whatever window is currently in front. Pass a control_id from desktop_list_controls only when scrolling one specific control inside a window.",
             parameters=obj(control_id=("STRING", "Optional control_id from desktop_list_controls; omit to scroll the current window"), direction=("STRING", "up, down, left or right"))),
        decl(name="browser_open_url", description="Open an http(s) URL in the browser.", parameters=obj(url=("STRING", "Full URL"))),
        decl(name="browser_search", description="Search the web in the browser.", parameters=obj(query=("STRING", "Search query"))),
        decl(name="files_list", description="List or search files inside the user's permitted folders.",
             parameters=obj(path=("STRING", "Folder, default home"), query=("STRING", "Optional search text"))),
        decl(name="files_read_open", description="Open a file or folder in its default application (permitted folders only).",
             parameters=obj(path=("STRING", "Path to open"))),
        decl(name="run_terminal", description="Run a bounded PowerShell/terminal command through TARS's guarded executor (read-only commands run; anything state-changing needs confirmation; destructive ones are blocked).",
             parameters=obj(command=("STRING", "The command"))),
        decl(name="analyze_chart", description="Synchronously capture and analyse the chart that is actually in front right now, and return a real read in this same turn -- never depends on or waits for the background watcher. Use for 'look at the chart', 'what changed', 'analyze this chart'. If the front window isn't a supported chart, it reports that honestly instead of analyzing the wrong thing.",
             parameters=obj(question=("STRING", "What to look for, e.g. 'the 15 minute chart, what changed'"))),
        decl(name="watch_this_chart", description="Acknowledge TARS's background chart watcher for the chart currently in front, and return immediately -- it does NOT run an analysis. Use only for an explicit 'watch this for me' / 'monitor EURUSD' / 'keep an eye on this' request, never as a substitute for analyze_chart."),
        decl(name="tradingview_status", description="The TradingView window's current symbol and timeframe, from TARS's own background chart monitor -- fast, no vision call. Use for 'what am I looking at' when TradingView is the current trading app, or to check before changing it."),
        decl(name="tradingview_set_symbol", description="Change the symbol on the running TradingView chart (e.g. switch to EURUSD, show gold). TradingView must already be open -- open it first if it is not the current trading app.",
             parameters=obj(symbol=("STRING", "Symbol/ticker as the user said it, e.g. EURUSD, gold, XAUUSD"))),
        decl(name="tradingview_set_timeframe", description="Change the timeframe/interval on the running TradingView chart (e.g. make it fifteen minutes, go to the one-hour chart).",
             parameters=obj(timeframe=("STRING", "Timeframe as the user said it, e.g. 15m, 1h, 1D"))),
        decl(name="confirm_pending_action", description="Run the action waiting for confirmation. ONLY after the user has clearly said yes."),
        decl(name="cancel_pending_action", description="Cancel the action waiting for confirmation (user said no)."),
        decl(name="web_get_context", description="The real browser's current URL, title and tab count. Use for 'what page am I on', or before a follow-up like 'open the first result' to confirm you're still on a results page."),
        decl(name="web_list_tabs", description="List open tabs in the real browser."),
        decl(name="web_focus_tab", description="Switch to an already-open tab.", parameters=obj(target=("STRING", "Tab index, or a word from its title/URL"))),
        decl(name="web_new_tab", description="Open a new browser tab, optionally at a URL.", parameters=obj(url=("STRING", "Optional URL; leave empty for a blank tab"))),
        decl(name="web_close_tab", description="Close a tab.", parameters=obj(target=("STRING", "Tab index/title word; leave empty for the current tab"))),
        decl(name="web_navigate", description="Go to a URL in the real browser (e.g. after 'open Chrome', 'go to YouTube'). Verified against the page actually loading -- do not claim success if it reports it did not finish loading.",
             parameters=obj(url=("STRING", "Full http(s) URL"))),
        decl(name="web_back", description="Browser back."),
        decl(name="web_forward", description="Browser forward."),
        decl(name="web_refresh", description="Reload the current page."),
        decl(name="web_find", description="Check whether something matching a plain description is on the current page, without acting on it. Use when unsure before web_click.",
             parameters=obj(target=("STRING", "Plain description, e.g. 'the login button', 'the first video result'"))),
        decl(name="web_click", description="Click something on the real page by plain description (e.g. 'the first result', 'the login button'), resolved against the live page -- never guess a CSS selector or coordinates. Reports NOT_FOUND/AMBIGUOUS honestly rather than guessing.",
             parameters=obj(target=("STRING", "Plain description of what to click"))),
        decl(name="web_type", description="Type text into an input/search box on the real page, identified by plain description.",
             parameters=obj(target=("STRING", "Plain description of the field, e.g. 'the search box'"),
                            text=("STRING", "Text to type"), submit=("BOOLEAN", "Press Enter / submit the form after typing, default false"))),
        decl(name="web_select", description="Choose an option in a dropdown on the real page.",
             parameters=obj(target=("STRING", "Plain description of the dropdown"), value=("STRING", "Option text or value to select"))),
        decl(name="web_scroll", description="Scroll the real page, or scroll a described element into view.",
             parameters=obj(direction=("STRING", "up/down/top/bottom, default down"), target=("STRING", "Optional: scroll this element into view instead"))),
        decl(name="web_wait_for", description="Wait briefly for something to appear on the page (e.g. after a search, before clicking a result).",
             parameters=obj(target=("STRING", "Plain description of what to wait for"), timeout=("NUMBER", "Seconds to wait, default 10, max 30"))),
        decl(name="web_extract_text", description="Read the current page's visible text.", parameters=obj(mode=("STRING", "all/summary/headings, default summary"))),
        decl(name="web_extract_table", description="Extract a table from the current page as rows of cell text.", parameters=obj(target=("STRING", "Optional word to find the right table if there are several"))),
        decl(name="web_get_links", description="List the links on the current page (text + URL)."),
        decl(name="web_download", description="Download a file by clicking a described link/button (e.g. 'the PDF', 'the first download link') and waiting for it to actually finish -- never claim it downloaded just because the link was clicked.",
             parameters=obj(target=("STRING", "Plain description of what to click to start the download"))),
        decl(name="web_get_last_download", description="The most recent completed download this session: filename, local path and size. Use for 'where did it download' / 'what did I just download'."),
    ])]


BASE_TOOL_NAMES = {"get_market_context", "get_recent_events", "get_mt5_state", "get_tradingview_state",
                   "get_economic_calendar", "get_news", "ask_claude",
                   "analyze_market", "get_today_market_news", "explain_move",
                   "watch_market", "unwatch_market", "list_watched_markets",
                   "pause_market_watching", "resume_market_watching",
                   "get_watcher_status", "get_daily_market_brief"}
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
                allowed = {"target", "control_id", "label", "text", "direction", "url", "query", "path", "command",
                          "question", "symbol", "timeframe", "value", "submit", "timeout", "mode", "expression",
                          "activate_top_result", "action", "percent", "step"}
                return await self.desktop.call(name, {k: v for k, v in (args or {}).items() if k in allowed})
            return await getattr(self, name)(**{k: v for k, v in (args or {}).items()
                                                if k in {"symbol", "limit", "hours_ahead", "question", "context",
                                                        "asset", "timeframe"}})
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

    async def get_news(self, symbol: str = "") -> dict:
        monitors = getattr(self.state, "monitors", None)
        news = getattr(monitors, "news", None) if monitors else None
        if news is None:
            return {"items": [], "detail": "news monitor not running"}
        rows = news.latest(symbols=[symbol] if symbol else None, limit=8)
        return {"source": news.provider.name, "state": news.state,
                "items": [{"headline": r["headline"], "url": r["url"], "published_at": r["published_at"].isoformat(),
                          "source": r["source"]} for r in rows]}

    async def ask_claude(self, question: str = "", context: str = "", symbol: str = "") -> dict:
        turns = getattr(self.state, "turn_controller", None)
        if turns is None or not question.strip():
            return {"error": "Claude backend unavailable or empty question"}
        live_context = ""
        monitors = getattr(self.state, "monitors", None)
        tradingview_adapter = getattr(self.state, "tradingview_adapter", None)
        if symbol.strip() and monitors is not None and tradingview_adapter is not None:
            # Universal Market Explainer (mission section 4/5): resolve
            # whatever asset name was given -- a ticker Gemini already
            # formed itself (e.g. "XAUUSD"), or any spoken name AssetResolver
            # covers (gold, Nvidia, Nasdaq, bitcoin, ...) -- against this
            # TARS instance's actually-known symbols before ever building an
            # evidence package for it. Never guesses: AMBIGUOUS/NOT_FOUND are
            # reported honestly instead of silently picking one or inventing
            # a ticker.
            from trading.asset_resolver import AssetResolver

            desktop = getattr(self, "desktop", None)
            active_symbol = getattr(desktop, "current_symbol", None) if desktop else None
            resolved = AssetResolver(monitors).resolve(symbol, active_symbol=active_symbol)
            if resolved.outcome == "AMBIGUOUS":
                names = ", ".join(resolved.candidates) or "more than one instrument"
                return {"error": f"'{symbol}' matches more than one instrument ({names}) -- ask which one."}
            if resolved.outcome == "NOT_FOUND":
                return {"error": f"'{symbol}' isn't a tradable instrument TARS recognizes."}
            symbol = resolved.symbol or symbol

            # A named symbol means this is trading analysis: build the one
            # compact MT5+TradingView+Calendar+News evidence package
            # (mission section 8/10) instead of the generic status line --
            # still exactly one Claude call below, never one per source.
            from trading.market_context import build_market_context

            market = await build_market_context(monitors, tradingview_adapter, symbol)
            live_context = (
                "[TARS evidence package -- real, current state; do not restate numbers not listed here:\n"
                f"{market.as_evidence_text()}]\n"
                "Research standard: label every claim OBSERVATION (a fact listed above), HYPOTHESIS (a "
                "reasoned guess), RISK, or MISSING INFORMATION (state plainly what you don't have). Never "
                "assume a chart pattern or indicator (FVG, order block, liquidity sweep, RSI, SMC, ICT, "
                "support/resistance) has automatic predictive edge -- if you cite one, call it a Hypothesis "
                "or Preliminary Evidence, never a Validated Finding, unless the evidence above actually "
                "validates it. Never invent a price, event or headline not in the evidence above.\n"
            )
        else:
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

    async def analyze_market(self, asset: str = "", timeframe: str = "", question: str = "") -> dict:
        """Universal Market Explainer (mission): one call for "what's
        happening with <asset> today?" -- resolves the asset, takes over
        TradingView (explicit analysis request, so this mutation is
        acceptable), verifies every switch, collects one or more fresh
        chart reads, and makes exactly ONE deep-synthesis call. Never for
        a simple quote/status question -- those stay on
        get_market_context/get_mt5_state/get_tradingview_state/
        get_economic_calendar/get_news (the fast path)."""
        orchestrator = getattr(self.state, "market_explainer", None)
        if orchestrator is None:
            return {"error": "The market explainer isn't available (TradingView isn't connected)."}
        query = asset.strip() or self.desktop.current_symbol or ""
        if not query:
            return {"error": "Which instrument? There's no current asset in context yet."}
        result = await orchestrator.analyze(
            query, question=question, timeframe=timeframe.strip() or None,
            active_symbol=self.desktop.current_symbol,
        )
        if result.status == "AMBIGUOUS":
            names = ", ".join(result.candidates) or "more than one instrument"
            return {"status": "AMBIGUOUS", "error": f"'{query}' matches more than one instrument ({names}) -- ask which one."}
        if result.status == "NOT_FOUND":
            return {"status": "NOT_FOUND", "error": f"'{query}' isn't a tradable instrument TARS recognizes."}
        if result.status in ("CHART_UNAVAILABLE", "SYMBOL_NOT_VERIFIED"):
            return {"status": result.status, "error": result.detail or "Couldn't set up the chart for that instrument."}

        # RESOLVED: persist follow-up context so "what about 5 minutes?" /
        # "any news coming?" / "what caused that move?" inherit this asset.
        self.desktop.current_symbol = result.symbol
        self.desktop.current_trading_app = "TradingView"
        if result.timeframes_analyzed:
            self.desktop.current_timeframe = result.timeframes_analyzed[-1]
        self.desktop.last_analysis_at = datetime.now(UTC).isoformat()
        self.desktop.last_chart_observations = [
            {"timeframe": o.timeframe, "summary": o.analysis.market_context} for o in result.observations
        ]
        if result.market_context is not None:
            self.desktop.last_relevant_news = result.market_context.news
        return {
            "status": "DONE", "answer": result.answer[:3000], "symbol": result.symbol,
            "timeframes_analyzed": result.timeframes_analyzed, "timeframes_failed": result.timeframes_failed,
        }

    async def get_today_market_news(self, asset: str = "") -> dict:
        """"What's on the news today" / "any news for X" -- financial/
        market context only (high-impact calendar + real financial
        headlines, both already-existing sources), never a general news
        dump. Asset-aware when named explicitly or already in follow-up
        context."""
        query = asset.strip() or self.desktop.current_symbol or ""
        symbol = None
        if query:
            from trading.asset_resolver import AssetResolver

            monitors = getattr(self.state, "monitors", None)
            resolved = AssetResolver(monitors).resolve(query, active_symbol=self.desktop.current_symbol)
            if resolved.outcome == "AMBIGUOUS":
                names = ", ".join(resolved.candidates) or "more than one instrument"
                return {"status": "AMBIGUOUS", "error": f"'{query}' matches more than one instrument ({names}) -- ask which one."}
            if resolved.outcome == "RESOLVED":
                symbol = resolved.symbol
        calendar = await self.get_economic_calendar(hours_ahead=24)
        news = await self.get_news(symbol=symbol or "")
        self.desktop.last_relevant_events = calendar.get("events", [])
        self.desktop.last_relevant_news = news.get("items", [])
        return {"status": "DONE", "symbol": symbol, "calendar": calendar.get("events", []), "news": news.get("items", [])}

    async def explain_move(self, question: str = "") -> dict:
        """Move/candle explainer: "why did that move?" / "what caused that
        drop?" -- bounded historical event search (persisted
        calendar/news/price/system events via RealtimeEventCore) centered
        on now, escalating +-15min -> +-30min -> +-60min only if the
        tighter window found nothing, never an unbounded search. One
        synthesis call with a causality standard the model must follow
        (DIRECT/STRONG TEMPORAL/PLAUSIBLE/INSUFFICIENT) -- never a bare
        causal claim the evidence doesn't support."""
        turns = getattr(self.state, "turn_controller", None)
        core = getattr(self.state, "realtime_events", None)
        if turns is None or core is None:
            return {"error": "Event history or the assistant backend isn't available."}
        symbol = self.desktop.current_symbol
        now = datetime.now(UTC)
        events: list[dict] = []
        window_minutes = 15
        for window_minutes in (15, 30, 60):
            events = await core.get_events_between(
                now - timedelta(minutes=window_minutes), now + timedelta(minutes=2), symbol=symbol
            )
            if events:
                break

        evidence_lines = [
            f"Searched {'for ' + symbol + ' ' if symbol else ''}+/-{window_minutes} minutes around now ({now.isoformat()})."
        ]
        if events:
            for row in events:
                ev = row["event"]
                evidence_lines.append(
                    f"- [{ev.get('source')}] {ev.get('title')}: {ev.get('summary')} (recorded {row.get('accepted_at')})"
                )
        else:
            evidence_lines.append("No persisted calendar/news/price/system event found in any searched window.")

        live_context = "[TARS evidence -- bounded historical event search, real persisted events only:\n" + "\n".join(
            evidence_lines
        ) + "]\n"
        causality_standard = (
            "Causality standard: label the evidence DIRECT (explicit source + tight temporal match), "
            "STRONG TEMPORAL (highly relevant event overlaps the move), PLAUSIBLE (relevant event exists but "
            "attribution is uncertain), or INSUFFICIENT (no defensible catalyst -- say so plainly). Never state "
            "a bare causal claim ('X caused Y') unless the evidence genuinely supports it; prefer 'the timing is "
            "consistent with' / 'may have contributed'.\n"
        )
        text = (
            f"{live_context}{causality_standard}{question.strip() or 'What caused that recent move?'} "
            "(Reply for a voice assistant to read aloud: 1-4 short sentences, no markdown, no lists.)"
        )
        answer = ""
        async with asyncio.timeout(60):
            async for event in turns.stream_text(
                text, turn_id=uuid4().hex, conversation_id=f"{self.session_id}:explain-move",
                input_mode=InputMode.voice, speak=False,
            ):
                if event.type == "complete" and event.response:
                    answer = event.response.display_text
                    if event.response.status.value == "failed":
                        return {"error": "Could not work out a cause right now"}
        return {"answer": answer[:2500], "symbol": symbol, "window_minutes": window_minutes, "events_found": len(events)}

    # ---- TARS Watcher Intelligence: watchlist/daily brief (all deterministic, no LLM call) ----

    async def watch_market(self, asset: str = "") -> dict:
        """"Watch gold." / "watch this for me." Resolves the asset (any
        instrument, same AssetResolver as analyze_market) and adds it to
        TARS's persistent background watchlist -- never TradingView itself,
        this never touches the chart."""
        service = getattr(self.state, "market_watch_service", None)
        if service is None:
            return {"error": "Market watching isn't available right now."}
        query = asset.strip() or self.desktop.current_symbol or ""
        if not query:
            return {"error": "Which instrument? There's no current asset in context yet."}
        result = await service.watch(query, active_symbol=self.desktop.current_symbol)
        resolved = result if not isinstance(result, tuple) else result[0]
        if resolved.outcome == "AMBIGUOUS":
            names = ", ".join(resolved.candidates) or "more than one instrument"
            return {"status": "AMBIGUOUS", "error": f"'{query}' matches more than one instrument ({names}) -- ask which one."}
        if resolved.outcome == "NOT_FOUND":
            return {"status": "NOT_FOUND", "error": f"'{query}' isn't a tradable instrument TARS recognizes."}
        _resolved, entry = result
        self.desktop.current_symbol = entry.canonical_symbol
        return {"status": "WATCHING", "symbol": entry.canonical_symbol}

    async def unwatch_market(self, asset: str = "") -> dict:
        """"Stop watching gold." Removes it from the persistent watchlist."""
        service = getattr(self.state, "market_watch_service", None)
        if service is None:
            return {"error": "Market watching isn't available right now."}
        query = asset.strip() or self.desktop.current_symbol or ""
        if not query:
            return {"error": "Which instrument? There's no current asset in context yet."}
        result = await service.unwatch(query, active_symbol=self.desktop.current_symbol)
        resolved = result if not isinstance(result, tuple) else result[0]
        if resolved.outcome == "AMBIGUOUS":
            names = ", ".join(resolved.candidates) or "more than one instrument"
            return {"status": "AMBIGUOUS", "error": f"'{query}' matches more than one instrument ({names}) -- ask which one."}
        if resolved.outcome == "NOT_FOUND":
            return {"status": "NOT_FOUND", "error": f"'{query}' isn't a tradable instrument TARS recognizes."}
        _resolved, removed = result
        return {"status": "STOPPED" if removed else "NOT_WATCHED", "symbol": resolved.symbol}

    async def list_watched_markets(self) -> dict:
        """"What are you watching?" -- the persisted watchlist, and whether
        watching is currently paused."""
        service = getattr(self.state, "market_watch_service", None)
        if service is None:
            return {"watched": [], "detail": "market watching isn't available"}
        status = await service.status()
        return status

    async def pause_market_watching(self) -> dict:
        """"Pause market monitoring." Stops the watcher's own alerting --
        the watchlist itself is kept, nothing is deleted."""
        service = getattr(self.state, "market_watch_service", None)
        if service is None:
            return {"error": "Market watching isn't available right now."}
        await service.pause()
        return {"status": "PAUSED"}

    async def resume_market_watching(self) -> dict:
        """"Resume monitoring."""
        service = getattr(self.state, "market_watch_service", None)
        if service is None:
            return {"error": "Market watching isn't available right now."}
        await service.resume()
        return {"status": "RESUMED"}

    async def get_watcher_status(self) -> dict:
        """Full watcher status: paused/active, watched symbols, and basic
        activity counters."""
        service = getattr(self.state, "market_watch_service", None)
        if service is None:
            return {"available": False}
        return {"available": True, **await service.status()}

    async def get_daily_market_brief(self) -> dict:
        """"Give me today's market brief." / "Give me today's briefing
        again." Every call through this voice tool is an explicit request,
        so it always regenerates regardless of whether one was already
        auto-sent today (mission: an explicit request must always work
        regardless of the once-per-day dedupe); the automatic once-per-day
        brief is a separate, non-voice-tool path (DailyMarketBriefService.
        maybe_generate_on_startup, called once per backend startup)."""
        service = getattr(self.state, "daily_brief_service", None)
        if service is None:
            return {"error": "The daily brief isn't available right now."}
        result = await service.generate(force=True)
        return {"status": result.status, "date": result.date, "brief": result.text,
                "degraded_sources": list(result.degraded_sources)}


class GeminiLiveVoiceSession:
    """Persistent Gemini Live conversation with lazy open and idle close."""

    voice_provider = "GEMINI_LIVE"

    def __init__(self, tools, emit, vad, *, model="gemini-3.8-live", voice=DEFAULT_VOICE, idle_seconds=25.0,
                 connect: Callable[[], Awaitable] | None = None, metrics: LatencyMetrics | None = None,
                 speech_open_frames=8, ack_tts=None):
        self.tools, self.emit, self.vad = tools, emit, vad
        # Local TTS used ONLY for an immediate spoken confirmation of verified native system actions
        # (Gemini's own post-tool reply varies from ~1 s to 5 s+). None = Gemini speaks everything.
        self.ack_tts = ack_tts
        self._ack_task = None
        self._report_task = None
        self._ack_muted = False
        self._ack_gate_until = 0.0
        self._tool_lock = asyncio.Lock()  # FIFO: tool calls in one batch execute in order
        self._tool_seq = 0  # bumps on every tool call; lets the report watchdog notice the model moved on
        # Safety net for the server's end-of-speech detection (observed: turn left open for 50+ s with no
        # tool call): our own VAD ends the turn if Gemini has not started answering.
        self._utt_speech, self._utt_silence, self._awaiting_server, self._forced_end = False, 0.0, False, False
        self.raw: deque[dict] = deque(maxlen=60)  # last server messages (types only, no audio) for diagnostics  # mic is not forwarded to Gemini while our own confirmation plays
        self.model, self.idle_seconds = model, idle_seconds
        self.voice = voice or DEFAULT_VOICE
        self._last_final_user = ("", 0.0)
        # Mission: P0 regression -- "confirmation yes does not resume
        # action." Gemini can invoke confirm_pending_action as a function
        # call in the SAME turn that transcribed the user's "yes," before
        # _finalize_user() has run (that only fires once the assistant's
        # own reply starts or the turn completes, either of which can
        # happen AFTER the tool call). Relying on `_last_final_user` alone
        # made confirm_pending_action wrongly conclude "not answered yet"
        # even though the live transcript already said "yes." This tracks
        # the best-available transcript -- finalized or still in progress,
        # whichever is more recent -- on the SAME clock (time.monotonic())
        # DesktopTools.pending["at"] already uses, so the two are directly
        # comparable.
        self._last_partial_user = ("", 0.0)
        if hasattr(tools, "bind_last_user"):
            tools.bind_last_user(self._best_last_user)
        self._connect = connect
        self.metrics = metrics or LatencyMetrics()
        self.speech_open_frames = speech_open_frames
        self.session_id = uuid4().hex
        self.generation, self.turn_id = 0, f"{self.session_id}:0"
        self.state = VoiceState.IDLE
        self.closed, self.seq = False, 0
        self.history: deque[dict] = deque(maxlen=300)
        self.stt = SimpleNamespace(active=False, name="gemini_live")
        self.provider_status = {"microphone": "STARTING", "gemini_live": "IDLE"}
        self._started_at = time.monotonic()
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
        # Microphone truth: what actually arrives here (levels/counters only, never audio content).
        self.mic = {"frames": 0, "db": -120.0, "max_db": -120.0, "vad": False, "speech_frames": 0,
                    "sent_to_gemini": 0, "last_frame_at": 0.0, "first_frame_at": 0.0, "last_signal_at": 0.0,
                    "status": "DISCONNECTED"}
        self.last_transcript = ""
        self._mic_logged = 0.0
        self.microphone_muted = False

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
        detail.setdefault("microphone_muted", self.microphone_muted)
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
            # Pin English: with no hint Gemini transcribed clear English commands as Portuguese.
            input_audio_transcription=types.AudioTranscriptionConfig(language_codes=["en-US"]),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            # Commit the turn quickly after the user stops: short commands should not wait out the
            # server's default silence window.
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,
                    silence_duration_ms=END_SILENCE_MS)),
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
            health = self.mic_health()
            if health != self.provider_status["microphone"]:
                self.provider_status["microphone"] = health
                await self._status(detail=f"Microphone {health.lower()}")
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
        if self.closed or self.microphone_muted:
            return  # muted: no VAD, no accounting, no Gemini, no session opening
        if self.provider_status["microphone"] in ("DISCONNECTED", "STARTING"):
            self.provider_status["microphone"] = "CONNECTED"  # frames are arriving; health() refines it
            await self._status()
            await self.wake()  # first live (unmuted) audio: connect now so the first command is not clipped
        self._pending.extend(frame)
        while len(self._pending) >= 1024:
            chunk = bytes(self._pending[:1024])
            del self._pending[:1024]
            speech = self.vad(chunk)
            self._account(chunk, speech)
            if speech:
                self._last_activity = time.monotonic()
                self._last_speech_pc = time.perf_counter()
            if self._live:
                # Speakers: the mic hears TARS's own local confirmation, and Gemini's server would treat
                # that as the user talking over it and interrupt (cutting the confirmation off).
                if time.monotonic() >= self._ack_gate_until:
                    await self._send_audio(chunk)
                    await self._watch_end_of_turn(speech)
                continue
            self._preroll.append(chunk)
            self._speech_frames = self._speech_frames + 1 if speech else 0
            if self._speech_frames >= self.speech_open_frames and not (self._open_task and not self._open_task.done()):
                self._speech_frames = 0
                self._open_task = asyncio.create_task(self._open())

    def _account(self, chunk: bytes, speech: bool):
        import numpy as np

        now = time.monotonic()
        samples = np.frombuffer(chunk, dtype="<i2").astype("float32") / 32768.0
        rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0
        db = 20 * np.log10(rms) if rms > 1e-6 else -120.0
        m = self.mic
        m["frames"] += 1
        m["db"] = float(db if db > m["db"] else m["db"] * 0.9 + db * 0.1)
        m["max_db"] = max(m["max_db"], float(db))
        m["vad"] = bool(speech)
        m["speech_frames"] += 1 if speech else 0
        m["last_frame_at"] = now
        m["first_frame_at"] = m["first_frame_at"] or now
        if db > MIC_SILENT_DB:
            m["last_signal_at"] = now
        if now - self._mic_logged > 10:
            self._mic_logged = now
            logger.info("mic frames=%s rms=%.1fdB max=%.1fdB vad=%s speech_frames=%s sent_to_gemini=%s gemini=%s",
                        m["frames"], m["db"], m["max_db"], m["vad"], m["speech_frames"], m["sent_to_gemini"],
                        self.provider_status["gemini_live"])

    def mic_health(self) -> str:
        """CONNECTED | SILENT (frames flow but never any signal) | DISCONNECTED (no frames)."""
        m, now = self.mic, time.monotonic()
        if self.microphone_muted:
            return "MUTED"
        if m["frames"] == 0:
            return "STARTING" if now - self._started_at < 6.0 else "DISCONNECTED"
        if now - m["last_frame_at"] > 3.0:
            return "DISCONNECTED"
        if m["max_db"] <= MIC_SILENT_DB and now - m["first_frame_at"] > MIC_SILENT_AFTER_S:
            return "SILENT"
        return "CONNECTED"

    def diag(self) -> dict:
        m = self.mic
        return {"mic": {**{k: (round(v, 1) if isinstance(v, float) else v) for k, v in m.items()
                           if k not in ("last_frame_at", "first_frame_at", "last_signal_at")},
                        "health": self.mic_health()},
                "gemini": self.provider_status["gemini_live"], "transcript": self.last_transcript,
                "voice_state": self.state.value, "voice_provider": self.voice_provider,
                "microphone_muted": self.microphone_muted, "raw": list(self.raw)}

    async def _watch_end_of_turn(self, speech: bool):
        if speech:
            self._utt_speech, self._utt_silence, self._forced_end = True, 0.0, False
            self._awaiting_server = True
            return
        if not self._utt_speech:
            return
        self._utt_silence += 0.032
        if self._utt_silence >= FORCE_END_SILENCE_S and self._awaiting_server and not self._forced_end:
            self._forced_end, self._utt_speech = True, False
            live = self._live
            if live is None:
                return
            try:
                await live.send_realtime_input(audio_stream_end=True)
                self.metrics.latest["forced_end_of_turn"] = self.metrics.latest.get("forced_end_of_turn", 0) + 1
                logger.info("gemini live: server did not end the user turn after %.1fs of silence; forced it",
                            self._utt_silence)
            except Exception as exc:
                logger.warning("forced end of turn failed: %s", type(exc).__name__)

    async def _send_audio(self, chunk: bytes):
        live = self._live
        if live is None:
            return
        from google.genai import types
        try:
            await live.send_realtime_input(audio=types.Blob(data=chunk, mime_type=f"audio/pcm;rate={INPUT_RATE}"))
            self.mic["sent_to_gemini"] += 1
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
        self.raw.append({"t": round(time.time(), 2), "tool_call": bool(getattr(message, "tool_call", None)),
                         "go_away": bool(getattr(message, "go_away", None)),
                         **({k: bool(getattr(content, k, None)) for k in (
                             "turn_complete", "generation_complete", "interrupted", "input_transcription",
                             "output_transcription", "model_turn")} if content else {})})
        if getattr(message, "tool_call", None) or (content and (getattr(content, "model_turn", None)
                                                                or getattr(content, "turn_complete", None))):
            self._awaiting_server = False
        if getattr(message, "tool_call", None):
            if self._ack_muted:
                self._muted, self._ack_muted = False, False  # model continues the task: let it report
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
        self.last_transcript = self._user_text.strip()
        self._last_user_at = time.perf_counter()
        self._last_partial_user = (resolve_trading_terms(self.last_transcript), time.monotonic())
        await self.send("partial_transcript", text=resolve_trading_terms(self._user_text.strip()))

    def _best_last_user(self) -> tuple[str, float]:
        """See __init__'s comment on `_last_partial_user`: whichever of the
        finalized or still-in-progress transcript is more recent."""
        if self._last_partial_user[1] > self._last_final_user[1] and self._last_partial_user[0]:
            return self._last_partial_user
        return self._last_final_user

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
            last_speech, done = getattr(self, "_last_speech_pc", None), getattr(self, "_tool_result_pc", None)
            if last_speech:
                self.metrics.latest["speech_end_to_first_audio_ms"] = round((time.perf_counter() - last_speech) * 1000)
            if done:
                self.metrics.latest["tool_result_to_first_audio_ms"] = round((time.perf_counter() - done) * 1000)
                self._tool_result_pc = None
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
        self._ack_muted = False
        if text:
            await self.send("response_complete", response={
                "display_text": text, "speech_text": text, "status": "completed",
                "provider": "gemini_live", "turn_id": self.turn_id})
        await self.transition(VoiceState.LISTENING)

    # ---- tools --------------------------------------------------------------
    async def _run_tool(self, call):
        from google.genai import types
        from voice.activity import describe_tool_call

        name, args = call.name, dict(call.args or {})
        # Gemini batches dependent calls ("open Start" + "scroll down") in one message. They must run in the
        # order given, never in parallel: the scroll raced ahead of Start opening and hit the wrong window.
        async with self._tool_lock:
            return await self._run_tool_locked(call, name, args)

    async def _run_tool_locked(self, call, name, args):
        from google.genai import types
        from voice.activity import describe_tool_call

        self._tool_seq += 1
        last_speech = getattr(self, "_last_speech_pc", None)
        if last_speech:  # local-VAD end of the user's speech -> Gemini's tool call (model + end-of-turn wait)
            self.metrics.latest["speech_end_to_tool_call_ms"] = round((time.perf_counter() - last_speech) * 1000)
        # The displayed text is derived from the real call about to run, not
        # invented by Gemini -- see voice/activity.py's module docstring.
        await self.send("tool_call", name=name, text=describe_tool_call(name, args))
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
        # UI-only signals (orb state, confirmation card). They never influence Gemini or the tools.
        from voice.activity import describe_tool_result

        status = result.get("status") if isinstance(result, dict) else None
        final_status = status or ("FAILED" if "error" in result else "DONE")
        self._tool_result_pc = time.perf_counter()
        await self.send("tool_result", name=name, status=final_status, text=describe_tool_result(name, args, final_status))
        desktop = getattr(self.tools, "desktop", None)
        if status == "NEEDS_CONFIRMATION" and desktop is not None and desktop.pending:
            await self.send("confirmation_pending", text=desktop.pending["describe"])
        elif name in ("confirm_pending_action", "cancel_pending_action"):
            await self.send("confirmation_cleared")
        self._last_activity = time.monotonic()
        live = self._live
        if live is None:
            return
        if (self.ack_tts is not None and name == "system_control" and final_status == "DONE"
                and isinstance(result, dict) and result.get("summary")
                and not (result.get("data") or {}).get("dispatched")):
            # Verified native action: confirm it aloud NOW (local TTS) and hold back Gemini's slower,
            # redundant follow-up until it either calls another tool or ends the turn.
            self._muted = self._ack_muted = True
            self._ack_task = asyncio.create_task(self._speak_ack(str(result["summary"]), self.generation))
        try:
            await live.send_tool_response(function_responses=[
                types.FunctionResponse(id=call.id, name=name, response=result)])
        except Exception as exc:
            logger.warning("send_tool_response failed: %s", type(exc).__name__)
            return
        if self.ack_tts is not None and name in REPORT_TOOLS and not self._ack_muted and isinstance(result, dict):
            self._report_task = asyncio.create_task(
                self._report_watchdog(result, self.generation, self._tool_seq))

    async def _report_watchdog(self, result: dict, generation: int, tool_seq: int):
        """Gemini sometimes finishes a tool and never reports back. If it has not begun speaking (and has
        not moved on to another tool) shortly after the tool finished, say the verified result ourselves."""
        try:
            await asyncio.sleep(REPORT_WAIT_S)
            if (generation != self.generation or self.closed or tool_seq != self._tool_seq
                    or self._first_audio or self._ack_muted or self.state is VoiceState.ASSISTANT_SPEAKING
                    or self.state is VoiceState.USER_SPEAKING):
                return
            status = result.get("status")
            if status == "NEEDS_CONFIRMATION":
                text = "That needs your confirmation. Say yes or no."
            else:
                text = str(result.get("summary") or result.get("error") or "").strip()
                if status not in (None, "DONE") and text:
                    text = f"That didn't work. {text}"
            if not text:
                return
            self.metrics.latest["report_watchdog_fired"] = self.metrics.latest.get("report_watchdog_fired", 0) + 1
            self._muted = self._ack_muted = True  # drop a late duplicate from the model
            await self._speak_ack(text, generation)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("report watchdog failed: %s", type(exc).__name__)

    async def _speak_ack(self, text: str, generation: int):
        try:
            started = time.perf_counter()
            result = await asyncio.wait_for(self.ack_tts.synthesize(text), 8)
            if generation != self.generation or self.closed:
                return
            from voice.audio_utils import wav_to_pcm16

            pcm, rate = wav_to_pcm16(result.audio)
            self.metrics.latest["local_ack_synth_ms"] = round((time.perf_counter() - started) * 1000)
            self._first_audio = True
            self._ack_gate_until = time.monotonic() + len(pcm) / (2 * rate) + ACK_ECHO_TAIL_S
            await self.transition(VoiceState.ASSISTANT_SPEAKING)
            await self.send("audio_pcm", sample_rate=rate, audio=base64.b64encode(pcm).decode("ascii"))
        except Exception as exc:  # the action already ran; if the local voice fails Gemini still speaks
            logger.warning("local ack failed: %s", type(exc).__name__)
            self._muted = self._ack_muted = False

    # ---- controls used by the router / UI ------------------------------------
    async def interrupt(self):
        """UI-initiated stop: drop audio locally now; the model's stale audio is muted until it ends."""
        previous = self.turn_id
        self.generation += 1
        self.turn_id = f"{self.session_id}:{self.generation}"
        self._muted = self._first_audio or self.state is VoiceState.ASSISTANT_SPEAKING
        self._first_audio, self._assistant_text = False, ""
        await self.send("interrupt", previous_turn_id=previous, status="interrupted")

    async def set_muted(self, muted: bool):
        """Mirror of the native mute (the authority). Muting drops any half-heard utterance cleanly."""
        muted = bool(muted)
        if muted == self.microphone_muted:
            return
        self.microphone_muted = muted
        if muted:
            mid_utterance = self._user_open or self.stt.active or self._speech_frames > 0
            self._pending.clear()
            self._preroll.clear()
            self._speech_frames = 0
            self._user_open, self._user_text = False, ""
            self.stt.active = False
            if self._open_task and not self._open_task.done():
                self._open_task.cancel()
                await asyncio.gather(self._open_task, return_exceptions=True)
            if self._live is not None and mid_utterance:
                # Closing the session is the only way to guarantee the unfinished utterance is not answered.
                await self._teardown_live("muted mid-utterance")
            self.provider_status["microphone"] = "MUTED"
            if self.state in (VoiceState.USER_SPEAKING, VoiceState.ENDPOINTING):
                await self.transition(VoiceState.LISTENING)
        else:
            self.provider_status["microphone"] = "STARTING"  # health() takes over once frames arrive
            # Not 0.0: with frames already > 0 from before the mute, a zero timestamp reads as
            # "last frame 40+ years ago" and mic_health() would report DISCONNECTED until the next
            # frame overwrites it -- a false alarm for what is otherwise an instant, clean resume.
            self.mic["last_frame_at"] = time.monotonic()
            self.mic["first_frame_at"] = time.monotonic()
        await self._status(detail="Microphone muted" if muted else "Microphone active", microphone_muted=muted)
        if not muted:
            await self.wake()  # unmuting = ready: connect before the user speaks, not after

    async def wake(self):
        """Orb click: open the Gemini session now instead of waiting for speech (no-op if already open)."""
        if self.microphone_muted:
            return
        self._last_activity = time.monotonic()
        if self._live is None and not (self._open_task and not self._open_task.done()) and not self.closed:
            self._open_task = asyncio.create_task(self._open())

    async def ui_confirm(self, approve: bool):
        """The user clicked Yes/No on the orb's confirmation card. Still executes through ActionRuntime."""
        desktop = getattr(self.tools, "desktop", None)
        if desktop is None or not desktop.pending:
            return
        result = await desktop.ui_confirm(approve)
        await self.send("tool_result", name="confirm_pending_action", status=result.get("status"))
        await self.send("confirmation_cleared")

    async def playback(self, message: dict):
        return None  # Gemini output plays client-side without acks; nothing to reconcile

    async def speak_alert(self, text: str):
        if self.closed or not self._live or self.state is not VoiceState.LISTENING:
            return
        with contextlib.suppress(Exception):
            await self._live.send_realtime_input(text=f"Briefly tell the user this alert: {text}")
