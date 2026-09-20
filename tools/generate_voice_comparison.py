"""Generate local Kokoro A/B/C listening samples without downloading models."""

from __future__ import annotations

import argparse
import json
import time
import wave
from pathlib import Path

LINES = (
    "Good morning. I'm ready. What are we working on today?",
    (
        "I've checked the chart. Price is approaching the previous high, but I "
        "wouldn't call this a valid trade yet."
    ),
)
DEFAULT_CANDIDATES = ("am_michael", "am_onyx", "bm_george")
CURRENT_REFERENCE = "af_heart"


def write_wav(path: Path, samples: object, sample_rate: int) -> float:
    import numpy as np

    normalized = np.asarray(samples, dtype=np.float32)
    pcm = (np.clip(normalized, -1.0, 1.0) * 32767.0).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(pcm.tobytes())
    return len(pcm) / sample_rate


def generate_candidate(
    kokoro: object, voice: str, speed: float
) -> tuple[object, int, float]:
    import numpy as np

    chunks: list[object] = []
    sample_rate = 0
    started = time.perf_counter()
    for index, line in enumerate(LINES):
        audio, line_rate = kokoro.create(  # type: ignore[attr-defined]
            line, voice=voice, speed=speed, lang="en-us", trim=True
        )
        sample_rate = int(line_rate)
        chunks.append(audio)
        if index != len(LINES) - 1:
            chunks.append(np.zeros(int(sample_rate * 0.35), dtype=np.float32))
    return np.concatenate(chunks), sample_rate, time.perf_counter() - started


def parse_args() -> argparse.Namespace:
    home_cache = Path.home() / ".cache/pipecat/kokoro-onnx"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=home_cache / "kokoro-v1.0.onnx")
    parser.add_argument(
        "--voices-file", type=Path, default=home_cache / "voices-v1.0.bin"
    )
    parser.add_argument("--voices", nargs="+", default=list(DEFAULT_CANDIDATES))
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--include-current-reference", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    missing = [path for path in (args.model, args.voices_file) if not path.is_file()]
    if missing:
        raise SystemExit(
            "Kokoro files are not already installed; refusing an implicit large download: "
            + ", ".join(str(path) for path in missing)
        )
    from kokoro_onnx import Kokoro

    kokoro = Kokoro(str(args.model), str(args.voices_file))
    available = set(kokoro.get_voices())
    voices = list(args.voices)
    if args.include_current_reference and CURRENT_REFERENCE not in voices:
        voices.insert(0, CURRENT_REFERENCE)
    unknown = [voice for voice in voices if voice not in available]
    if unknown:
        raise SystemExit(f"unknown installed Kokoro voices: {unknown}")

    manifest: dict[str, object] = {
        "lines": list(LINES),
        "speed": args.speed,
        "subjective_listening_required": True,
        "note": (
            "Latency/duration are measured; naturalness, warmth, clarity, "
            "authority, and conversational quality require user listening."
        ),
        "candidates": [],
    }
    candidate_number = 0
    for voice in voices:
        is_reference = voice == CURRENT_REFERENCE and voice not in args.voices
        if is_reference:
            label = "reference-current"
        else:
            candidate_number += 1
            label = f"voice_{chr(64 + candidate_number)}"
        samples, sample_rate, generation_seconds = generate_candidate(
            kokoro, voice, args.speed
        )
        filename = f"{label}.wav"
        audio_seconds = write_wav(args.output_dir / filename, samples, sample_rate)
        manifest["candidates"].append(  # type: ignore[union-attr]
            {
                "label": label,
                "voice": voice,
                "file": filename,
                "sample_rate": sample_rate,
                "generation_seconds": round(generation_seconds, 3),
                "audio_seconds": round(audio_seconds, 3),
                "real_time_factor": round(generation_seconds / audio_seconds, 3),
            }
        )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest | {"output_dir": str(args.output_dir.resolve())}, indent=2))


if __name__ == "__main__":
    main()
