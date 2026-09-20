"""Acoustic acceptance test for the live voice loop (real mic capture path).

Plays a spoken prompt through the laptop speakers, so the running desktop app's
NATIVE microphone capture hears it, then plays a barge-in utterance while TARS is
speaking. Timeline comes from the backend's own session history (polled) and its
latency metrics. This is a speaker->air->microphone test; a human voice test with
tools/realtime_voice_test.py remains the reference for accents/noise/echo.

Requires the app running (scripts/start_tars.ps1). Pocket TTS is used to make the
prompts, so it is run in-process here (about 10 s model load).

python tools/voice_acceptance.py [--first "..."] [--second "..."]
"""
import argparse
import io
import json
import sys
import time
import urllib.request
import wave
import winsound
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
URL = "http://127.0.0.1:8000/api/v1/voice/realtime/diagnostics"


def diag():
    with urllib.request.urlopen(URL, timeout=3) as r:
        return json.load(r)


def make_wav(text, path):
    import torch
    from pocket_tts import TTSModel

    torch.set_num_threads(2)
    m = TTSModel.load_model()
    state = m.get_state_for_audio_prompt("alba")
    audio = np.concatenate([c.detach().cpu().numpy().reshape(-1) for c in m.generate_audio_stream(state, text)])
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(m.sample_rate)
        w.writeframes(pcm)
    return len(audio) / m.sample_rate


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--first", default="TARS, what is happening with EURUSD?")
    ap.add_argument("--second", default="Stop. What about gold?")
    a = ap.parse_args()
    out = ROOT / "artifacts" / "voice-acceptance"
    out.mkdir(parents=True, exist_ok=True)
    d1 = make_wav(a.first, out / "first.wav")
    d2 = make_wav(a.second, out / "second.wav")
    d = diag()
    if d["state"] == "IDLE" and not d["providers"].get("microphone") == "CONNECTED":
        print("WARNING: voice session/mic not connected yet:", d["providers"], d["state"])
    seen: dict[str, float] = {}
    last_seq = max((e.get("seq", 0) for e in d["events"]), default=0)
    t0 = time.perf_counter()
    winsound.PlaySound(str(out / "first.wav"), winsound.SND_FILENAME | winsound.SND_ASYNC)
    print(f"played first prompt ({d1:.1f}s) at t=0")
    barged = False
    barge_at = None
    deadline = t0 + 90
    log = []
    while time.perf_counter() < deadline:
        try:
            d = diag()
        except OSError:
            continue
        now = time.perf_counter() - t0
        for ev in d["events"]:
            if ev.get("seq", 0) <= last_seq:
                continue
            last_seq = ev["seq"]
            log.append((round(now, 2), ev["type"], ev.get("state") or (ev.get("text") or "")[:70]))
            seen.setdefault(ev["type"], now)
        # barge in once TARS is audibly speaking (first audio chunk sent to the client)
        if not barged and "audio_chunk" in seen and now - seen["audio_chunk"] > 0.8:
            barge_at = now
            winsound.PlaySound(str(out / "second.wav"), winsound.SND_FILENAME | winsound.SND_ASYNC)
            barged = True
            print(f"played barge-in prompt at t={now:.2f}s")
        if barged and log and sum(1 for r in log if r[1] == "final_transcript") >= 2 and \
                any(r[1] == "tts_complete" and r[0] > barge_at + 1 for r in log):
            break
        time.sleep(0.05)
    winsound.PlaySound(None, winsound.SND_PURGE)
    print("\n-- timeline (s, event, detail)")
    last = None
    for row in log:
        if row[1] in ("delta",):
            if last == "delta":
                continue
        last = row[1]
        print(row)
    print("\n-- backend latency metrics (ms)")
    print(json.dumps(d.get("latest_ms"), indent=1))
    finals = [r for r in log if r[1] == "final_transcript"]
    partials = [r for r in log if r[1] == "partial_transcript"]
    print("\nfinal transcripts:", [f[2] for f in finals])
    print("partials before first final:", len([p for p in partials if not finals or p[0] < finals[0][0]]))
    print("interrupt events:", [r for r in log if r[1] == "interrupt"])
    sys.exit(0)


main()
