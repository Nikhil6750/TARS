"""No microphone or speaker claims: measure local inference on synthetic audio.

python tools/benchmark_realtime_voice.py --provider kokoro
uvx --from pocket-tts python tools/benchmark_realtime_voice.py --provider pocket
"""
import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/backend"))


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=["kokoro", "pocket"], required=True)
    args = parser.parse_args()
    text = "I don't have live EURUSD data yet. Keep your chart visible and I can inspect it."
    first, total = [], []
    if args.provider == "pocket":
        import torch
        from pocket_tts import TTSModel
        torch.set_num_threads(2)
        model = TTSModel.load_model()
        state = model.get_state_for_audio_prompt("alba")
        for _ in range(3):
            started = time.perf_counter()
            for index, _chunk in enumerate(model.generate_audio_stream(state, text)):
                if index == 0:
                    first.append((time.perf_counter() - started) * 1000)
            total.append((time.perf_counter() - started) * 1000)
    else:
        from app.config import Settings
        from voice.factory import build_tts_provider
        model = build_tts_provider(Settings(tts_provider="kokoro"))
        for _ in range(3):
            started = time.perf_counter()
            await model.synthesize(text.split(". ")[0] + ".")
            first.append((time.perf_counter() - started) * 1000)
            await model.synthesize(text.split(". ")[1])
            total.append((time.perf_counter() - started) * 1000)
    print(json.dumps({"provider": args.provider, "first_audio_ms": first,
                      "total_ms": total, "first_audio_p50_ms": statistics.median(first),
                      "physical_test": False}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
