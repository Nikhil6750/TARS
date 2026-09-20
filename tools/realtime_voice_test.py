"""Human-only physical verification against the actual desktop audio path.

One command: python tools/realtime_voice_test.py --launch
Never reports PASS based only on synthetic audio or backend acknowledgements.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
import urllib.request
from pathlib import Path


def run():
    parser = argparse.ArgumentParser()
    parser.add_argument("--launch", action="store_true")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.launch:
        subprocess.run(["powershell", "-ExecutionPolicy", "Bypass", "-File",
                        str(root / "scripts/start_tars.ps1")], cwd=root, check=True)
    print('\nSay: "TARS, what is happening with EURUSD?"')
    print('While TARS speaks say: "Stop. What about gold?"')
    print("Observe the partial transcript while talking, then listen for the second answer.")
    print("Native capture has no acoustic echo cancellation: use headphones for the first run.")
    print("Waiting up to 3 minutes. Ctrl+C stops the diagnostic, not TARS.", flush=True)
    deadline = time.monotonic() + 180
    previous = None
    first_turn = None
    second_turn = None
    snapshot = {}
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(args.url + "/api/v1/voice/realtime/diagnostics", timeout=3) as response:
                snapshot = json.load(response)
        except OSError as exc:
            print(f"Backend unavailable: {exc}")
            time.sleep(1)
            continue
        events = snapshot.get("events", [])
        for event in events:
            if event["type"] == "final_transcript":
                text = event.get("text", "").lower()
                if "eur" in text or "euro" in text:
                    first_turn = event["turn_id"]
                if "gold" in text and first_turn and event["turn_id"] != first_turn:
                    second_turn = event["turn_id"]
        current = json.dumps({"state": snapshot.get("state"), "metrics": snapshot.get("latest_ms")})
        if current != previous:
            print(current, flush=True)
            previous = current
        if first_turn and second_turn and snapshot.get("state") == "LISTENING":
            break
        time.sleep(0.25)
    events = snapshot.get("events", [])
    first = [e for e in events if e.get("turn_id") == first_turn]
    second = [e for e in events if e.get("turn_id") == second_turn]
    interrupt = next((i for i, e in enumerate(events) if e["type"] == "interrupt"
                      and e.get("previous_turn_id") == first_turn and e.get("turn_id") == second_turn), None)
    checks = {
        "partial_during_first_speech": any(e["type"] == "partial_transcript" for e in first),
        "first_finalized": bool(first_turn),
        "first_response_audio": any(e["type"] == "audio_chunk" for e in first),
        "second_turn_owns_interruption": interrupt is not None,
        "no_old_audio_after_interruption": interrupt is not None and not any(
            e["type"] == "audio_chunk" and e.get("turn_id") == first_turn for e in events[interrupt + 1:]),
        "second_finalized": bool(second_turn),
        "second_response_audio": any(e["type"] == "audio_chunk" for e in second),
        "latency_printed": bool(snapshot.get("latest_ms")),
    }
    print(json.dumps({"checks": checks, "metrics": snapshot.get("latest_ms"),
                      "p50_ms": snapshot.get("p50_ms")}, indent=2))
    heard = input("Did the first speech stop immediately, stay stopped, and the gold answer speak? [yes/no]: ")
    saw = input("Did you see partial words DURING your first utterance? [yes/no]: ")
    passed = all(checks.values()) and heard.strip().lower() == "yes" and saw.strip().lower() == "yes"
    print("PHYSICAL MIC TEST: " + ("PASS" if passed else "FAIL"))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(run())
