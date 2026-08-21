"""Detect known TARS core-experience release blockers.

A healthy HTTP endpoint or a running process is not treated as proof that
wake, voice, rendering, or the native window is usable.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
try:
    from tools.capture_native_tars import choose_main_window, enumerate_windows
except ModuleNotFoundError:  # direct ``python tools/core_experience_checks.py``
    from capture_native_tars import choose_main_window, enumerate_windows


@dataclass(frozen=True)
class Finding:
    finding_id: str
    severity: str
    detail: str


def _text(relative: str, root: Path) -> str:
    return (root / relative).read_text(encoding="utf-8")


def inspect_source(root: Path = ROOT) -> list[Finding]:
    findings: list[Finding] = []
    launcher = _text("scripts/start_tars.ps1", root)
    storage = _text("apps/web/src/services/storage.ts", root)
    voice_view = _text("apps/web/src/components/voice/VoiceControlView.tsx", root)
    wake = _text("apps/web/src-tauri/src/wake_engine.rs", root)
    voice_runtime = _text("apps/web/src/runtime/VoiceAssistantRuntime.tsx", root)
    assistant_message = _text(
        "apps/web/src/components/assistant/AssistantMessage.tsx", root
    )

    if "No .env found" in launcher and "exit 1" in launcher:
        findings.append(
            Finding(
                "launcher.worktree_env_required",
                "blocker",
                "a newly-created worktree cannot start until a separate .env is copied",
            )
        )
    if "[7/7] TARS READY" in launcher and "if (-not $readiness.ready" in launcher:
        findings.append(
            Finding(
                "launcher.false_ready",
                "blocker",
                "launcher warns on readiness=false but still prints TARS READY and Say: Hey TARS",
            )
        )
    if "D:\\TARS-cache\\cargo-target" in launcher and "tars-companion.exe" in launcher:
        findings.append(
            Finding(
                "launcher.shared_stale_binary",
                "blocker",
                "launcher accepts a shared Cargo-cache executable without source/build provenance",
            )
        )
    if "MainWindowHandle" not in launcher and "frontend loaded" not in launcher.lower():
        findings.append(
            Finding(
                "launcher.no_native_window_verification",
                "blocker",
                "launcher never proves the main Tauri webview was created and loaded",
            )
        )
    if "compactMode: true" in storage:
        findings.append(
            Finding(
                "native.default_compact_clips_workstation",
                "blocker",
                "default settings shrink the workstation UI to the 380x180 compact window",
            )
        )
    if "openWakeWord" in voice_view and "Fish Speech / Kokoro" in voice_view:
        findings.append(
            Finding(
                "voice.ui_hardcoded_provider_status",
                "high",
                "Voice Control presents hard-coded provider labels rather than runtime state",
            )
        )
    if "enum Mode" in wake and "Wake," in wake and "Command," in wake:
        findings.append(
            Finding(
                "wake.incomplete_state_machine",
                "blocker",
                "native wake engine has only Wake/Command modes, not the required explicit lifecycle",
            )
        )
    required_timing_names = {
        "audio_detected_at",
        "speech_end_at",
        "transcription_start",
        "transcription_complete",
        "wake_detected_at",
        "command_ready_at",
    }
    missing_timing = sorted(name for name in required_timing_names if name not in wake)
    if missing_timing:
        findings.append(
            Finding(
                "wake.missing_latency_instrumentation",
                "high",
                f"native wake engine does not emit: {', '.join(missing_timing)}",
            )
        )
    wake_branch = wake.split("wake_regex().is_match(&transcript)", 1)
    if len(wake_branch) == 2 and "command-transcript" not in wake_branch[1].split("}", 1)[0]:
        findings.append(
            Finding(
                "wake.single_utterance_command_dropped",
                "blocker",
                "general commands following Hey TARS in the same utterance are not extracted or emitted",
            )
        )
    if "feedStreamingText(chunk)" in voice_runtime:
        findings.append(
            Finding(
                "speech.raw_stream_markdown_to_tts",
                "blocker",
                "raw provider Markdown deltas are sentence-chunked directly into TTS",
            )
        )
    if "onSpeak(message.content)" in assistant_message:
        findings.append(
            Finding(
                "speech.display_markdown_to_tts",
                "blocker",
                "manual Speak sends the full display Markdown to synthesis",
            )
        )
    if not any((root / "apps/backend").rglob("*response_quality*")):
        findings.append(
            Finding(
                "answers.no_response_quality_contract",
                "high",
                "no internal ResponseQualityContract implementation is present",
            )
        )
    return findings


def _get_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


def inspect_runtime(base_url: str, process_name: str) -> list[Finding]:
    findings: list[Finding] = []
    try:
        health = _get_json(f"{base_url.rstrip('/')}/api/v1/health")
        readiness = _get_json(f"{base_url.rstrip('/')}/api/v1/runtime/readiness")
        health_wake = health.get("wake_word_provider")
        readiness_wake = readiness.get("wake", {}).get("configured")
        if health_wake != readiness_wake:
            findings.append(
                Finding(
                    "runtime.conflicting_wake_status",
                    "high",
                    "health and readiness report different wake architectures: "
                    f"{health_wake!r} vs {readiness_wake!r}",
                )
            )
        if not readiness.get("ready", False):
            findings.append(
                Finding(
                    "runtime.core_not_ready",
                    "blocker",
                    f"runtime readiness is false: {readiness}",
                )
            )
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        findings.append(Finding("runtime.backend_unreachable", "blocker", str(exc)))

    if os.name == "nt":
        try:
            import psutil

            processes = [
                process
                for process in psutil.process_iter(["pid", "name", "create_time"])
                if (process.info["name"] or "").lower() == process_name.lower()
            ]
            if not processes:
                findings.append(
                    Finding("runtime.native_process_missing", "blocker", process_name)
                )
            else:
                newest = max(
                    processes, key=lambda process: process.info["create_time"] or 0
                )
                window = choose_main_window(enumerate_windows(newest.pid))
                if window is None:
                    findings.append(
                        Finding(
                            "runtime.native_main_window_missing",
                            "blocker",
                            f"PID {newest.pid} has no usable visible native main window",
                        )
                    )
                elif window.width < 800 or window.height < 500:
                    findings.append(
                        Finding(
                            "runtime.native_window_clipped",
                            "blocker",
                            f"native main window is only {window.width}x{window.height}",
                        )
                    )
        except Exception as exc:  # noqa: BLE001 - probe failures are findings
            findings.append(Finding("runtime.native_probe_failed", "high", str(exc)))
    return findings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--runtime", action="store_true")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--process-name", default="tars-companion.exe")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    findings = inspect_source(args.root.resolve())
    if args.runtime:
        findings.extend(inspect_runtime(args.base_url, args.process_name))
    payload = {
        "root": str(args.root.resolve()),
        "runtime_checked": args.runtime,
        "finding_count": len(findings),
        "findings": [asdict(item) for item in findings],
    }
    print(json.dumps(payload, indent=2))
    raise SystemExit(1 if findings else 0)


if __name__ == "__main__":
    main()
