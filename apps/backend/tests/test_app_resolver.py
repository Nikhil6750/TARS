from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

from skills.app_resolver import ALIASES, AppRecord, WindowsAppResolver, _normalize

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="winreg/win32com are Windows-only")


def _resolver_with(records: dict[str, AppRecord]) -> WindowsAppResolver:
    r = WindowsAppResolver()
    r._records = records
    r._cached_at = __import__("time").monotonic()
    return r


# ---- aliases (item 5) -------------------------------------------------------------------------


@pytest.mark.parametrize("phrase,canonical", [
    ("tradingview", "tradingview"), ("trading view", "tradingview"),
    ("mt5", "metatrader 5"), ("metatrader", "metatrader 5"), ("meta trader", "metatrader 5"),
    ("metatrader 5", "metatrader 5"),
    ("vscode", "visual studio code"), ("vs code", "visual studio code"),
    ("visual studio code", "visual studio code"), ("code", "visual studio code"),
    ("terminal", "windows terminal"), ("windows terminal", "windows terminal"),
    ("clock", "clock"), ("windows clock", "clock"), ("alarms", "clock"), ("alarms and clock", "clock"),
    ("calculator", "calculator"), ("calc", "calculator"),
    ("explorer", "file explorer"), ("file explorer", "file explorer"), ("files", "file explorer"),
])
def test_every_required_alias_maps_to_its_canonical_app(phrase, canonical):
    assert ALIASES[phrase] == canonical


def test_alias_resolves_to_the_discovered_app_not_a_hardcoded_path():
    app = AppRecord(id="metatrader 5", display_name="MetaTrader 5", app_type="WIN32",
                    executable="C:\\Program Files\\MetaTrader 5\\terminal64.exe",
                    process_names=("terminal64.exe",), launch_method="exe")
    resolver = _resolver_with({"metatrader 5": app})
    for phrase in ("mt5", "metatrader", "meta trader", "metatrader 5"):
        result = resolver.resolve(phrase)
        assert result.outcome == "MATCH"
        assert result.app is app


# ---- resolution behavior (item 7) -------------------------------------------------------------


def test_resolve_not_found_when_nothing_matches():
    resolver = _resolver_with({})
    result = resolver.resolve("some app nobody has installed")
    assert result.outcome == "NOT_FOUND"


def test_resolve_ambiguous_when_multiple_candidates_match():
    resolver = _resolver_with({
        "photo editor": AppRecord(id="photo editor", display_name="Photo Editor Pro"),
        "photo viewer": AppRecord(id="photo viewer", display_name="Photo Viewer Lite"),
    })
    result = resolver.resolve("photo")
    assert result.outcome == "AMBIGUOUS"
    assert {c.display_name for c in result.candidates} == {"Photo Editor Pro", "Photo Viewer Lite"}


def test_resolve_exact_display_name_match_is_unambiguous():
    resolver = _resolver_with({
        "calculator": AppRecord(id="calculator", display_name="Calculator"),
    })
    result = resolver.resolve("calculator")
    assert result.outcome == "MATCH"
    assert result.app.display_name == "Calculator"


def test_resolve_never_touches_the_network_or_a_browser():
    """A resolver miss is NOT_FOUND, never an implicit web search."""
    resolver = _resolver_with({})
    with patch("webbrowser.open") as mock_open:
        result = resolver.resolve("definitely not installed")
    assert result.outcome == "NOT_FOUND"
    mock_open.assert_not_called()


# ---- discovery merging: MSIX apps must always launch via AUMID, never a raw exe path ----------
# Regression test: Start Menu shortcut / App Paths enrichment used to unconditionally overwrite
# launch_method to "exe" whenever it found ANY resolvable path for a name already discovered via
# Get-StartApps, even for packaged apps -- discovered live on this machine (Notepad's .lnk
# resolves to a WindowsApps-internal exe path that isn't reliably launchable by directly spawning
# it outside package activation).


def test_msix_launch_method_survives_start_menu_shortcut_enrichment(tmp_path):
    resolver = WindowsAppResolver()
    records = {
        "clock": AppRecord(id="clock", display_name="Clock", app_type="MSIX",
                           app_user_model_id="Microsoft.WindowsAlarms_8wekyb3d8bbwe!App",
                           launch_method="app_id", source="start_apps"),
    }
    lnk = tmp_path / "Clock.lnk"
    lnk.write_bytes(b"")  # content is irrelevant; CreateShortcut is mocked below

    fake_shell = type("FakeShell", (), {
        "CreateShortcut": lambda self, path: type("Shortcut", (), {"Targetpath": str(tmp_path / "Clock.exe")})(),
    })()
    (tmp_path / "Clock.exe").write_bytes(b"")

    with patch("skills.app_resolver._START_MENU_DIRS", [tmp_path]), \
         patch("win32com.client.Dispatch", return_value=fake_shell):
        resolver._discover_start_menu_shortcuts(records)

    clock = records["clock"]
    assert clock.launch_method == "app_id"
    assert clock.app_user_model_id == "Microsoft.WindowsAlarms_8wekyb3d8bbwe!App"
    assert clock.executable is None  # never downgraded to a raw exe path


def test_win32_app_gets_enriched_with_a_real_exe_path_from_a_shortcut(tmp_path):
    resolver = WindowsAppResolver()
    records = {
        "metatrader 5": AppRecord(id="metatrader 5", display_name="MetaTrader 5", app_type="WIN32",
                                  app_user_model_id="{GUID}\\MetaTrader 5\\terminal64.exe",
                                  launch_method="app_id", source="start_apps"),
    }
    lnk = tmp_path / "MetaTrader 5.lnk"
    lnk.write_bytes(b"")
    target = tmp_path / "terminal64.exe"
    target.write_bytes(b"")

    fake_shell = type("FakeShell", (), {
        "CreateShortcut": lambda self, path: type("Shortcut", (), {"Targetpath": str(target)})(),
    })()

    with patch("skills.app_resolver._START_MENU_DIRS", [tmp_path]), \
         patch("win32com.client.Dispatch", return_value=fake_shell):
        resolver._discover_start_menu_shortcuts(records)

    mt5 = records["metatrader 5"]
    assert mt5.launch_method == "exe"
    assert mt5.executable == str(target)


# ---- normalization ------------------------------------------------------------------------------


def test_normalize_is_case_and_punctuation_insensitive():
    assert _normalize("Visual Studio Code") == _normalize("visual-studio-code!!")
    assert _normalize("  Clock  ") == "clock"
