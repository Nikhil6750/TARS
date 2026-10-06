"""Local STT accuracy/latency benchmark (no network, no cloud). Usage:
  python tools/stt_bench.py <wav_dir> [--beam N] [--device auto|cpu] [--model small.en] [--repeat N]
WAVs are pNN.wav with phrases.txt (one expected phrase per line)."""
import argparse, asyncio, json, os, re, statistics, sys, time, wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "backend"))
from voice.local_intents import LocalIntentRouter  # noqa: E402
from voice.providers.faster_whisper_stt import FasterWhisperSTTProvider  # noqa: E402


def norm(t): return re.sub(r"[^a-z0-9 ]", "", t.lower().replace("-", " ")).strip()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("wav_dir"); ap.add_argument("--beam", type=int, default=1)
    ap.add_argument("--device", default="auto"); ap.add_argument("--model", default="small.en")
    ap.add_argument("--repeat", type=int, default=3)
    a = ap.parse_args()
    d = Path(a.wav_dir)
    phrases = (d / "phrases.txt").read_text(encoding="utf-8-sig").splitlines()
    t0 = time.perf_counter()
    prov = FasterWhisperSTTProvider(a.model, a.device, "auto", beam_size=a.beam)
    load_s = time.perf_counter() - t0
    await prov.transcribe(bytes(32000))  # warm
    router = LocalIntentRouter()
    rows, lat = [], []
    for i, exp in enumerate(phrases, 1):
        with wave.open(str(d / f"p{i:02d}.wav")) as w:
            pcm = w.readframes(w.getnframes())
        times = []
        for _ in range(a.repeat):
            s = time.perf_counter(); r = await prov.transcribe(pcm); times.append((time.perf_counter() - s) * 1000)
        lat += times
        intent = router.route(r.text)
        rows.append({"expected": exp, "actual": r.text, "match": norm(exp) == norm(r.text),
                     "ms": round(statistics.median(times)), "route": intent and (intent.name, intent.needs_cloud)})
    lat.sort()
    p95 = lat[min(len(lat) - 1, int(len(lat) * 0.95 + 0.9999) - 1)]
    out = {"model": a.model, "beam": a.beam, "health": prov.health.snapshot(), "load_s": round(load_s, 2),
           "median_ms": round(statistics.median(lat)), "p95_ms": round(p95), "n": len(lat),
           "exact": sum(r["match"] for r in rows), "total": len(rows), "rows": rows}
    try:
        import psutil; out["rss_mb"] = round(psutil.Process().memory_info().rss / 1e6)
    except Exception: pass
    print(json.dumps(out, indent=1))

asyncio.run(main())
