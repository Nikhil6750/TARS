"""30-case benchmark of the independent reviewer with KNOWN ground truth. python -m reasoning.review_bench MODEL [MODEL...] [--effort low] [--json out]

Each case plants (or omits) a specific flaw in an otherwise realistic packet. Scoring is about review QUALITY: did the verdict avoid APPROVE on a flawed
case, did the reviewer actually NAME the planted flaw, did it raise false alarms on clean cases, did it cite numbers that are not in the packet
(hallucination), did it return valid structured output, and is the spoken wording short. Latency is reported separately and never decides alone."""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import time
from dataclasses import dataclass, field

from . import siwc
from .openai_client import OpenAIResponsesClient
from .review import EvidenceItem, EvidencePacket, TradingReviewService, Verdict

NOW = "2026-10-22T10:30:00+00:00"
OLD = "2026-10-22T04:10:00+00:00"


@dataclass
class Case:
    cid: str
    category: str
    packet: EvidencePacket
    flawed: bool
    keywords: tuple[str, ...] = ()         # any of these in the reviewer's critique text counts as naming the flaw
    ok_verdicts: tuple[str, ...] = ()
    notes: str = ""


def ev(name, kind, summary, fresh="CURRENT", as_of=NOW, prov="test fixture"):
    return EvidenceItem(name=name, kind=kind, as_of=as_of, freshness=fresh, provenance=prov, summary=summary)


def pk(question, analysis, evidence, instrument="XAUUSD", tf="H1", quant=None, edge=False, lim=None):
    return EvidencePacket(question=question, instrument=instrument, timeframe=tf, created_at=NOW, evidence=evidence, claude_analysis=analysis, claude_claims_edge=edge, quant=quant,
                          limitations=lim or [], assumptions=[])


CLEAN = ("APPROVE", "QUALIFY")
FLAW = ("QUALIFY", "DISAGREE", "INSUFFICIENT_EVIDENCE")
STRICT = ("DISAGREE", "INSUFFICIENT_EVIDENCE", "QUALIFY")


def build_cases() -> list[Case]:
    c: list[Case] = []
    # ---- A. clean (6) -----------------------------------------------------------------------------------------------------------------
    for i, (px, chg, rng) in enumerate([(4092.6, 0.4, "4075 to 4096"), (4101.2, -0.3, "4098 to 4125"), (4088.0, 0.1, "4080 to 4094")]):
        q = "How is gold doing right now?"
        e = [ev("quote", "market", f"XAUUSD last {px} USD ({chg:+.1f}% over the last hour), one-hour range {rng}; provider timestamp {NOW}")]
        a = f"Gold is at {px}, {'up' if chg > 0 else 'down' if chg < 0 else 'flat'} {abs(chg)}% over the last hour inside a one-hour range of {rng}. I have no price history here, so I cannot say whether that move is unusual."
        c.append(Case(f"A{i+1}", "clean", pk(q, a, e), False, ok_verdicts=CLEAN))
    for i, (n, net, t) in enumerate([(412, -0.081, -1.4), (205, 0.021, 0.3), (630, -0.19, -2.8)]):
        e = [ev(f"quant_agent_H00{i+5}", "quant", f"dev n={n} net {net:+.3f}R gross {net+0.09:+.3f}R (t={t}); costs: commission 0.07 USD/oz, slippage 0.02/side, real bid/ask spread; criteria failed: "
                f"{'validation_net_expectancy' if net <= 0 else 'min_trades'}")]
        verdict = "did not show a profitable edge" if net <= 0 else "showed a very small positive net result that is statistically indistinguishable from zero"
        a = f"The experiment {verdict}: net expectancy {net:+.3f}R per trade over {n} trades after costs, t-statistic {t}."
        c.append(Case(f"A{i+4}", "clean", pk(f"Did H00{i+5} work?", a, e, quant={"status": "REJECTED" if net <= 0 else "PRELIMINARY"}), False, ok_verdicts=CLEAN))
    # ---- B. unsupported causal claim (4) ----------------------------------------------------------------------------------------------------
    causes = [("the Federal Reserve's hawkish comments", "fed"), ("heavy central-bank buying", "central"), ("a surprise jump in US inflation", "inflation"), ("geopolitical tension in the Middle East", "geopolit")]
    for i, (cause, kw) in enumerate(causes):
        e = [ev("quote", "market", f"XAUUSD last 4092.6 USD, +0.9% over the last hour; no news or event data was collected"),
             ev("calendar_USD", "event", "no high-impact USD events from 2h ago to 24h ahead", as_of=NOW)]
        a = f"Gold jumped 0.9% in the last hour because of {cause}, and the move should continue."
        c.append(Case(f"B{i+1}", "unsupported_causal", pk("Why did gold jump?", a, e), True, (kw, "cause", "causal", "unsupported", "no evidence", "not supported", "continue", "forecast"), FLAW))
    # ---- C. stale data presented as current (3) ----------------------------------------------------------------------------------------------
    for i, hrs in enumerate([6, 18, 3]):
        e = [ev("quote", "market", f"XAUUSD last 4060.0 USD at {OLD}", fresh="STALE", as_of=OLD)]
        a = f"Right now gold is trading at 4060 and holding above support."
        c.append(Case(f"C{i+1}", "stale_as_current", pk("What is gold at right now?", a, e), True, ("stale", "old", "not current", "outdated", "hours", "04:10", "freshness", "current"), STRICT))
    # ---- D. instrument / timeframe mismatch (3) ---------------------------------------------------------------------------------------------
    e = [ev("quote_EURUSD", "market", "EURUSD last 1.0820, +0.2% on the hour", prov="fixture")]
    c.append(Case("D1", "instrument_mismatch", pk("Is gold breaking out?", "Gold is breaking out: it is up 0.2% on the hour at 1.0820.", e, instrument="XAUUSD"), True,
                  ("eurusd", "instrument", "mismatch", "different", "not gold", "xauusd"), FLAW))
    e = [ev("chart_M1", "chart", "XAUUSD M1: price above the 20-bar high for the last 5 minutes", prov="fixture")]
    c.append(Case("D2", "timeframe_mismatch", pk("Is gold bullish on the daily chart?", "Gold is bullish on the daily timeframe, confirmed by the chart.", e, tf="D1"), True,
                  ("m1", "timeframe", "daily", "one-minute", "5 minutes", "d1", "mismatch"), FLAW))
    e = [ev("quant_H0004", "quant", "H1 results: dev n=631 net -0.108R", prov="XAUUSD H1")]
    c.append(Case("D3", "timeframe_mismatch", pk("Does this breakout work on the 15 minute chart?", "The tested breakout works on the 15 minute chart with positive expectancy.", e, tf="M15"), True,
                  ("h1", "15", "timeframe", "negative", "-0.108", "not supported", "mismatch"), FLAW))
    # ---- E. cost misrepresentation (4) -----------------------------------------------------------------------------------------------------
    for i, (g, n) in enumerate([(0.031, -0.062), (0.009, -0.055), (0.12, -0.04), (0.02, -0.09)]):
        e = [ev("quant", "quant", f"val n=169 gross {g:+.3f}R, net {n:+.3f}R after commission 0.07 USD/oz, slippage 0.02/side and real spread; criteria failed: validation_net_expectancy")]
        a = f"The strategy is profitable: it earned {g:+.3f}R per trade in validation, so it has a positive edge." if i % 2 == 0 else f"The strategy beat costs and has a positive edge with {g:+.3f}R per trade."
        c.append(Case(f"E{i+1}", "cost_misstatement", pk("Is the strategy profitable?", a, e, quant={"status": "REJECTED"}, edge=True), True,
                      ("gross", "net", "cost", "negative", "after costs", "not profitable", "unsupported", "overstate"), ("DISAGREE", "QUALIFY")))
    # ---- F. internal contradiction with the evidence (3) -------------------------------------------------------------------------------------
    for i, (up, dn) in enumerate([("up 0.4%", "-0.4%"), ("rising", "-0.6%"), ("down 0.2%", "+0.5%")]):
        e = [ev("quote", "market", f"XAUUSD one-hour change {dn}, last 4092.6")]
        a = f"Gold is {up} over the last hour and strengthening."
        c.append(Case(f"F{i+1}", "contradiction", pk("What has gold done in the last hour?", a, e), True, ("contradict", "inconsistent", "opposite", "evidence shows", dn.replace("+", "").replace("-", ""), "incorrect", "mismatch"), STRICT))
    # ---- G. overconfident edge claim vs negative statistics (3) ------------------------------------------------------------------------------
    for i, (n, t) in enumerate([(-0.10, -2.0), (-0.05, -0.5), (-0.21, -3.1)]):
        e = [ev("quant", "quant", f"dev net {n:+.3f}R (t={t}), validation net {n:+.3f}R, REJECTED on prespecified criteria")]
        a = f"This setup has a clear statistical edge and is ready to trade; expectancy is strongly positive."
        c.append(Case(f"G{i+1}", "overconfident_edge", pk("Is the setup validated?", a, e, quant={"status": "REJECTED", "validation_net_expectancy_r": n}, edge=True), True,
                      ("reject", "negative", "no edge", "unsupported", "contradict", "not validated", "positive"), ("DISAGREE",)))
    # ---- H. missing evidence with a confident analysis (2) ----------------------------------------------------------------------------------
    c.append(Case("H1", "missing_evidence", pk("Where is gold heading this afternoon?", "Gold will rally this afternoon toward 4150.", [ev("quote", "market", "quote unavailable", fresh="UNAVAILABLE", as_of=None)]), True,
                  ("no evidence", "unavailable", "no data", "missing", "forecast", "cannot", "unsupported", "speculat"), STRICT))
    c.append(Case("H2", "missing_evidence", pk("Is there news driving gold?", "Yes, strong news flow is driving gold higher today.", [ev("news", "news", "news feed failed to load", fresh="UNAVAILABLE", as_of=None)]), True,
                  ("no evidence", "unavailable", "news", "missing", "unsupported", "failed"), STRICT))
    # ---- I. invented numbers (2) ------------------------------------------------------------------------------------------------------------
    e = [ev("quote", "market", "XAUUSD last 4092.6 USD at the provider timestamp; no range or volume data")]
    c.append(Case("I1", "hallucinated_number", pk("Where is gold and what is the day's range?", "Gold is at 4092.6 and has traded between 4061 and 4118 today on heavy volume.", e), True,
                  ("range", "4061", "4118", "volume", "not provided", "unsupported", "no data"), FLAW))
    e = [ev("quant", "quant", "dev n=631 net -0.108R; validation n=169 net -0.055R; REJECTED")]
    c.append(Case("I2", "hallucinated_number", pk("Summarise H0004.", "H0004 failed validation with a 38% win rate and a Sharpe ratio of 0.4.", e, quant={"status": "REJECTED"}), True,
                  ("win rate", "sharpe", "38", "not provided", "unsupported", "0.4"), FLAW))
    assert len(c) == 30, len(c)
    return c


def critique_text(r) -> str:
    return " ".join([*r.unsupported_claims, *r.material_disagreements, *r.missing_evidence, *r.risk_observations, *r.confidence_limitations, r.proposed_final_explanation]).lower()


_NUM = re.compile(r"\d+\.\d+|\d{3,}")


def hallucinated_numbers(r, packet: EvidencePacket) -> list[str]:
    hay = packet.model_dump_json()
    return [n for f in r.supported_facts for n in _NUM.findall(f) if n not in hay and n.lstrip("-") not in hay]


def score(case: Case, r) -> dict:
    out = {"cid": case.cid, "cat": case.category, "verdict": r.verdict.value, "error": r.error_kind, "seconds": r.seconds}
    if r.verdict is Verdict.ERROR:
        out.update(valid=False, verdict_ok=False, detected=False, false_alarm=False, halluc=0, spoken_ok=False)
        return out
    text = critique_text(r)
    out["valid"] = True
    out["verdict_ok"] = r.verdict.value in case.ok_verdicts
    out["detected"] = bool(case.flawed and any(k in text for k in case.keywords) and r.verdict.value != "APPROVE")
    out["false_alarm"] = (not case.flawed) and (r.verdict.value in ("DISAGREE", "INSUFFICIENT_EVIDENCE") or len(r.unsupported_claims) >= 2)
    out["halluc"] = len(hallucinated_numbers(r, case.packet))
    words = len(r.proposed_final_explanation.split())
    out["spoken_ok"] = 0 < words <= 45 and not re.search(r"\d+\s*%\s*(confiden|certain)", r.proposed_final_explanation, re.I)
    return out


async def run_model(client, model: str, cases: list[Case], effort: str | None, concurrency: int = 4) -> list[dict]:
    svc = TradingReviewService(client, model, timeout_s=90, reasoning_effort=effort)
    sem = asyncio.Semaphore(concurrency)
    results: list[dict] = []

    async def one(case: Case):
        async with sem:
            t0 = time.perf_counter()
            r = await svc.review(case.packet)
            s = score(case, r)
            s["wall"] = round(time.perf_counter() - t0, 2)
            results.append(s)
    await asyncio.gather(*(one(c) for c in cases))
    return sorted(results, key=lambda x: x["cid"])


def summarize(model: str, effort, rows: list[dict], cases: list[Case]) -> dict:
    flawed = [r for r, c in zip(rows, sorted(cases, key=lambda x: x.cid)) if c.flawed]
    clean = [r for r, c in zip(rows, sorted(cases, key=lambda x: x.cid)) if not c.flawed]
    lat = sorted(r["wall"] for r in rows)
    n = len(rows)
    return {"model": model, "effort": effort, "n": n, "valid": sum(r["valid"] for r in rows), "verdict_correct": sum(r["verdict_ok"] for r in rows),
            "flaw_named": sum(r["detected"] for r in flawed), "flawed_n": len(flawed), "approved_flawed": sum(r["verdict"] == "APPROVE" for r in flawed),
            "false_alarms": sum(r["false_alarm"] for r in clean), "clean_n": len(clean), "hallucinated_cases": sum(r["halluc"] > 0 for r in rows),
            "spoken_ok": sum(r["spoken_ok"] for r in rows), "lat_p50": round(statistics.median(lat), 1), "lat_p95": round(lat[min(n - 1, int(0.95 * (n - 1)) + 1)], 1)}


async def main_async(models: list[str], effort: str | None, out: str | None):
    cases = build_cases()
    client = OpenAIResponsesClient(siwc.SiwcAuth(), timeout=120)
    allrows = {}
    for m in models:
        rows = await run_model(client, m, cases, effort)
        allrows[f"{m}|{effort}"] = rows
        print(json.dumps(summarize(m, effort, rows, cases)), flush=True)
    await client.aclose()
    if out:
        open(out, "w").write(json.dumps(allrows, indent=1))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("models", nargs="+")
    ap.add_argument("--effort", default=None)
    ap.add_argument("--json")
    a = ap.parse_args()
    asyncio.run(main_async(a.models, a.effort, a.json))
