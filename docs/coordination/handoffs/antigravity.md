# Handoff — Antigravity

Owned directory: `apps/web/`. Only Antigravity edits this file. See
[AGENTS.md](../../../AGENTS.md) for the full handoff protocol.

Update this file at the end of every session working on `apps/web/`, using
the template below. Keep only the latest handoff at the top; older entries
may be kept below a `---` separator for history, but are not required
reading for the next session (`CURRENT_STATE.md` is authoritative for that).

---

## Latest handoff (TRADING INTELLIGENCE AUDIT & PROVENANCE VERIFICATION)

**Status**: COMPLETE — Completed comprehensive audit and verification of trading intelligence architecture, enforced strict fail-closed real retrieval for market research, separated 6 explicit conceptual routing domains, resolved test parameterization skews, eliminated raw markdown asterisks in web/desktop UI, and verified full 520-test backend suite.
**Branch**: `feature/trading-intelligence-architecture`
**Commit SHA**: `44faf8f210113e6f0cebaa72470969600bbc0fcf`
**Work completed**:
1. **Market Research Real Retrieval & Provenance Enforcement**:
   - Audited `MarketResearchEngine` (`apps/backend/intelligence/market_research.py`).
   - Implemented `MarketEvidence` dataclass preserving `source`, `retrieval_timestamp`, `publication_timestamp`, `url` / `source_id`, and `evidence_text` / `value`.
   - Enforced fail-closed behavior: without real retrieval feeds configured, returns `CURRENT RESEARCH UNAVAILABLE` and explicitly names the missing integration (`Live Macro/News Retrieval Provider (e.g. FRED, Financial Modeling Prep, Finnhub, or Live Economic Calendar API)`). Never relies on Claude model memory for current Fed stance, macro releases, yields, or DXY.
2. **Explicit 6-Domain Routing**:
   - Verified and implemented 6 distinct conceptual domains in `IntentKind` (`apps/backend/intelligence/router.py` & `apps/backend/assistant/router.py`):
     1. `MARKET_RESEARCH`: Macro drivers, catalysts, and cross-asset context (fail-closed without real retrieval).
     2. `CHART_ANALYSIS`: Native chart capture and institutional structure/scenario formatting.
     3. `STRATEGY_EVALUATION`: Deterministic quant_brain setup evaluation; returns `NO VALIDATED TRADE` when unvalidated.
     4. `TRADE_CALCULATION`: Deterministic math for capital, risk, position sizing, and profit projections; rejects calculation without parameters.
     5. `GENERAL_CHAT`: General dialogue; strictly rejects numerical confidence percentages.
     6. `DEVELOPER_REQUEST`: Repository and internal tooling queries.
3. **Institutional Chart Formatting**:
   - Updated `IntelligenceComposer.format_chart_response` (`apps/backend/intelligence/composer.py`) to generate all 8 required analytical sections: `Market State`, `Structure`, `Key Levels`, `Bullish Scenario`, `Bearish Scenario`, `Invalidation`, `Trade Status` (`NO VALIDATED TRADE` indicator), and `Action`.
4. **Markdown Rendering & Native UI**:
   - Overhauled `apps/web/src/components/assistant/MarkdownContent.tsx` with an inline parser (`renderInline`) that parses bold (`**text**`), italic (`*text*`), code (`` `code` ``), and links into native React elements, preventing raw asterisk leakage.
5. **Full Backend Test Suite Execution**:
   - Fixed dynamic time offset parameterization in `apps/backend/tests/test_action_runtime.py`.
   - Fixed `direction` NoneType handling in `apps/backend/intelligence/strategy_evaluation.py`.
   - Executed full backend pytest suite: **520 passed, 0 failed, 0 skipped** across all 58 test files.
   - Frontend TypeScript check (`npm run typecheck`): **0 errors**.

**Files changed**:
- `apps/backend/assistant/router.py`
- `apps/backend/intelligence/composer.py`
- `apps/backend/intelligence/market_research.py`
- `apps/backend/intelligence/router.py`
- `apps/backend/intelligence/strategy_evaluation.py`
- `apps/backend/tests/test_action_runtime.py`
- `apps/backend/tests/test_trading_intelligence.py`
- `apps/web/src/components/assistant/MarkdownContent.tsx`
- `apps/web/vitest.config.ts`
- `docs/coordination/handoffs/antigravity.md`

**Interfaces exposed**:
- `MarketEvidence`, `MarketResearchReport`, `MarketResearchEngine`, `StrategyEvaluationEngine.evaluate_entry_decision()`, `IntelligenceComposer.format_chart_response()`

**Tests run**:
- Backend full suite: `python -m pytest apps/backend/tests -q` -> 520 passed in 478s (100%).
- Frontend TypeScript check: `npm run typecheck` in `apps/web` -> 0 errors.

**Known limitations**:
- Live macro news feeds (FRED, Finnhub, etc.) require external API integration to supply real-time `MarketEvidence` objects.
- Physical screenshot verification of the native desktop window is marked `PHYSICAL VERIFICATION REQUIRED` when the Tauri executable is not running in the active desktop session.

**Exact dependencies required from other agents**:
- `claude.md`: Integration of real external macroeconomic feeds / calendar API into `MarketResearchEngine.retrieval_provider`.
- `codex.md`: Verification against acceptance test suites A through E.

**Next recommended action**:
- Hand off to coordinator for cross-agent evaluation. Do not merge into `integration/v1` or `main`.
