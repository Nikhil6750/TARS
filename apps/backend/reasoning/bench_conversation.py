"""Real latency/quality probe for the CONVERSATION path. python -m reasoning.bench_conversation [--n 8] [--json out.json]
Every call is a real network call with a hard timeout. Measures first-token and total latency per candidate (p50/p95) and flags failures."""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time

from . import siwc
from .conversation import SYSTEM_PROMPT
from .openai_client import OpenAIError, OpenAIResponsesClient

PROMPTS = [
    "Tell me a short joke.", "What's a good way to stay focused when studying?", "Explain what an API is in simple words.",
    "What's the difference between a stock and a bond?", "Give me a quick tip for sleeping better.", "How do I say thank you in Spanish?",
    "What is machine learning, briefly?", "Suggest a name for a calm productivity playlist.", "Why is the sky blue?", "How long should I boil an egg?",
]


def pct(v: list[float], p: float) -> float:
    s = sorted(v)
    return round(s[min(len(s) - 1, int(round(p * (len(s) - 1))))], 2)


async def run_openai(client: OpenAIResponsesClient, model: str, effort: str | None, prompts: list[str]) -> dict:
    firsts, totals, fails, words = [], [], [], []
    for q in prompts:
        t0 = time.perf_counter()
        first = None
        text = ""
        try:
            async for kind, payload in client.stream(model, SYSTEM_PROMPT, q, max_output_tokens=None, reasoning_effort=effort):
                if kind == "delta":
                    first = first or time.perf_counter() - t0
                elif kind == "completed":
                    text = payload
            firsts.append(first or time.perf_counter() - t0)
            totals.append(time.perf_counter() - t0)
            words.append(len(text.split()))
        except (OpenAIError, asyncio.TimeoutError) as exc:
            fails.append(type(exc).__name__)
    return summarize(f"{model} effort={effort}", firsts, totals, fails, words, len(prompts))


async def run_claude(prompts: list[str]) -> dict:
    from assistant.provider import AssistantRequest
    from assistant.providers.claude_code import ClaudeCodeProvider
    prov = ClaudeCodeProvider(timeout_seconds=40, persist_sessions=False)
    totals, fails, words = [], [], []
    for i, q in enumerate(prompts):
        t0 = time.perf_counter()
        try:
            r = await prov.respond(AssistantRequest(text=q, conversation_id=f"bench-{i}", system_context=SYSTEM_PROMPT))
            totals.append(time.perf_counter() - t0)
            words.append(len(r.text.split()))
        except Exception as exc:
            fails.append(type(exc).__name__)
    return summarize("claude-code-cli (non-streaming)", totals, totals, fails, words, len(prompts))


async def run_gemini(model: str, prompts: list[str], key: str) -> dict:
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=20000))
    firsts, totals, fails, words = [], [], [], []
    for q in prompts:
        t0 = time.perf_counter()
        first = None
        out = ""
        try:
            async def go():
                nonlocal first, out
                stream = await client.aio.models.generate_content_stream(model=model, contents=q, config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT))
                async for ch in stream:
                    first = first or time.perf_counter() - t0
                    out += ch.text or ""
            await asyncio.wait_for(go(), timeout=25)
            firsts.append(first or time.perf_counter() - t0)
            totals.append(time.perf_counter() - t0)
            words.append(len(out.split()))
        except Exception as exc:
            fails.append(type(exc).__name__ + ":" + str(exc)[:60])
    return summarize(f"gemini {model}", firsts, totals, fails, words, len(prompts))


def summarize(label, firsts, totals, fails, words, n) -> dict:
    return {"candidate": label, "n": n, "ok": len(totals), "failures": fails[:3], "first_p50": pct(firsts, .5) if firsts else None, "first_p95": pct(firsts, .95) if firsts else None,
            "total_p50": pct(totals, .5) if totals else None, "total_p95": pct(totals, .95) if totals else None,
            "words_p50": int(statistics.median(words)) if words else None}


async def main_async(n: int, gemini_key: str | None, only: list[str] | None) -> list[dict]:
    client = OpenAIResponsesClient(siwc.SiwcAuth(), timeout=40)
    prompts = PROMPTS[:n]
    rows = []
    plan = [("gpt-6-luna", None), ("gpt-6-luna", "low"), ("gpt-5.6-luna", "low"), ("gpt-5.6-terra", "low"), ("gpt-6.1-sol", "low")]
    for model, effort in plan:
        if only and model not in only:
            continue
        rows.append(await run_openai(client, model, effort, prompts))
        print(json.dumps(rows[-1]), flush=True)
    if not only or "claude" in only:
        rows.append(await run_claude(prompts[: max(3, n // 2)]))
        print(json.dumps(rows[-1]), flush=True)
    if gemini_key and (not only or "gemini" in only):
        for gm in ("gemini-2.5-flash", "gemini-2.0-flash"):
            rows.append(await run_gemini(gm, prompts, gemini_key))
            print(json.dumps(rows[-1]), flush=True)
    await client.aclose()
    return rows


if __name__ == "__main__":
    import os
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--json")
    a = ap.parse_args()
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        for line in open(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".env"), encoding="utf-8"):
            if line.startswith("GEMINI_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"')
    rows = asyncio.run(main_async(a.n, key, a.only))
    if a.json:
        open(a.json, "w").write(json.dumps(rows, indent=1))
