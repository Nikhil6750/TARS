"""Deterministic, truthful activity-text formatting for the orb/pill UI.

`describe_tool_call(name, args)` turns a tool TARS is actually about to run
into one short natural-language line ("Opening Chrome…") -- never invented
by Gemini, never a technical name like `browser_click`/`UIAutomationControl`
leaking to the user. Called once, at the moment `voice/gemini_live.py`'s
`_run_tool` dispatches the call, so the text shown is always exactly what
is really executing (mission: "activity text must be driven by actual
execution state").
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

_SEARCH_WORDS = re.compile(r"search|find box|query", re.IGNORECASE)
_RESULT_WORDS = re.compile(r"result|link|item", re.IGNORECASE)
_VIDEO_WORDS = re.compile(r"video|watch", re.IGNORECASE)
_DOWNLOAD_WORDS = re.compile(r"download|\.(pdf|zip|docx?|xlsx?|csv|png|jpg|jpeg)\b", re.IGNORECASE)


def _site_name(url: str) -> str:
    host = urlsplit(url).netloc or url
    host = host.split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    base = host.split(".")[0] if host else url
    known = {"youtube": "YouTube", "google": "Google", "github": "GitHub", "fastapi": "FastAPI"}
    return known.get(base.lower(), base.capitalize() or url)


def _basename(path: str) -> str:
    return (path.rstrip("/\\").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]) or path


def describe_tool_call(name: str, args: dict) -> str:
    target = str(args.get("target") or "").strip()
    url = str(args.get("url") or "").strip()
    text = str(args.get("text") or "").strip()
    path = str(args.get("path") or "").strip()

    if name == "desktop_open_app":
        return f"Opening {target or 'the app'}…"
    if name == "desktop_focus_window":
        return f"Switching to {target or 'the app'}…"
    if name == "desktop_close_app":
        return f"Closing {target or 'the app'}…"
    if name in ("desktop_list_controls", "desktop_scroll"):
        return "Looking at the screen…"
    if name == "desktop_click_control":
        label = str(args.get("label") or "").strip()
        return f"Clicking {label or 'that'}…"
    if name == "desktop_type_text":
        label = str(args.get("label") or "").strip()
        return f"Typing into {label or 'the field'}…"

    if name in ("web_navigate", "browser_open_url"):
        return f"Opening {_site_name(url) if url else 'the page'}…"
    if name == "browser_search":
        query = str(args.get("query") or "").strip()
        return f"Searching for {query}…" if query else "Searching the web…"
    if name == "web_type":
        if text and (bool(args.get("submit")) or _SEARCH_WORDS.search(target)):
            return f"Searching for {text}…"
        return f"Typing '{text}' into {target or 'the page'}…" if text else f"Typing into {target or 'the page'}…"
    if name == "web_click":
        if target and _VIDEO_WORDS.search(target):
            return "Playing a video…"
        if target and _RESULT_WORDS.search(target):
            return f"Opening {target}…"
        return f"Clicking {target or 'that'}…"
    if name == "web_download":
        return f"Downloading {target}…" if target else "Downloading the file…"
    if name in ("web_back", "web_forward"):
        return "Going back…" if name == "web_back" else "Going forward…"
    if name == "web_new_tab":
        return "Opening a new tab…"
    if name == "web_close_tab":
        return "Closing the tab…"
    if name == "web_focus_tab":
        return f"Switching to tab '{target}'…" if target else "Switching tabs…"
    if name in ("web_get_context", "web_list_tabs", "web_find", "web_extract_text", "web_extract_table", "web_get_links"):
        return "Reading the page…"
    if name == "web_wait_for":
        return f"Waiting for {target or 'the page to update'}…"
    if name == "web_get_last_download":
        return "Checking the last download…"
    if name == "web_select":
        return f"Selecting '{args.get('value', '')}' in {target or 'the page'}…"
    if name == "web_scroll":
        return "Scrolling the page…"

    if name == "files_list":
        return f"Looking in {_basename(path) or 'your files'}…"
    if name == "files_read_open":
        return f"Opening {_basename(path) or 'the file'}…"

    if name == "run_terminal":
        return "Running a command…"
    if name == "analyze_chart":
        return "Analyzing the chart…"
    if name == "ask_claude":
        return "Thinking it through…"
    if name in ("tradingview_set_symbol",):
        return f"Switching chart to {args.get('symbol', '')}…"
    if name == "tradingview_set_timeframe":
        return f"Setting the timeframe to {args.get('timeframe', '')}…"
    if name == "tradingview_status":
        return "Checking the chart…"
    if name in ("confirm_pending_action", "cancel_pending_action"):
        return "Confirming…" if name == "confirm_pending_action" else "Cancelling…"

    # Honest, generic fallback for anything not explicitly mapped above --
    # never a raw tool/skill identifier, but also never fabricating detail
    # the call doesn't have.
    pretty = name.replace("_", " ").strip()
    return f"{pretty[:1].upper()}{pretty[1:]}…" if pretty else "Working…"


def describe_tool_result(name: str, args: dict, status: str) -> str:
    """The brief DONE/ERROR line the pill shows for ~1.5-2s before collapsing
    (mission: "✓ Done" / "! Couldn't find Calculator"). Uses only the real
    target already present in this call's own arguments -- never a made-up
    reason."""
    if status == "DONE":
        return "Done"
    target = str(args.get("target") or args.get("url") or args.get("query") or "").strip()
    if status == "NOT_FOUND":
        return f"Couldn't find {target}" if target else "Couldn't find that"
    if status == "AMBIGUOUS":
        return f"'{target}' was ambiguous" if target else "That was ambiguous"
    if status == "BLOCKED":
        return "That's not allowed"
    if status == "NEEDS_CONFIRMATION":
        return "Waiting for confirmation"
    return f"Couldn't {('open ' + target) if target else 'do that'}"
