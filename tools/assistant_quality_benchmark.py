"""Run and score the fixed TARS Claude/Codex answer-quality corpus.

The scorer is intentionally deterministic and lightweight. It does not make a
second LLM call. Correctness still requires human review of the saved answers;
the automated score covers explicit corpus requirements and hygiene failures.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = Path(__file__).with_name("quality_corpus.json")
MARKDOWN_MARKER = re.compile(r"(^|\s)(#{1,6}|\*\*|__|```|`)(?=\S|$)")
URL = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
WINDOWS_PATH = re.compile(r"\b[A-Za-z]:\\[^\s]+")
UNIX_PATH = re.compile(r"(?<!\w)/(?:[^\s/]+/)+[^\s]+")
INTERNAL = re.compile(
    r"provider subprocess|exit code|system[_ ]context|C:\\TARS|git branch|commit sha|"
    r"permission prompt wasn't granted|web search tool was blocked",
    re.IGNORECASE,
)
UNCERTAINTY = re.compile(
    r"don't have|do not have|cannot|can't|not provided|insufficient|unknown|need (?:the|more)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class QualityCase:
    id: str
    category: str
    prompt: str
    max_words: int
    must_include_any: list[str]
    forbidden: list[str]
    requires_uncertainty: bool


@dataclass(frozen=True)
class QualityResult:
    directness: bool
    grounding: bool
    completeness: bool
    structure: bool
    uncertainty: bool
    user_mode_cleanliness: bool
    speech_suitability: bool

    @property
    def passed(self) -> int:
        return sum(asdict(self).values())


def load_corpus(path: Path = DEFAULT_CORPUS) -> list[QualityCase]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    cases = [
        QualityCase(
            id=item["id"],
            category=item["category"],
            prompt=item["prompt"],
            max_words=int(item["max_words"]),
            must_include_any=list(item.get("must_include_any", [])),
            forbidden=list(item.get("forbidden", [])),
            requires_uncertainty=bool(item.get("requires_uncertainty", False)),
        )
        for item in raw
    ]
    if len(cases) < 30 or len({case.id for case in cases}) != len(cases):
        raise ValueError("quality corpus must contain at least 30 uniquely identified cases")
    return cases


def compose_speech(display_text: str, limit: int = 600) -> str:
    """Produce a bounded, Markdown-free speech representation."""

    text = re.sub(r"```[\s\S]*?```", " Code example omitted. ", display_text)
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = URL.sub("", text)
    text = WINDOWS_PATH.sub("", text)
    text = UNIX_PATH.sub("", text)
    text = re.sub(r"^\s{0,3}(?:#{1,6}|[-*+] |\d+\. )\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*|__([^_]+)__", lambda m: m.group(1) or m.group(2), text)
    text = re.sub(r"\*([^*]+)\*|_([^_]+)_", lambda m: m.group(1) or m.group(2), text)
    text = re.sub(r"[|#*_~]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:") + "."
    return text


def evaluate_response(case: QualityCase, response: str) -> QualityResult:
    normalized = response.strip()
    lower = normalized.lower()
    word_count = len(normalized.split())
    forbidden_clean = not any(token.lower() in lower for token in case.forbidden)
    uncertainty_ok = not case.requires_uncertainty or bool(UNCERTAINTY.search(normalized))
    speech = compose_speech(normalized)
    return QualityResult(
        directness=bool(normalized) and word_count <= case.max_words,
        grounding=forbidden_clean,
        completeness=(
            not case.must_include_any
            or any(token.lower() in lower for token in case.must_include_any)
        ),
        structure=not (normalized.startswith("{") and "JSON" not in case.prompt),
        uncertainty=uncertainty_ok,
        user_mode_cleanliness=not bool(INTERNAL.search(normalized)),
        speech_suitability=bool(speech) and not bool(MARKDOWN_MARKER.search(speech)),
    )


def build_provider(name: str, command: str | None, timeout: float) -> object:
    backend_root = ROOT / "apps/backend"
    sys.path.insert(0, str(backend_root))
    if name == "claude_code":
        from assistant.providers.claude_code import ClaudeCodeProvider

        return ClaudeCodeProvider(command=command or "claude", timeout_seconds=timeout)
    if name == "codex":
        from assistant.providers.codex import CodexProvider

        return CodexProvider(command=command or "codex", timeout_seconds=timeout)
    raise ValueError(f"unsupported provider: {name}")


async def run_provider(
    provider_name: str,
    cases: list[QualityCase],
    command: str | None,
    timeout: float,
) -> list[dict[str, Any]]:
    provider = build_provider(provider_name, command, timeout)
    from assistant.provider import AssistantRequest

    results: list[dict[str, Any]] = []
    system_context = (
        "Answer the user's actual request directly. Match requested brevity. "
        "Never invent unavailable facts or trading validation. Keep developer "
        "internals out of normal user-facing answers."
    )
    for case in cases:
        started = time.perf_counter()
        response = ""
        error: str | None = None
        try:
            reply = await provider.respond(  # type: ignore[attr-defined]
                AssistantRequest(
                    text=case.prompt,
                    conversation_id=str(uuid4()),
                    system_context=system_context,
                )
            )
            response = reply.text
        except Exception as exc:  # noqa: BLE001 - provider failures are benchmark data
            error = str(exc)
        quality = evaluate_response(case, response)
        results.append(
            {
                "case_id": case.id,
                "category": case.category,
                "prompt": case.prompt,
                "response": response,
                "speech_response": compose_speech(response),
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "error": error,
                "quality": asdict(quality),
                "checks_passed": quality.passed,
            }
        )
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--provider", choices=("claude_code", "codex"), action="append")
    parser.add_argument("--claude-command")
    parser.add_argument("--codex-command")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


async def async_main() -> int:
    args = parse_args()
    cases = load_corpus(args.corpus)
    if args.limit is not None:
        cases = cases[: args.limit]
    if not args.provider:
        print(json.dumps({"cases": len(cases), "categories": sorted({c.category for c in cases})}, indent=2))
        return 0
    all_results: dict[str, Any] = {
        "corpus": str(args.corpus.resolve()),
        "case_count": len(cases),
        "human_correctness_review_required": True,
        "providers": {},
    }
    for provider_name in args.provider:
        command = args.claude_command if provider_name == "claude_code" else args.codex_command
        responses = await run_provider(provider_name, cases, command, args.timeout)
        all_results["providers"][provider_name] = {
            "responses": responses,
            "failure_count": sum(item["error"] is not None for item in responses),
            "mean_latency_ms": round(
                sum(item["latency_ms"] for item in responses) / len(responses), 2
            ),
            "deterministic_checks_passed": sum(item["checks_passed"] for item in responses),
            "deterministic_checks_total": len(responses) * 7,
        }
    rendered = json.dumps(all_results, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)
    return 1 if any(
        data["failure_count"] for data in all_results["providers"].values()
    ) else 0


def main() -> None:
    raise SystemExit(asyncio.run(async_main()))


if __name__ == "__main__":
    main()
