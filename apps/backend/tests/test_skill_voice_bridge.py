from __future__ import annotations

from app.action_contracts import ActionSource
from skills.voice_bridge import build_action_request_from_voice


def test_focus_phrase_builds_windows_app_focus_request():
    request = build_action_request_from_voice("focus notepad", source=ActionSource.voice_ptt)
    assert request is not None
    assert request.skill == "windows_app"
    assert request.action == "focus"
    assert request.arguments == {"target": "notepad"}
    assert request.source == ActionSource.voice_ptt


def test_open_url_phrase_builds_browser_open_url_request():
    request = build_action_request_from_voice(
        "open https://example.com", source=ActionSource.voice_wake_word
    )
    assert request is not None
    assert request.skill == "browser"
    assert request.action == "open_url"
    assert request.arguments == {"url": "https://example.com"}


def test_search_phrase_builds_browser_search_request():
    request = build_action_request_from_voice(
        "search for python tutorials", source=ActionSource.voice_ptt
    )
    assert request is not None
    assert request.skill == "browser"
    assert request.action == "search"
    assert request.arguments == {"query": "python tutorials"}


def test_launch_phrase_builds_windows_app_launch_request():
    request = build_action_request_from_voice("launch notepad", source=ActionSource.hotkey)
    assert request is not None
    assert request.skill == "windows_app"
    assert request.action == "launch"
    assert request.arguments == {"target": "notepad"}


def test_run_phrase_builds_terminal_run_command_request():
    request = build_action_request_from_voice("run whoami", source=ActionSource.voice_ptt)
    assert request is not None
    assert request.skill == "terminal"
    assert request.action == "run_command"
    assert request.arguments == {"command": "whoami"}


def test_run_command_phrase_builds_terminal_run_command_request():
    request = build_action_request_from_voice(
        "run command format C: /y", source=ActionSource.voice_ptt
    )
    assert request is not None
    assert request.skill == "terminal"
    assert request.action == "run_command"
    assert request.arguments == {"command": "format C: /y"}


def test_real_stt_output_with_trailing_period_still_resolves_launch_target():
    # Regression: real speech-to-text output for "open notepad" comes back
    # as "Open Notepad." (capitalized, trailing period) -- verified against
    # a real faster-whisper transcription, not assumed. Left unstripped,
    # "Notepad." never resolves via shutil.which (only "Notepad" does), so
    # a genuinely spoken "open notepad" would have been silently denied.
    request = build_action_request_from_voice("Open Notepad.", source=ActionSource.voice_ptt)
    assert request is not None
    assert request.skill == "windows_app"
    assert request.action == "launch"
    assert request.arguments == {"target": "Notepad"}


def test_bare_open_phrase_without_app_keyword_builds_launch_request():
    # Regression: the launch pattern must accept bare "open X" (the most
    # natural phrasing), not only "open app X" / "open application X".
    request = build_action_request_from_voice("open notepad", source=ActionSource.voice_ptt)
    assert request is not None
    assert request.skill == "windows_app"
    assert request.action == "launch"
    assert request.arguments == {"target": "notepad"}


def test_unrecognized_phrase_returns_none():
    assert build_action_request_from_voice(
        "what's the weather like today", source=ActionSource.voice_ptt
    ) is None


def test_empty_text_returns_none():
    assert build_action_request_from_voice("", source=ActionSource.voice_ptt) is None
    assert build_action_request_from_voice("   ", source=ActionSource.voice_ptt) is None


def test_open_invalid_url_scheme_does_not_match_url_pattern_falls_to_launch():
    # "open javascript:alert(1)" isn't http(s), so it must not be treated
    # as a URL open -- it also shouldn't match anything since it doesn't
    # look like a normal app name either, but at minimum must never
    # construct an open_url request with a disallowed scheme.
    request = build_action_request_from_voice(
        "open javascript:alert(1)", source=ActionSource.voice_ptt
    )
    if request is not None:
        assert not (request.skill == "browser" and request.action == "open_url")


def test_active_context_is_passed_through():
    from app.action_contracts import ActiveWindowContext

    ctx = ActiveWindowContext(executable="explorer.exe", window_title="File Explorer")
    request = build_action_request_from_voice(
        "focus notepad", source=ActionSource.voice_ptt, active_context=ctx
    )
    assert request is not None
    assert request.active_context == ctx
