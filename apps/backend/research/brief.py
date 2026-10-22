"""Researcher brief: everything Claude (lead quant researcher) needs to propose ONE mechanism-first hypothesis as a ResearchSpec,
and nothing about performance. No backtest result of any kind is included or computed."""
from __future__ import annotations

import json

from .cost_baseline import baseline_cost_model
from .demo import demo_spec_dict
from .instruments import get_meta
from .registry import ExperimentRegistry
from .spec import FEATURE_KINDS, OPS, REGIME_DIMENSIONS, SESSION_NAMES

RULES = """RESEARCH RULES (non-negotiable)
1. Start from a MARKET MECHANISM: name the participants/flows that create the opportunity, why they act that way (constraint,
   incentive, institutional schedule, inventory/liquidity effect, information diffusion), why it persists despite being known,
   when it should exist and when it should vanish. Only THEN express it as an objective rule. Do not start from an indicator.
2. You have seen NO performance data and must not guess any. Choose parameter values from the mechanism (natural horizons such as
   session lengths, round numbers), state why each is natural, and DO NOT tune. One rule, a few parameters, no indicator stacks.
3. Falsifiable: say in `hypothesis` what outcome would show the idea is wrong (rejection criteria must match).
4. Declare any parameter variants for the stability check up front (at most 4); they are reported, never used to pick parameters.
5. The rule must trade often enough to be testable: expect several hundred trades in the development period; set
   rejection.min_trades_per_period >= 100.
6. Costs: use AT LEAST the baseline below (real bid/ask spread is applied on top automatically). Include stress tests.
7. Use the periods given verbatim. Never reference the FINAL window beyond declaring it as final_untouched.
8. Output ONE JSON object only (schema in the system prompt). `spec` must be a complete ResearchSpec. diagnostic=false."""


def build_brief(split: dict, registry: ExperimentRegistry, instrument: str = "XAUUSD") -> str:
    meta = get_meta(instrument)
    base = baseline_cost_model(instrument)
    example = demo_spec_dict("dukascopy")
    example["development"] = {"start": split["development"][0], "end": split["development"][1]}
    example["validation"] = {"start": split["validation"][0], "end": split["validation"][1]}
    example["final_untouched"] = {"start": split["final_untouched"][0], "end": split["final_untouched"][1]}
    example.pop("untouched_test", None)
    example["diagnostic"] = False
    example["signal"]["entry_sessions"] = None
    reg = [{"id": r["id"], "title": r["title"][:80], "status": r["status"], "diagnostic": bool(r["diagnostic"])} for r in registry.list()]
    parts = [
        RULES,
        f"INSTRUMENT: {meta.instrument} ({meta.asset_class}), spot gold in USD per troy oz, tick {meta.tick_size}, 'pip' {meta.pip_size} USD. "
        f"Trades Sun 18:00 - Fri 17:00 New York with a daily 17:00-18:00 NY break; data is M1 bid+ask (Dukascopy) 2020-01 to 2026-09.",
        f"COST BASELINE (price units, USD/oz): commission_round_turn >= {base.commission_round_turn}, slippage_per_side >= {base.slippage_per_side}, "
        f"latency_bars {base.latency_bars}, financing disabled (intraday placeholder). Observed median spread in a sample is about 0.54 USD/oz (~5 pips), "
        "wider at rollover/news; the engine charges the REAL bid/ask spread at every fill.",
        "PERIODS (use exactly): " + json.dumps({k: split[k] for k in ("development", "validation", "final_untouched")}),
        "ENGINE VOCABULARY (closed): features kinds " + ", ".join(sorted(FEATURE_KINDS)) + " (n = lookback in BARS of data.timeframe; "
        "rolling_high/low exclude the current bar; all features are causal). Conditions: {left: <feature name>, op: one of " + ", ".join(sorted(OPS)) +
        ", right: <number or feature name>}, ANDed within long_when / short_when. signal.entry_sessions optionally restricts entries to DST-aware sessions "
        "among " + ", ".join(sorted(SESSION_NAMES)) + " (ASIA=Tokyo 09-18, LONDON=London 08-16:30, NEWYORK=NY 08-17, LONDON_NY_OVERLAP=both; local clocks). "
        "Entry = next bar open at the real ask (long) / bid (short). Exit: stop = exit.stop_atr_mult x ATR (feature named in exit.atr_feature), optional "
        "target_r, hard max_hold_bars. One position at a time. data.timeframe may be M5, M15, M30, H1, H4 (derived from M1). regime_tests dims: "
        + ", ".join(sorted(REGIME_DIMENSIONS)) + ". If the mechanism needs something this vocabulary cannot express, say so in `reasoning` and choose the closest "
        "faithful rule rather than abusing indicators.",
        "REGISTRY SO FAR (all entries are pipeline diagnostics, not research): " + json.dumps(reg),
        "SPEC FORMAT EXAMPLE (field names/shape ONLY - its content is a meaningless pipeline check, do not copy ideas from it): " + json.dumps(example),
    ]
    return "\n\n".join(parts)
