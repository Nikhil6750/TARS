"""WindowsAppResolver -- deterministic, multi-source discovery and resolution of installed
Windows applications.

Gemini (or any caller) determines INTENT ("open clock"); this module determines HOW
("Microsoft.WindowsAlarms_8wekyb3d8bbwe!App", launched via shell:appsFolder). Nothing here ever
falls back to a web search or a browser -- see `windows_app.py` for how `resolve`/`launch`/`focus`/
`list_installed` plug this into the existing `windows_app` skill.

Discovery sources, in priority order:
  A. currently running processes/visible windows (`_running_app_records`, reuses
     `windows_app._enum_visible_windows`)
  B. `Get-StartApps` (PowerShell) -- covers BOTH packaged (MSIX/UWP) apps like Clock, Calculator,
     Settings, Microsoft Store, Photos AND classic Start-Menu-registered win32 apps
  C. Start Menu .lnk shortcuts (user + system), resolved via the WScript.Shell COM shortcut API --
     used to enrich a Get-StartApps record with a real absolute .exe path where one exists
  D. Windows "App Paths" registry (HKCU/HKLM ...\\CurrentVersion\\App Paths)
  E. PATH (shutil.which) -- tried at resolve() time for bare command names, not bulk-discovered
  F. explicit aliases (spoken name -> canonical search term), see ALIASES below

Every AppRecord carries enough to launch it correctly without guessing:
  - launch_method == "app_id"   -> explorer.exe shell:appsFolder\\<app_user_model_id>
    (the standard, documented activation path for packaged apps; it also works for many classic
    Start-Menu shortcut AppIDs, since the shell's Apps virtual folder indexes both by the same id)
  - launch_method == "exe"      -> subprocess.Popen([executable])   (real absolute path known)
  - launch_method == "shortcut" -> os.startfile(shortcut)           (.lnk only, no resolved target)
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
import winreg
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

logger = logging.getLogger("tars.app_resolver")

AppType = Literal["WIN32", "MSIX", "UWP", "SYSTEM", "BROWSER", "WEB_APP"]
LaunchMethod = Literal["app_id", "exe", "shortcut"]

_START_MENU_DIRS = [
    Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
    Path(os.environ.get("PROGRAMDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
]
_APP_PATHS_KEYS = [
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
    (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
]

# Item 5: explicit aliases. Every alias phrase maps to the canonical query used to search the
# discovered registry -- these point at REAL discovered applications, never at a hardcoded path.
ALIASES: dict[str, str] = {}
for _canonical, _phrases in {
    "tradingview": ("tradingview", "trading view"),
    "metatrader 5": ("mt5", "metatrader", "meta trader", "metatrader 5"),
    "visual studio code": ("vscode", "vs code", "visual studio code", "code"),
    "windows terminal": ("terminal", "windows terminal"),
    "clock": ("clock", "windows clock", "alarms", "alarms and clock", "alarms & clock"),
    "calculator": ("calculator", "calc"),
    "file explorer": ("explorer", "file explorer", "files"),
}.items():
    for _phrase in _phrases:
        ALIASES[_phrase] = _canonical


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


@dataclass
class AppRecord:
    id: str
    display_name: str
    aliases: tuple[str, ...] = ()
    app_type: AppType = "WIN32"
    executable: str | None = None
    app_user_model_id: str | None = None
    process_names: tuple[str, ...] = ()
    shortcut: str | None = None
    launch_method: LaunchMethod = "exe"
    confidence: float = 1.0
    source: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id, "display_name": self.display_name, "aliases": list(self.aliases),
            "app_type": self.app_type, "executable": self.executable,
            "app_user_model_id": self.app_user_model_id, "process_names": list(self.process_names),
            "shortcut": self.shortcut, "launch_method": self.launch_method,
            "confidence": self.confidence, "source": self.source,
        }


@dataclass
class ResolveResult:
    outcome: Literal["MATCH", "AMBIGUOUS", "NOT_FOUND"]
    app: AppRecord | None = None
    candidates: tuple[AppRecord, ...] = ()


def _run_powershell_json(command: str, timeout: float = 10.0) -> list[dict] | dict | None:
    try:
        proc = subprocess.run(  # noqa: S603
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("[app_resolver] powershell discovery failed: %s", exc)
        return None
    out = proc.stdout.strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        logger.warning("[app_resolver] could not parse Get-StartApps JSON output")
        return None


class WindowsAppResolver:
    """Multi-source installed-app discovery with an in-memory, TTL'd cache. `resolve()` never
    consults the network or a browser -- a miss is reported as NOT_FOUND, not silently escalated."""

    def __init__(self, cache_ttl: float = 600.0) -> None:
        self._records: dict[str, AppRecord] = {}
        self._cached_at: float = 0.0
        self._ttl = cache_ttl

    # ---- discovery ----------------------------------------------------------------------------
    def discover(self, force: bool = False) -> dict[str, AppRecord]:
        now = time.monotonic()
        if not force and self._records and now - self._cached_at < self._ttl:
            return self._records
        records: dict[str, AppRecord] = {}
        try:
            self._discover_start_apps(records)
        except Exception as exc:
            logger.warning("[app_resolver] Get-StartApps discovery failed: %s", exc)
        try:
            self._discover_start_menu_shortcuts(records)
        except Exception as exc:
            logger.warning("[app_resolver] Start Menu shortcut discovery failed: %s", exc)
        try:
            self._discover_app_paths_registry(records)
        except Exception as exc:
            logger.warning("[app_resolver] App Paths registry discovery failed: %s", exc)
        self._records = records
        self._cached_at = now
        logger.info("[app_resolver] discovered %d installed application(s)", len(records))
        return records

    def _discover_start_apps(self, records: dict[str, AppRecord]) -> None:
        raw = _run_powershell_json("Get-StartApps | ConvertTo-Json -Compress -Depth 3")
        if raw is None:
            return
        entries = raw if isinstance(raw, list) else [raw]
        for entry in entries:
            name = str(entry.get("Name") or "").strip()
            app_id = str(entry.get("AppID") or "").strip()
            if not name or not app_id:
                continue
            key = _normalize(name)
            if not key or key in records:
                continue
            if "!" in app_id:
                records[key] = AppRecord(
                    id=key, display_name=name, app_type="MSIX",
                    app_user_model_id=app_id, launch_method="app_id", source="start_apps",
                )
            elif app_id.lower().endswith(".exe") and Path(app_id).is_file():
                records[key] = AppRecord(
                    id=key, display_name=name, app_type="WIN32", executable=app_id,
                    process_names=(Path(app_id).name,), launch_method="exe", source="start_apps",
                )
            else:
                # Opaque classic AppID (e.g. "Chrome", "Microsoft.Windows.Explorer",
                # "Microsoft.VisualStudioCode") -- still launchable through the same
                # shell:appsFolder activation path the Start Menu itself uses for it.
                records[key] = AppRecord(
                    id=key, display_name=name, app_type="WIN32",
                    app_user_model_id=app_id, launch_method="app_id", source="start_apps",
                )

    def _discover_start_menu_shortcuts(self, records: dict[str, AppRecord]) -> None:
        dirs = [d for d in _START_MENU_DIRS if d.is_dir()]
        if not dirs:
            return
        shell = None
        for base in dirs:
            for lnk in base.rglob("*.lnk"):
                name = lnk.stem
                key = _normalize(name)
                if not key:
                    continue
                existing = records.get(key)
                if existing and (existing.executable or existing.app_type in ("MSIX", "UWP")):
                    # Already has a real launch target, or is a packaged app that must always
                    # activate via its AUMID -- a shortcut's Targetpath for a packaged app can
                    # point at the underlying package executable, which is not reliably launchable
                    # by directly spawning it outside package activation. Never downgrade to that.
                    continue
                try:
                    if shell is None:
                        import win32com.client

                        shell = win32com.client.Dispatch("WScript.Shell")
                    target = shell.CreateShortcut(str(lnk)).Targetpath
                except Exception:
                    continue
                if not target or not target.lower().endswith(".exe") or not Path(target).is_file():
                    continue
                process_name = Path(target).name
                if existing:
                    existing.executable = target
                    existing.launch_method = "exe"
                    existing.process_names = tuple({*existing.process_names, process_name})
                    existing.shortcut = str(lnk)
                else:
                    records[key] = AppRecord(
                        id=key, display_name=name, app_type="WIN32", executable=target,
                        process_names=(process_name,), shortcut=str(lnk),
                        launch_method="exe", source="start_menu_shortcut",
                    )

    def _discover_app_paths_registry(self, records: dict[str, AppRecord]) -> None:
        for hive, subkey in _APP_PATHS_KEYS:
            try:
                key = winreg.OpenKey(hive, subkey)
            except OSError:
                continue
            with key:
                for i in range(winreg.QueryInfoKey(key)[0]):
                    try:
                        exe_name = winreg.EnumKey(key, i)
                    except OSError:
                        continue
                    try:
                        with winreg.OpenKey(key, exe_name) as sub:
                            path = winreg.QueryValueEx(sub, "")[0]
                    except OSError:
                        continue
                    if not path or not Path(path).is_file():
                        continue
                    display = Path(exe_name).stem
                    match_key = _normalize(display)
                    existing = records.get(match_key)
                    if existing:
                        if not existing.executable and existing.app_type not in ("MSIX", "UWP"):
                            existing.executable = path
                            existing.launch_method = "exe"
                        existing.process_names = tuple({*existing.process_names, exe_name})
                    else:
                        records[match_key] = AppRecord(
                            id=match_key, display_name=display, app_type="WIN32", executable=path,
                            process_names=(exe_name,), launch_method="exe", source="app_paths_registry",
                        )

    # ---- resolution -----------------------------------------------------------------------------
    def resolve(self, spoken: str) -> ResolveResult:
        query = _normalize(spoken)
        if not query:
            return ResolveResult(outcome="NOT_FOUND")
        records = self.discover()

        canonical = ALIASES.get(query)
        search_terms = [t for t in (canonical, query) if t]

        # Exact normalized-name match (covers both alias->canonical and a direct exact name).
        for term in search_terms:
            hit = records.get(_normalize(term))
            if hit:
                return ResolveResult(outcome="MATCH", app=hit)

        # Substring match against display names (both directions -- "clock" in "windows clock" and
        # "vs code" containing enough of "visual studio code").
        candidates: list[AppRecord] = []
        for term in search_terms:
            term_n = _normalize(term)
            for rec in records.values():
                name_n = _normalize(rec.display_name)
                if term_n in name_n or name_n in term_n:
                    candidates.append(rec)
            if candidates:
                break
        candidates = list({c.id: c for c in candidates}.values())

        if len(candidates) == 1:
            return ResolveResult(outcome="MATCH", app=candidates[0])
        if len(candidates) > 1:
            return ResolveResult(outcome="AMBIGUOUS", candidates=tuple(candidates[:6]))

        # Last resort: a bare command already on PATH (e.g. a portable tool with no Start Menu
        # entry). Still not a web search -- either it resolves to a real local executable or it
        # doesn't resolve at all.
        if re.fullmatch(r"[a-z0-9_.-]+", query.replace(" ", "")):
            resolved = shutil.which(query.replace(" ", ""))
            if resolved:
                return ResolveResult(outcome="MATCH", app=AppRecord(
                    id=query, display_name=Path(resolved).stem, app_type="WIN32",
                    executable=resolved, process_names=(Path(resolved).name,),
                    launch_method="exe", confidence=0.6, source="path",
                ))

        return ResolveResult(outcome="NOT_FOUND")


_resolver: WindowsAppResolver | None = None


def get_resolver() -> WindowsAppResolver:
    global _resolver
    if _resolver is None:
        _resolver = WindowsAppResolver()
    return _resolver


# ---- ApplicationAdapter boundary (item 13: establish the interface only) -----------------------
# Deep per-app integrations (TradingView, MT5, ...) are future work; this is only the seam they
# will plug into, so `windows_app.py`/the resolver never need to change shape when they arrive.
class ApplicationAdapter:
    """Interface boundary for a deeper, app-specific control adapter. Not implemented for any
    application yet -- generic launch/focus/UIA control (windows_app.py, desktop_control.py)
    covers every app today. A future TradingViewAdapter/MT5Adapter would implement this to add
    app-specific semantics (e.g. "set symbol", "set timeframe") on top of that generic layer."""

    def discover(self) -> AppRecord | None:  # pragma: no cover - boundary only
        raise NotImplementedError

    def status(self) -> dict:  # pragma: no cover - boundary only
        raise NotImplementedError

    def focus(self) -> bool:  # pragma: no cover - boundary only
        raise NotImplementedError

    def inspect(self) -> dict:  # pragma: no cover - boundary only
        raise NotImplementedError

    def perform(self, action: str, **kwargs) -> dict:  # pragma: no cover - boundary only
        raise NotImplementedError

    def verify(self, action: str, **kwargs) -> bool:  # pragma: no cover - boundary only
        raise NotImplementedError
