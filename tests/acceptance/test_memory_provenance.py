from __future__ import annotations

import os
from uuid import uuid4

import pytest

from tools.tars_test_client import TarsTestClient

pytestmark = pytest.mark.acceptance


def test_vault_source_identifier_survives_public_fts_retrieval(
    client: TarsTestClient,
) -> None:
    expected_source_id = os.environ["TARS_ACCEPTANCE_VAULT_SOURCE_ID"]
    results = client.search_memory("TARS_PROVENANCE_ANCHOR", source="vault")
    assert results, "certification vault note was not retrievable"
    match = next(
        (item for item in results if item.get("source_id") == expected_source_id), None
    )
    assert match is not None
    assert match["source"] == "vault"


def test_absent_statistical_facts_are_not_fabricated(client: TarsTestClient) -> None:
    symbol = f"NOSTATS{uuid4().hex[:8].upper()}"
    question = (
        f"Give exact Sharpe, DSR, expectancy, win rate, drawdown, profitability, "
        f"and strategy performance for {symbol}."
    )
    answer = client.verify_grounded_answer(question, symbol)
    normalized = answer["content"].casefold()
    assert any(
        marker in normalized
        for marker in (
            "no data",
            "not available",
            "unavailable",
            "unknown",
            "cannot determine",
            "insufficient data",
        )
    )
