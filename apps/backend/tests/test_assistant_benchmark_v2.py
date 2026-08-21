from __future__ import annotations

from assistant.provider import AssistantRequest, render_provider_prompt
from benchmarks.assistant_provider_benchmark import (
    REQUIRED_CATEGORIES,
    CorpusCase,
    load_corpus,
    score_rubric,
    summarize,
)


def test_fixed_corpus_has_30_cases_and_all_required_categories():
    cases = load_corpus()
    assert len(cases) == 30
    assert {case.category for case in cases} == REQUIRED_CATEGORIES
    assert any(case.history for case in cases if case.category == "follow_ups")


def test_completeness_requires_every_concept_group_not_one_anchor():
    case = CorpusCase(
        id="rubric",
        category="reasoning",
        prompt="Explain the failure and fix",
        max_words=50,
        required_concepts=(("duplicate",), ("idempotency key",)),
    )

    assert score_rubric(case, "It can create a duplicate charge.").correctness is False
    assert score_rubric(
        case,
        "It can create a duplicate charge. Use an idempotency key.",
    ).correctness is True


def test_followup_history_is_rendered_for_stateless_cli_providers():
    prompt = render_provider_prompt(
        AssistantRequest(
            text="What should I verify instead?",
            conversation_id="c1",
            history=[
                {"role": "user", "content": "Is HTTP 200 enough?"},
                {"role": "assistant", "content": "No, verify readiness separately."},
            ],
        )
    )
    assert "Relevant conversation context" in prompt
    assert "verify readiness separately" in prompt
    assert prompt.endswith("What should I verify instead?")


def test_failed_cases_score_zero_in_provider_summary():
    quality = {
        "directness": True,
        "completeness": True,
        "grounding": True,
        "uncertainty": True,
        "structure": True,
        "user_mode_cleanliness": True,
        "speech_suitability": True,
        "issues": (),
    }
    rubric = {
        "correctness": True,
        "instruction_following": True,
        "hallucination_free": True,
        "structure": True,
    }
    summary = summarize(
        [
            {
                "failed": True,
                "latency_ms": 10.0,
                "category": "simple_questions",
                "before": {"quality": quality, "rubric": rubric},
                "after": {"quality": quality, "rubric": rubric},
            }
        ]
    )
    assert summary["failure_rate_pct"] == 100.0
    assert summary["after"]["quality_pct"] == 0.0
    assert summary["after"]["correctness_pct"] == 0.0
