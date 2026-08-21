"""Run the isolated fixed-corpus Claude Code/Codex quality benchmark.

Each prompt is sent exactly once to each requested provider. Deterministic
rubrics score both the raw provider answer ("before") and the mechanically
composed display/speech answer ("after"). No model judges another model.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from assistant.provider import AssistantProvider, AssistantRequest
from assistant.providers.claude_code import ClaudeCodeProvider
from assistant.providers.codex import CodexProvider
from assistant.response_quality import (
    QUALITY_SYSTEM_PROMPT,
    ResponseComposer,
    ResponseQualityContract,
    prepare_speech_text,
)

CORPUS_PATH = Path(__file__).with_name("quality_corpus.json")
REQUIRED_CATEGORIES = {
    "simple_questions",
    "reasoning",
    "coding",
    "debugging",
    "trading_epistemics",
    "insufficient_evidence",
    "structured_answers",
    "short_answer_requests",
    "complex_requests",
    "follow_ups",
}
_UNCERTAINTY = re.compile(
    r"\b(can(?:not|'t)|do not have|don't have|not provided|insufficient|"
    r"incomplete|unavailable|unknown|need (?:the|more)|no validated trade)\b",
    re.IGNORECASE,
)
_UNSUPPORTED_CERTAINTY = re.compile(
    r"\b(?:guaranteed|definitely)\b|\b\d{1,3}\s*%\s*(?:confidence|certain)",
    re.IGNORECASE,
)
_LIST_ITEM = re.compile(r"(?m)^\s*(?:[-*+]\s+|\d+[.)]\s+)")
_SENTENCE = re.compile(r"(?<!\b[A-Z])(?:[.!?]+)(?:\s+|$)")


@dataclass(frozen=True)
class CorpusCase:
    id: str
    category: str
    prompt: str
    max_words: int
    required_concepts: tuple[tuple[str, ...], ...]
    history: tuple[dict[str, str], ...] = ()
    forbidden: tuple[str, ...] = ()
    requires_uncertainty: bool = False
    required_headings: tuple[str, ...] = ()
    min_list_items: int | None = None
    exact_list_items: int | None = None
    max_sentences: int | None = None
    requires_code: bool = False


@dataclass(frozen=True)
class RubricScore:
    correctness: bool
    instruction_following: bool
    hallucination_free: bool
    structure: bool


def load_corpus(path: Path = CORPUS_PATH) -> list[CorpusCase]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    cases = [
        CorpusCase(
            id=item["id"],
            category=item["category"],
            prompt=item["prompt"],
            max_words=int(item["max_words"]),
            required_concepts=tuple(tuple(group) for group in item["required_concepts"]),
            history=tuple(item.get("history", [])),
            forbidden=tuple(item.get("forbidden", [])),
            requires_uncertainty=bool(item.get("requires_uncertainty", False)),
            required_headings=tuple(item.get("required_headings", [])),
            min_list_items=item.get("min_list_items"),
            exact_list_items=item.get("exact_list_items"),
            max_sentences=item.get("max_sentences"),
            requires_code=bool(item.get("requires_code", False)),
        )
        for item in raw
    ]
    categories = {case.category for case in cases}
    if len(cases) != 30 or len({case.id for case in cases}) != 30:
        raise ValueError("benchmark corpus must contain exactly 30 unique cases")
    if categories != REQUIRED_CATEGORIES:
        raise ValueError(f"benchmark categories differ: {sorted(categories ^ REQUIRED_CATEGORIES)}")
    return cases


def _normalize_text(text: str) -> str:
    return text.lower().replace("’", "'").replace("×", "x")


def score_rubric(case: CorpusCase, response: str) -> RubricScore:
    normalized = _normalize_text(response)
    concepts_ok = all(
        any(_normalize_text(term) in normalized for term in group)
        for group in case.required_concepts
    )
    forbidden_ok = not any(_normalize_text(term) in normalized for term in case.forbidden)
    uncertainty_ok = not case.requires_uncertainty or bool(_UNCERTAINTY.search(normalized))
    hallucination_free = forbidden_ok and uncertainty_ok and not bool(
        _UNSUPPORTED_CERTAINTY.search(normalized)
    )

    list_items = len(_LIST_ITEM.findall(response))
    headings_ok = all(
        re.search(rf"(?im)^\s*(?:#+\s*)?{re.escape(heading)}\s*:?(?:\s|$)", response)
        for heading in case.required_headings
    )
    list_ok = case.min_list_items is None or list_items >= case.min_list_items
    exact_list_ok = case.exact_list_items is None or list_items == case.exact_list_items
    code_ok = not case.requires_code or "```" in response
    structure = bool(response.strip()) and headings_ok and list_ok and exact_list_ok and code_ok

    word_ok = len(response.split()) <= case.max_words
    sentence_count = len(_SENTENCE.findall(re.sub(r"```[\s\S]*?```", "", response)))
    sentence_ok = case.max_sentences is None or sentence_count <= case.max_sentences
    instruction_following = word_ok and sentence_ok and structure
    return RubricScore(
        correctness=concepts_ok and forbidden_ok,
        instruction_following=instruction_following,
        hallucination_free=hallucination_free,
        structure=structure,
    )


def build_provider(name: str, timeout: float) -> AssistantProvider:
    if name == "claude_code":
        return ClaudeCodeProvider(timeout_seconds=timeout, persist_sessions=False)
    if name == "codex":
        return CodexProvider(timeout_seconds=timeout)
    raise ValueError(f"unsupported benchmark provider: {name}")


async def run_case(
    provider: AssistantProvider,
    case: CorpusCase,
) -> dict[str, Any]:
    started = time.perf_counter()
    raw_response = ""
    error_type: str | None = None
    try:
        reply = await provider.respond(
            AssistantRequest(
                text=case.prompt,
                conversation_id=str(uuid4()),
                system_context=QUALITY_SYSTEM_PROMPT,
                history=list(case.history),
            )
        )
        raw_response = reply.text.strip()
    except Exception as exc:  # noqa: BLE001 - failures are benchmark measurements
        error_type = type(exc).__name__
    latency_ms = round((time.perf_counter() - started) * 1000, 2)

    composer = ResponseComposer()
    composed = composer.compose(
        user_text=case.prompt,
        display_text=raw_response,
        grounding_context=QUALITY_SYSTEM_PROMPT,
    ) if raw_response else None
    contract = ResponseQualityContract()
    raw_quality = contract.assess(
        user_text=case.prompt,
        display_text=raw_response,
        speech_text=prepare_speech_text(raw_response),
        grounding_context=QUALITY_SYSTEM_PROMPT,
    )
    raw_rubric = score_rubric(case, raw_response)
    after_text = composed.display_text if composed is not None else ""
    after_rubric = score_rubric(case, after_text)
    return {
        "case_id": case.id,
        "category": case.category,
        "prompt": case.prompt,
        "latency_ms": latency_ms,
        "failed": error_type is not None,
        "error_type": error_type,
        "raw_response": raw_response,
        "display_text": after_text,
        "speech_text": composed.speech_text if composed is not None else "",
        "before": {
            "quality": raw_quality.to_dict(),
            "rubric": asdict(raw_rubric),
        },
        "after": {
            "quality": composed.quality.to_dict() if composed is not None else raw_quality.to_dict(),
            "rubric": asdict(after_rubric),
        },
    }


def _percentage(numerator: float, denominator: float) -> float:
    return round((numerator / denominator) * 100, 2) if denominator else 0.0


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(results)
    failures = sum(item["failed"] for item in results)
    latencies = [item["latency_ms"] for item in results]
    successful_latencies = [item["latency_ms"] for item in results if not item["failed"]]

    def phase_summary(phase: str) -> dict[str, float]:
        quality_passed = sum(
            0 if item["failed"] else sum(
                bool(item[phase]["quality"][key])
                for key in (
                    "directness",
                    "completeness",
                    "grounding",
                    "uncertainty",
                    "structure",
                    "user_mode_cleanliness",
                    "speech_suitability",
                )
            )
            for item in results
        )
        return {
            "quality_pct": _percentage(quality_passed, count * 7),
            "correctness_pct": _percentage(
                sum(
                    not item["failed"] and item[phase]["rubric"]["correctness"]
                    for item in results
                ),
                count,
            ),
            "instruction_following_pct": _percentage(
                sum(
                    not item["failed"] and item[phase]["rubric"]["instruction_following"]
                    for item in results
                ),
                count,
            ),
            "hallucination_free_pct": _percentage(
                sum(
                    not item["failed"] and item[phase]["rubric"]["hallucination_free"]
                    for item in results
                ),
                count,
            ),
            "structure_pct": _percentage(
                sum(
                    not item["failed"] and item[phase]["rubric"]["structure"]
                    for item in results
                ),
                count,
            ),
            "overall_quality_pct": _percentage(
                quality_passed
                + sum(
                    not item["failed"] and bool(value)
                    for item in results
                    for value in item[phase]["rubric"].values()
                ),
                count * 11,
            ),
        }

    categories: dict[str, dict[str, float]] = {}
    for category in sorted({item["category"] for item in results}):
        subset = [item for item in results if item["category"] == category]
        categories[category] = {
            "case_count": len(subset),
            "failure_rate_pct": _percentage(sum(item["failed"] for item in subset), len(subset)),
            "after_correctness_pct": _percentage(
                sum(
                    not item["failed"] and item["after"]["rubric"]["correctness"]
                    for item in subset
                ),
                len(subset),
            ),
        }

    return {
        "case_count": count,
        "failure_count": failures,
        "failure_rate_pct": _percentage(failures, count),
        "attempt_mean_latency_ms": round(statistics.fmean(latencies), 2) if latencies else None,
        "success_mean_latency_ms": (
            round(statistics.fmean(successful_latencies), 2) if successful_latencies else None
        ),
        "success_p50_latency_ms": (
            round(statistics.median(successful_latencies), 2) if successful_latencies else None
        ),
        "before": phase_summary("before"),
        "after": phase_summary("after"),
        "categories": categories,
    }


async def run_benchmark(
    provider_names: list[str],
    cases: list[CorpusCase],
    timeout: float,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "methodology": {
            "fixed_corpus": str(CORPUS_PATH.resolve()),
            "single_call_per_case": True,
            "isolated_non_repository_working_directory": True,
            "deterministic_rubric": True,
            "second_llm_judge": False,
            "rubric_version": 2,
        },
        "providers": {},
        "conclusion": (
            "This run reports measured behavior only. It does not establish a permanent "
            "provider winner; routing must continue to consider task, capability, health, and latency."
        ),
    }
    for name in provider_names:
        provider = build_provider(name, timeout)
        results = [await run_case(provider, case) for case in cases]
        report["providers"][name] = {
            "summary": summarize(results),
            "results": results,
        }
    return report


def rescore_report(report: dict[str, Any], cases: list[CorpusCase]) -> dict[str, Any]:
    """Reapply deterministic rubrics to saved raw answers without new model calls."""

    case_by_id = {case.id: case for case in cases}
    composer = ResponseComposer()
    contract = ResponseQualityContract()
    for provider_data in report["providers"].values():
        results = provider_data["results"]
        for item in results:
            case = case_by_id[item["case_id"]]
            raw = item.get("raw_response", "")
            raw_quality = contract.assess(
                user_text=case.prompt,
                display_text=raw,
                speech_text=prepare_speech_text(raw),
                grounding_context=QUALITY_SYSTEM_PROMPT,
            )
            composed = composer.compose(
                user_text=case.prompt,
                display_text=raw,
                grounding_context=QUALITY_SYSTEM_PROMPT,
            ) if raw else None
            item["display_text"] = composed.display_text if composed is not None else ""
            item["speech_text"] = composed.speech_text if composed is not None else ""
            item["before"] = {
                "quality": raw_quality.to_dict(),
                "rubric": asdict(score_rubric(case, raw)),
            }
            item["after"] = {
                "quality": (
                    composed.quality.to_dict() if composed is not None else raw_quality.to_dict()
                ),
                "rubric": asdict(
                    score_rubric(case, composed.display_text if composed is not None else "")
                ),
            }
        provider_data["summary"] = summarize(results)
    report["methodology"]["rubric_version"] = 2
    report["methodology"]["rescored_from_saved_raw_responses"] = True
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", action="append", choices=("claude_code", "codex"))
    parser.add_argument("--corpus", type=Path, default=CORPUS_PATH)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--rescore", type=Path)
    return parser.parse_args()


async def async_main() -> int:
    args = parse_args()
    cases = load_corpus(args.corpus)
    if args.limit is not None:
        cases = cases[: args.limit]
    providers = args.provider or []
    if args.rescore:
        report = rescore_report(
            json.loads(args.rescore.read_text(encoding="utf-8")),
            cases,
        )
        destination = args.output or args.rescore
        destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {
                    "methodology": report["methodology"],
                    "providers": {
                        name: data["summary"] for name, data in report["providers"].items()
                    },
                    "conclusion": report["conclusion"],
                },
                indent=2,
            )
        )
        return 0
    if not providers:
        print(json.dumps({"case_count": len(cases), "categories": sorted({c.category for c in cases})}, indent=2))
        return 0
    report = await run_benchmark(providers, cases, args.timeout)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "methodology": report["methodology"],
                "providers": {
                    name: data["summary"] for name, data in report["providers"].items()
                },
                "conclusion": report["conclusion"],
            },
            indent=2,
        )
    )
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(async_main()))


if __name__ == "__main__":
    main()
