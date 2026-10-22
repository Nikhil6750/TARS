"""Drive a REAL TARS realtime voice session (backend + Gemini Live) with injected speech and record what happened.

python scripts/voice_probe.py --wav open_calc.wav [--wav joke.wav ...] [--interrupt-with stop.wav --interrupt-after 1.5] [--foreground Calculator]

The audio is 16 kHz mono 16-bit speech (e.g. synthesised with Windows SAPI) sent in 32 ms frames at real-time pace followed by silence, exactly
like a microphone would deliver it. This is NOT a physical-microphone test; it exercises everything after the microphone: VAD, Gemini Live
understanding, routing/tools, the reviewed intelligence pipeline, speech output, interruption. No credentials are printed."""
from __future__ import annotations

import argparse
import asyncio
import base64
import ctypes
import json
import time
import wave

import websockets

URL = "ws://127.0.0.1:8000/api/v1/voice/realtime"
FRAME = 1024                 # bytes = 32 ms at 16 kHz mono 16-bit
SILENCE_S = 1.8


def read_pcm(path: str) -> bytes:
    with wave.open(path, "rb") as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2, "need 16 kHz mono 16-bit"
        return w.readframes(w.getnframes())


def foreground_title() -> str:
    u = ctypes.windll.user32
    h = u.GetForegroundWindow()
    buf = ctypes.create_unicode_buffer(256)
    u.GetWindowTextW(h, buf, 256)
    return buf.value


class Probe:
    """Acts like a microphone: a continuous 32 ms frame clock that carries queued speech, otherwise silence (the backend treats a stalled mic as a disconnect)."""

    def __init__(self, ws) -> None:
        self.ws, self.events, self.t0 = ws, [], time.perf_counter()
        self.queue: list[bytes] = []
        self.task = asyncio.create_task(self._recv())
        self.mic = asyncio.create_task(self._mic())

    async def _mic(self):
        nxt = time.perf_counter()
        try:
            while True:
                frame = self.queue.pop(0) if self.queue else b"\0" * FRAME
                await self.ws.send(frame)
                nxt += FRAME / 2 / 16000
                await asyncio.sleep(max(0.0, nxt - time.perf_counter()))
        except Exception:
            pass

    async def _recv(self):
        try:
            async for raw in self.ws:
                if isinstance(raw, bytes):
                    continue
                m = json.loads(raw)
                m["_t"] = time.perf_counter()
                if m.get("type") == "audio_pcm":
                    m["_bytes"] = len(base64.b64decode(m.get("audio", "")))
                    m["_rate"] = m.get("sample_rate", 24000)
                    m.pop("audio", None)
                self.events.append(m)
        except Exception:
            pass

    async def send_speech(self, pcm: bytes, label: str) -> float:
        """Queue pcm onto the mic clock; return the perf_counter time the SPEECH ended (silence follows automatically)."""
        frames = [pcm[i:i + FRAME].ljust(FRAME, b"\0") for i in range(0, len(pcm), FRAME)]
        self.queue.extend(frames)
        while self.queue:
            await asyncio.sleep(0.02)
        return time.perf_counter()

    def since(self, t, kind, start=0):
        return [e for e in self.events[start:] if e.get("type") == kind and e["_t"] >= t]


async def settle(p: Probe, after: float, timeout: float, quiet_s: float = 2.5) -> None:
    """Wait for response_complete (or a quiet period after the last event) after `after`."""
    end = time.perf_counter() + timeout
    while time.perf_counter() < end:
        await asyncio.sleep(0.25)
        done = p.since(after, "response_complete")
        last = max((e["_t"] for e in p.events if e["_t"] >= after), default=after)
        if done and time.perf_counter() - last > quiet_s:
            return


def summarize(p: Probe, t_end: float, label: str, start_idx: int) -> dict:
    ev = [e for e in p.events[start_idx:] if e["_t"] >= t_end - 8]
    rel = lambda t: round(t - t_end, 2)
    first = lambda kind: next((e for e in ev if e.get("type") == kind and e["_t"] >= t_end - 0.3), None)
    tc, tr, au, rc = first("tool_call"), first("tool_result"), first("audio_pcm"), first("response_complete")
    audio = [e for e in ev if e.get("type") == "audio_pcm"]
    transcripts = [e.get("text") for e in ev if e.get("type") == "final_transcript"]
    spoken = "".join(e.get("text", "") for e in ev if e.get("type") == "delta")
    tools = [(e.get("name"), rel(e["_t"])) for e in ev if e.get("type") == "tool_call"]
    results = [(e.get("name"), e.get("status"), rel(e["_t"])) for e in ev if e.get("type") == "tool_result"]
    return {"label": label, "heard": transcripts, "tool_calls": tools, "tool_results": results,
            "t_first_tool_call": rel(tc["_t"]) if tc else None, "t_tool_result": rel(tr["_t"]) if tr else None, "t_first_audio": rel(au["_t"]) if au else None,
            "t_response_complete": rel(rc["_t"]) if rc else None, "spoken_text": spoken[:600], "audio_seconds": round(sum(e["_bytes"] / 2 / e["_rate"] for e in audio), 1),
            "interrupts": [rel(e["_t"]) for e in ev if e.get("type") == "interrupt"],
            "last_audio_chunk_at": rel(audio[-1]["_t"]) if audio else None, "states": [e.get("state") or e.get("name") for e in ev if e.get("type") == "state"][:12]}


async def main(a) -> None:
    async with websockets.connect(URL, max_size=None, open_timeout=20) as ws:
        p = Probe(ws)
        await asyncio.sleep(1.0)
        out = []
        for path in a.wav:
            idx = len(p.events)
            pcm = read_pcm(path)
            t_end = await p.send_speech(pcm, path)
            await settle(p, t_end, a.timeout)
            row = summarize(p, t_end, path.split("\\")[-1], idx)
            if a.foreground:
                await asyncio.sleep(0.5)
                row["foreground_after"] = foreground_title()
            out.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
            await asyncio.sleep(1.0)
        if a.interrupt_with:
            idx = len(p.events)
            speak = asyncio.create_task(p.send_speech(read_pcm(a.first_long), "long"))
            t_first_audio = None
            for _ in range(int(a.timeout / 0.1)):
                await asyncio.sleep(0.1)
                au = [e for e in p.events[idx:] if e.get("type") == "audio_pcm"]
                if au:
                    t_first_audio = au[0]["_t"]
                    break
            t_speech_end = await speak
            # wait until TARS is audibly speaking, then barge in
            for _ in range(int(a.timeout / 0.1)):
                if any(e.get("type") == "audio_pcm" for e in p.events[idx:]):
                    break
                await asyncio.sleep(0.1)
            await asyncio.sleep(a.interrupt_after)
            speaking_before = [e for e in p.events[idx:] if e.get("type") == "audio_pcm"]
            t_barge = time.perf_counter()
            idx2 = len(p.events)
            t_end2 = await p.send_speech(read_pcm(a.interrupt_with), "barge-in")
            await settle(p, t_end2, a.timeout)
            row = summarize(p, t_end2, "barge-in", idx2)
            row["audio_chunks_before_barge_in"] = len(speaking_before)
            audio_after = [e for e in p.events[idx:] if e.get("type") == "audio_pcm" and e["_t"] > t_barge + 0.2]
            row["barge_in_started_at"] = round(t_barge - p.t0, 2)
            row["first_interrupt_after_barge_in_start_s"] = next((round(e["_t"] - t_barge, 2) for e in p.events[idx:] if e.get("type") == "interrupt" and e["_t"] >= t_barge), None)
            row["long_answer_audio_after_barge_in_chunks"] = len(audio_after)
            if a.foreground:
                await asyncio.sleep(0.5)
                row["foreground_after"] = foreground_title()
            print(json.dumps(row, ensure_ascii=False), flush=True)
        p.task.cancel()
        p.mic.cancel()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", action="append", default=[])
    ap.add_argument("--timeout", type=float, default=120)
    ap.add_argument("--foreground", action="store_true")
    ap.add_argument("--interrupt-with")
    ap.add_argument("--first-long", help="wav for the long question that gets interrupted")
    ap.add_argument("--interrupt-after", type=float, default=1.5)
    asyncio.run(main(ap.parse_args()))
