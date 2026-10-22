from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from reasoning.cache import ResearchAnswerCache, cache_key, normalize_question
from reasoning.collectors import detect_instrument, experiment_collector, public_quote, tars_state_collector, upcoming_events
from reasoning.conversation import TRUTHFUL_AUTH, TRUTHFUL_FAILURE, Candidate, ConversationService
from reasoning.decision import DecisionEngine
from reasoning.openai_client import ApiKeyAuth, OpenAIAuthError, OpenAIResponsesClient, OpenAIUnavailable
from reasoning.pipeline import IntelligencePipeline
from reasoning.review import EvidenceItem, EvidencePacket, ReviewResult, TradingReviewService, Verdict
from reasoning.service import IntelligenceService

NOW = datetime(2026, 10, 22, 10, 30, tzinfo=timezone.utc)


def item(name="quant_agent_H0004", kind="quant", immutable=True, fresh="CURRENT", summary="dev net -0.108R; REJECTED"):
    return EvidenceItem(name=name, kind=kind, as_of="2026-10-22T09:00:00+00:00", freshness=fresh, provenance="Quant Agent; spec abc", summary=summary, immutable=immutable)


def sse_text(text: str) -> bytes:
    return (f'event: response.output_text.delta\ndata: {json.dumps({"type": "response.output_text.delta", "delta": text})}\n\n'
            'event: response.completed\ndata: {"type":"response.completed","response":{"usage":{}}}\n\n').encode()


REVIEW_JSON = json.dumps({"verdict": "QUALIFY", "supported_facts": ["net -0.108R"], "confidence_limitations": ["financing not modelled"],
                          "proposed_final_explanation": "No. It failed validation, although financing was not modelled."})


# ============================ immutable research cache ===========================================================================
def test_only_fully_immutable_reviewed_evidence_is_cacheable(tmp_path):
    assert cache_key("did h0004 work", [item()], "m") is not None
    assert cache_key("x", [], "m") is None
    assert cache_key("x", [item(), item("quote", "market", immutable=False)], "m") is None            # any mutable evidence disables caching
    assert cache_key("x", [item(immutable=False)], "m") is None


def test_cache_key_pins_question_evidence_and_model():
    a = cache_key("Did H0004 work?", [item()], "m1")
    assert a == cache_key("did  h0004   WORK", [item()], "m1") and normalize_question("Did H0004 work?!") == "did h0004 work"
    assert a != cache_key("Did H0004 work?", [item()], "m2")
    assert a != cache_key("Did H0004 work?", [item(summary="dev net -0.200R; REJECTED")], "m1")           # changed result -> different key
    assert a != cache_key("Did H0004 work?", [EvidenceItem(**{**item().model_dump(), "provenance": "Quant Agent; spec DIFFERENT"})], "m1")


async def _pipe(tmp_path, *, review=None, claude_calls=None, evid=None, model="m"):
    claude_calls = claude_calls if claude_calls is not None else []
    review_calls = []

    async def claude(packet):
        claude_calls.append(1)
        return {"text": "H0004 failed validation.", "claims_edge": False, "quant": {"experiment_id": "H0004", "status": "REJECTED", "validation_net_expectancy_r": -0.05, "net_expectancy_r": -0.1}}

    async def llm(system, user, cancel):
        review_calls.append(1)
        if review == "fail":
            raise OpenAIAuthError("signed out")
        return REVIEW_JSON, {"model": "gpt-test"}

    async def collector(q):
        return evid or item()
    p = IntelligencePipeline(collectors={"x": collector}, claude=claude, reviewer=TradingReviewService(llm=llm), cache=ResearchAnswerCache(tmp_path), cache_model=model)
    return p, claude_calls, review_calls


async def _final(p, q="Did H0004 survive validation?"):
    return [e async for e in p.stream(q, instrument="XAUUSD")][-1]


async def test_second_identical_research_question_is_served_from_cache_without_model_calls(tmp_path):
    p, claude_calls, review_calls = await _pipe(tmp_path)
    first = await _final(p)
    assert first.data.get("cached") is None and len(claude_calls) == 1 and len(review_calls) == 1 and first.data["independently_reviewed"]
    t = time.perf_counter()
    second = await _final(p)
    assert second.data["cached"] is True and "CACHED" in second.data["flags"] and second.text == first.text
    assert len(claude_calls) == 1 and len(review_calls) == 1 and time.perf_counter() - t < 0.5 and p.calls["cache_hit"] == 1
    assert second.data["independently_reviewed"] is True and second.data["status"] == first.data["status"]


async def test_changed_evidence_or_model_never_reuses_a_cached_answer(tmp_path):
    p, claude_calls, _ = await _pipe(tmp_path)
    await _final(p)
    p2, c2, _ = await _pipe(tmp_path, evid=item(summary="dev net -0.300R; REJECTED"))
    await _final(p2)
    assert len(c2) == 1                                                       # different evidence -> fresh Claude call
    p3, c3, _ = await _pipe(tmp_path, model="another-model")
    await _final(p3)
    assert len(c3) == 1


async def test_unreviewed_answers_are_never_cached(tmp_path):
    p, claude_calls, _ = await _pipe(tmp_path, review="fail")
    f1 = await _final(p)
    assert f1.data["status"] == "UNREVIEWED"
    f2 = await _final(p)
    assert len(claude_calls) == 2 and not f2.data.get("cached") and not list(tmp_path.glob("*.json"))


async def test_mutable_market_evidence_is_never_cached(tmp_path):
    p, claude_calls, _ = await _pipe(tmp_path, evid=item("quote", "market", immutable=False, summary="XAUUSD 4092.6"))
    await _final(p, "what is gold doing right now")
    await _final(p, "what is gold doing right now")
    assert len(claude_calls) == 2 and not list(tmp_path.glob("*.json"))


# ============================ conversation path ===================================================================================
def cand(name, chunks=(), exc=None, delay=0.0, hang=False):
    async def stream(text, history):
        if hang:
            await asyncio.sleep(30)
        if delay:
            await asyncio.sleep(delay)
        if exc is not None:
            raise exc
        for c in chunks:
            yield c
    return Candidate(name, stream)


async def say(svc, text="hi"):
    return "".join([c async for c in svc.stream(text)])


async def test_conversation_uses_the_first_working_candidate_only():
    calls = []
    a = cand("fast", ["Hello ", "there."])

    async def never(text, history):
        calls.append(1)
        yield "x"
    svc = ConversationService([a, Candidate("slow", never)])
    assert await say(svc) == "Hello there." and svc.last_model == "fast" and calls == [] and svc.attempts == [("fast", "ok")]


async def test_conversation_falls_back_on_transient_failure_before_any_text():
    svc = ConversationService([cand("a", exc=OpenAIUnavailable("down")), cand("b", ["From B."])])
    assert await say(svc) == "From B." and svc.attempts[0] == ("a", "OpenAIUnavailable") and svc.last_model == "b"


async def test_conversation_first_token_timeout_moves_on():
    svc = ConversationService([cand("hung", hang=True), cand("b", ["ok"])], first_token_timeout_s=0.2)
    t = time.perf_counter()
    assert await say(svc) == "ok" and time.perf_counter() - t < 2


async def test_all_candidates_failing_gives_a_truthful_line_not_an_invented_answer():
    svc = ConversationService([cand("a", exc=OpenAIUnavailable("x")), cand("b", exc=OSError("net"))])
    assert await say(svc) == TRUTHFUL_FAILURE and "can't reach" in TRUTHFUL_FAILURE


async def test_auth_failure_says_sign_in_again_and_does_not_try_other_models():
    b_calls = []

    async def b(text, history):
        b_calls.append(1)
        yield "x"
    svc = ConversationService([cand("a", exc=OpenAIAuthError("revoked")), Candidate("b", b)])
    assert await say(svc) == TRUTHFUL_AUTH and b_calls == []


async def test_interrupted_stream_never_splices_a_second_models_words_onto_the_first():
    async def partial(text, history):
        yield "Half an answ"
        raise OpenAIUnavailable("stream dropped")
    svc = ConversationService([Candidate("a", partial), cand("b", ["Different answer."])])
    out = await say(svc)
    assert out == "Half an answ" and "Different" not in out and svc.history == []


async def test_conversation_keeps_a_bounded_history():
    seen = []

    async def echo(text, history):
        seen.append(list(history))
        yield f"r{len(history)}"
    svc = ConversationService([Candidate("a", echo)], max_history=4)
    for i in range(6):
        await say(svc, f"q{i}")
    assert len(seen[-1]) == 4 and seen[0] == []


# ============================ collectors =========================================================================================
def gold_client(price=4092.6, age_s=20, status=200):
    updated = (NOW - timedelta(seconds=age_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(status, json={"price": price, "updatedAt": updated, "symbol": "XAU"})))


async def test_public_quote_current_stale_unavailable_and_unsupported():
    cur = await public_quote("XAUUSD", client=gold_client(age_s=30), now=NOW)
    assert cur.freshness == "CURRENT" and "4092.60" in cur.summary and "no bid/ask" in cur.provenance and cur.as_of.startswith("2026-10-22T10:29")
    assert (await public_quote("XAUUSD", client=gold_client(age_s=900), now=NOW)).freshness == "STALE"
    assert (await public_quote("XAUUSD", client=gold_client(status=500), now=NOW)).freshness == "UNAVAILABLE"
    assert (await public_quote("EURUSD", now=NOW)).freshness == "UNAVAILABLE"


async def test_calendar_collector_reports_events_in_window_or_honest_absence():
    rows = [{"title": "US CPI", "country": "USD", "impact": "High", "date": (NOW + timedelta(hours=3)).isoformat(), "forecast": "0.3%"},
            {"title": "UK GDP", "country": "GBP", "impact": "High", "date": (NOW + timedelta(hours=2)).isoformat()},
            {"title": "Minor", "country": "USD", "impact": "Low", "date": (NOW + timedelta(hours=1)).isoformat()},
            {"title": "Next week", "country": "USD", "impact": "High", "date": (NOW + timedelta(days=5)).isoformat()}]
    c = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=rows)))
    e = await upcoming_events("USD", client=c, now=NOW)
    assert e.freshness == "CURRENT" and "US CPI" in e.summary and "UK GDP" not in e.summary and "Minor" not in e.summary and "Next week" not in e.summary
    none = await upcoming_events("USD", client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[]))), now=NOW)
    assert "no high-impact" in none.summary
    bad = await upcoming_events("USD", client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))), now=NOW)
    assert bad.freshness == "UNAVAILABLE"


def test_instrument_detection():
    assert detect_instrument("what is gold doing") == "XAUUSD" and detect_instrument("is bitcoin up") == "BTCUSD" and detect_instrument("tell me a joke") is None
    assert detect_instrument("EURUSD outlook") == "EURUSD"


async def test_experiment_collector_only_fires_for_experiment_ids(tmp_path):
    called = []

    class Reg:
        pass
    col = experiment_collector(Reg(), object())
    assert await col("tell me about gold") is None
    res = await col("how did H0099 do")
    assert res.freshness == "UNAVAILABLE" and "H0099" in res.name                                   # unknown id -> honest unavailable, not a crash


async def test_tars_state_collector_reports_each_source_with_its_own_freshness(monkeypatch):
    import trading.market_context as mc

    async def fake_ctx(monitors, tv, symbol):
        return SimpleNamespace(mt5={"state": "DISCONNECTED", "quotes": {}}, tradingview={"evidence": "STALE", "detail": "Last chart frame 83s ago"}, generated_at=NOW.isoformat())
    monkeypatch.setattr(mc, "build_market_context", fake_ctx)
    items = await tars_state_collector(object(), object())("what is gold doing")
    by = {i.name: i for i in items}
    assert by["mt5_XAUUSD"].freshness == "UNAVAILABLE" and by["tradingview_XAUUSD"].freshness == "STALE" and by["tradingview_XAUUSD"].as_of is None
    assert await tars_state_collector(None, None)("what is gold doing") == []
    assert await tars_state_collector(object(), object())("tell me a joke") == []


# ============================ reviewer fallback models ==========================================================================
def multi_model_client(behaviour: dict):
    """behaviour: model -> 'ok' | 'down' | 'auth' | 'limit'."""
    seen = []

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        seen.append(body["model"])
        kind = behaviour[body["model"]]
        if kind == "down":
            return httpx.Response(503, json={"error": {"code": "subscription_sharing_usage_unavailable"}})
        if kind == "auth":
            return httpx.Response(401)
        if kind == "limit":
            return httpx.Response(429, json={"error": {"code": "subscription_sharing_usage_limit_exceeded"}})
        return httpx.Response(200, content=sse_text(REVIEW_JSON))
    return OpenAIResponsesClient(ApiKeyAuth("sk-test-0123456789"), transport=httpx.MockTransport(handler), timeout=5), seen


def pkt():
    return EvidencePacket(question="q", instrument="XAUUSD", evidence=[item("quote", "market", immutable=False)], claude_analysis="a")


async def test_transient_primary_failure_uses_the_fallback_model_and_says_so():
    client, seen = multi_model_client({"primary": "down", "backup": "ok"})
    r = await TradingReviewService(client, "primary", fallback_models=("backup",)).review(pkt())
    assert r.verdict is Verdict.QUALIFY and r.model == "backup" and r.fallback_from == "primary" and seen == ["primary", "backup"]


async def test_auth_and_usage_failures_do_not_hop_between_models():
    for kind, err in (("auth", "auth"), ("limit", "usage_limit")):
        client, seen = multi_model_client({"primary": kind, "backup": "ok"})
        r = await TradingReviewService(client, "primary", fallback_models=("backup",)).review(pkt())
        assert r.verdict is Verdict.ERROR and r.error_kind == err and seen == ["primary"]


async def test_all_models_failing_is_an_error_that_the_decision_engine_labels_unreviewed():
    client, seen = multi_model_client({"primary": "down", "backup": "down"})
    r = await TradingReviewService(client, "primary", fallback_models=("backup",)).review(pkt())
    assert r.verdict is Verdict.ERROR and seen == ["primary", "backup"]
    assert DecisionEngine().decide(pkt(), r).status.value == "UNREVIEWED"


# ============================ pipeline paths + service wiring ====================================================================
async def test_final_events_name_their_execution_path():
    async def local(intent):
        return "Done."

    async def chat(t):
        yield "hi"
    p = IntelligencePipeline(local_exec=local, chat=chat)
    assert (await _final(p, "open calculator")).data["path"] == "FAST"
    assert [e async for e in p.stream("tell me a joke")][-1].data["path"] == "CONVERSATION"


def routed_client(chat_text="Paris.", review_json=REVIEW_JSON, claude=None):
    log = []

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        system = body["input"][0]["content"]
        if "independent reviewer" in system:
            log.append(("review", body["model"]))
            return httpx.Response(200, content=sse_text(review_json))
        log.append(("chat", body["model"]))
        return httpx.Response(200, content=sse_text(chat_text))
    return OpenAIResponsesClient(ApiKeyAuth("sk-test-0123456789"), transport=httpx.MockTransport(handler), timeout=5), log


class FakeClaude:
    def __init__(self):
        self.calls = 0

    async def __call__(self, system, user):
        self.calls += 1
        return json.dumps({"text": "Gold is at 4092.6, one reading only.", "claims_edge": False})


async def test_service_routes_all_three_paths_with_the_right_models(tmp_path):
    client, log = routed_client()
    claude = FakeClaude()

    async def quote(sym):
        return EvidenceItem(name="quote_XAUUSD", kind="market", as_of=NOW.isoformat(), freshness="CURRENT", provenance="fixture", summary="XAUUSD last 4092.60")

    async def cal(ccy):
        return EvidenceItem(name="calendar_USD", kind="event", as_of=NOW.isoformat(), freshness="CURRENT", provenance="fixture", summary="no high-impact events")

    async def local(intent):
        return "Done."
    svc = IntelligenceService(openai_client=client, review_model="gpt-6.1-sol", claude_llm=claude, quote_fn=quote, calendar_fn=cal, cache_dir=tmp_path, local_exec=local)
    fast = [e async for e in svc.stream("open calculator")]
    assert [e.kind for e in fast] == ["FINAL"] and log == [] and claude.calls == 0                           # FAST: no model at all
    conv = [e async for e in svc.stream("what is the capital of France")]
    assert "".join(e.text for e in conv if e.kind == "DELTA") == "Paris." and log == [("chat", "gpt-5.6-luna")] and claude.calls == 0   # CONVERSATION: one fast model
    log.clear()
    res = [e async for e in svc.stream("what do you think about gold right now")]
    final = res[-1]
    assert res[0].kind == "ACK" and final.kind == "FINAL" and final.data["path"] == "RESEARCH" and final.data["independently_reviewed"] is True
    assert claude.calls == 1 and log == [("review", "gpt-6.1-sol")]                                         # RESEARCH: Claude once, reviewer once, no conversation model
    assert not any(e.data.get("path") == "CONVERSATION" for e in res)


async def test_service_marks_research_unreviewed_when_chatgpt_is_unreachable(tmp_path):
    def handler(req):
        return httpx.Response(401)
    client = OpenAIResponsesClient(ApiKeyAuth("sk-test-0123456789"), transport=httpx.MockTransport(handler), timeout=5)

    async def quote(sym):
        return EvidenceItem(name="quote_XAUUSD", kind="market", as_of=NOW.isoformat(), freshness="CURRENT", provenance="fixture", summary="XAUUSD last 4092.60")

    async def cal(ccy):
        return EvidenceItem(name="calendar_USD", kind="event", as_of=NOW.isoformat(), freshness="CURRENT", provenance="fixture", summary="none")
    svc = IntelligenceService(openai_client=client, review_model="m", claude_llm=FakeClaude(), quote_fn=quote, calendar_fn=cal)
    final = [e async for e in svc.stream("is gold bullish right now")][-1]
    assert final.data["status"] == "UNREVIEWED" and final.data["independently_reviewed"] is False and "unverified" in final.text


async def test_review_existing_gates_an_answer_another_component_produced():
    client, log = routed_client()
    svc = IntelligenceService(openai_client=client, review_model="m")
    ev = [EvidenceItem(name="chart_15m", kind="chart", as_of=NOW.isoformat(), freshness="CURRENT", provenance="fixture", summary="price above range")]
    d, r = await svc.review_existing("what is gold doing", "Gold broke out.", ev, instrument="XAUUSD", timeframe="15m")
    assert d.status.value in {"APPROVED", "QUALIFIED"} and d.independently_reviewed and log == [("review", "m")]
    dead = OpenAIResponsesClient(ApiKeyAuth("sk-test-0123456789"), transport=httpx.MockTransport(lambda req: httpx.Response(401)), timeout=5)
    d2, r2 = await IntelligenceService(openai_client=dead, review_model="m").review_existing("q", "a", ev, instrument="XAUUSD")
    assert d2.status.value == "UNREVIEWED" and not d2.independently_reviewed


# ============================ voice tools: review can never be bypassed ==============================================================
class FakeSvc:
    def __init__(self, status="QUALIFIED", reviewed=True, spoken="Not a clear breakout, one reading only."):
        self.calls, self.status, self.reviewed, self.spoken = [], status, reviewed, spoken

    async def stream(self, text, cancel=None, *, instrument=None, timeframe=None):
        from reasoning.pipeline import Event
        self.calls.append((text, instrument, timeframe))
        yield Event("ACK", "Checking…")
        yield Event("FINAL", self.spoken, 1.0, {"status": self.status, "independently_reviewed": self.reviewed, "flags": ["LIMITATIONS"], "detail": "full detail", "path": "RESEARCH"})

    async def review_existing(self, question, answer, evidence, **kw):
        self.calls.append(("review_existing", question))
        d = SimpleNamespace(status=SimpleNamespace(value=self.status), independently_reviewed=self.reviewed, spoken=self.spoken, flags=["LIMITATIONS"], detail="full detail")
        return d, None


def tools_with(svc, raw_answer="Gold looks bullish. More detail follows."):
    from voice.gemini_live import TarsTools
    state = SimpleNamespace(intelligence_service=svc)
    t = TarsTools(state, "sess")
    t.raw_calls = []

    async def raw(question="", context="", symbol=""):
        t.raw_calls.append(question)
        return {"answer": raw_answer, "source": "claude"} if raw_answer else {"error": "down"}
    t._ask_claude_raw = raw
    return t


async def test_ask_claude_routes_trading_questions_through_review_and_never_returns_raw():
    svc = FakeSvc()
    t = tools_with(svc)
    r = await t.call("ask_claude", {"question": "is gold bullish right now", "symbol": "gold"})
    assert r["status"] == "QUALIFIED" and r["independently_reviewed"] is True and r["answer"] == svc.spoken and t.raw_calls == [] and svc.calls[0][1] == "gold"
    r2 = await t.call("ask_claude", {"question": "did H0004 survive validation"})
    assert t.raw_calls == [] and len(svc.calls) == 2 and r2["status"] == "QUALIFIED"


async def test_ask_claude_still_serves_non_trading_questions_directly():
    svc = FakeSvc()
    t = tools_with(svc)
    r = await t.call("ask_claude", {"question": "explain recursion in simple terms"})
    assert t.raw_calls == ["explain recursion in simple terms"] and svc.calls == [] and r["source"] == "claude"


async def test_trading_intelligence_without_chatgpt_is_labelled_unreviewed_never_verified(monkeypatch):
    t = tools_with(None)

    async def no_service():
        return None
    t._intelligence = no_service
    r = await t.call("trading_intelligence", {"question": "is gold bullish right now", "symbol": "gold"})
    assert r["status"] == "UNREVIEWED" and r["independently_reviewed"] is False and "unverified" in r["answer"] and "UNREVIEWED" in r["flags"]
    t2 = tools_with(None, raw_answer=None)
    t2._intelligence = no_service
    r2 = await t2.call("trading_intelligence", {"question": "is gold bullish"})
    assert r2["status"] == "UNREVIEWED" and r2["independently_reviewed"] is False and "error" in r2        # nothing was concluded; the honest error passes through


async def test_trading_intelligence_passes_instrument_and_timeframe_and_empty_questions_are_rejected():
    svc = FakeSvc()
    t = tools_with(svc)
    r = await t.call("trading_intelligence", {"question": "gold on the 15 minute", "symbol": "XAUUSD", "timeframe": "15m"})
    assert svc.calls == [("gold on the 15 minute", "XAUUSD", "15m")] and r["path"] == "RESEARCH"
    assert "error" in await t.call("trading_intelligence", {"question": "  "})


async def test_analyze_market_answers_pass_the_review_gate():
    svc = FakeSvc(status="QUALIFIED", spoken="Gold is above its range, but that alone isn't a breakout.")
    t = tools_with(svc)
    obs = SimpleNamespace(timeframe="15m", analysis=SimpleNamespace(market_context="price above the 20-bar high"))
    result = SimpleNamespace(status="RESOLVED", symbol="XAUUSD", answer="Gold has broken out strongly and will keep rising.", timeframes_analyzed=["15m"], timeframes_failed=[],
                             observations=[obs], market_context=None, candidates=[], detail="")

    class Orch:
        async def analyze(self, query, **kw):
            return result
    t.state.market_explainer = Orch()
    r = await t.call("analyze_market", {"asset": "gold", "question": "analyze gold"})
    assert r["status"] == "QUALIFIED" and r["answer"] == svc.spoken and r["independently_reviewed"] is True
    assert "broken out strongly" in r["full_analysis"] and svc.calls[-1][0] == "review_existing"                  # the raw claim is only kept as detail


async def test_analyze_market_without_a_reviewer_is_labelled_unreviewed():
    t = tools_with(None)

    async def no_service():
        return None
    t._intelligence = no_service
    result = SimpleNamespace(status="RESOLVED", symbol="XAUUSD", answer="Gold has broken out. Strong momentum.", timeframes_analyzed=["15m"], timeframes_failed=[],
                             observations=[], market_context=None, candidates=[], detail="")

    class Orch:
        async def analyze(self, query, **kw):
            return result
    t.state.market_explainer = Orch()
    r = await t.call("analyze_market", {"asset": "gold"})
    assert r["status"] == "UNREVIEWED" and r["independently_reviewed"] is False and r["answer"].startswith("I couldn't get an independent check")


def test_tool_is_declared_and_the_status_pill_is_honest():
    from voice.activity import describe_tool_call, describe_tool_result
    from voice.gemini_live import SYSTEM_PROMPT, TOOL_NAMES, _tool_declarations
    assert "trading_intelligence" in TOOL_NAMES and "trading_intelligence" in {d.name for d in _tool_declarations()[0].function_declarations}
    assert "Independently REVIEWED" in next(d.description for d in _tool_declarations()[0].function_declarations if d.name == "trading_intelligence")
    assert "NEVER add your own trading conclusion" in SYSTEM_PROMPT
    assert describe_tool_result("trading_intelligence", {}, "QUALIFIED") == "Reviewed, with caveats" and describe_tool_result("x", {}, "UNREVIEWED").startswith("Answered, not")
    assert describe_tool_result("x", {}, "WITHHELD") == "Not calling it" and "review" in describe_tool_call("trading_intelligence", {}).lower()


async def test_analyze_market_with_no_chart_falls_back_to_the_reviewed_path_not_a_dead_end():
    svc = FakeSvc(status="WITHHELD", spoken="I only have a single price reading and no chart, so I'm not calling a direction.")
    t = tools_with(svc)
    result = SimpleNamespace(status="CHART_UNAVAILABLE", symbol=None, answer="", timeframes_analyzed=[], timeframes_failed=[], observations=[], market_context=None,
                             candidates=[], detail="TradingView isn't open")

    class Orch:
        async def analyze(self, query, **kw):
            return result
    t.state.market_explainer = Orch()
    r = await t.call("analyze_market", {"asset": "gold", "question": "what is gold doing right now"})
    assert r["status"] == "WITHHELD" and r["answer"] == svc.spoken and r["chart_status"] == "CHART_UNAVAILABLE" and svc.calls[0][1] == "gold"
    t2 = tools_with(None)

    async def no_service():
        return None
    t2._intelligence = no_service
    t2.state.market_explainer = Orch()
    r2 = await t2.call("analyze_market", {"asset": "gold"})
    assert r2["status"] == "UNREVIEWED" and r2["independently_reviewed"] is False        # even the fallback never pretends to be reviewed


# ============================ server-side lead-in acknowledgement + instrument resolution ==========================================
def _wav_bytes(seconds=0.2, rate=16000):
    import io
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\0\0" * int(rate * seconds))
    return buf.getvalue()


class FakeTTS:
    name = "fake"

    def __init__(self):
        self.said = []

    async def synthesize(self, text):
        self.said.append(text)
        return SimpleNamespace(audio=_wav_bytes())


def lead_session():
    from voice import gemini_live as G
    events = []

    async def emit(ev):
        events.append(ev)
    tts = FakeTTS()
    s = G.GeminiLiveVoiceSession(SimpleNamespace(), emit, lambda pcm: True, ack_tts=tts)
    return G, s, tts, events


async def test_lead_in_is_spoken_when_a_long_tool_runs_in_silence(monkeypatch):
    G, s, tts, events = lead_session()
    monkeypatch.setattr(G, "LEAD_IN_AFTER_S", 0.05)
    await s._lead_in("trading_intelligence", s.generation)
    assert tts.said == ["Let me check that properly. One moment."] and any(e.get("type") == "audio_pcm" for e in events)
    assert s.metrics.latest["lead_in_spoken"] == 1 and s._muted is False          # the model's own final answer is NOT muted


async def test_lead_in_stays_quiet_if_the_model_already_spoke_or_the_turn_changed(monkeypatch):
    G, s, tts, events = lead_session()
    monkeypatch.setattr(G, "LEAD_IN_AFTER_S", 0.05)
    s._first_audio = True
    await s._lead_in("trading_intelligence", s.generation)
    s._first_audio = False
    await s._lead_in("trading_intelligence", s.generation + 1)
    assert tts.said == []


async def test_lead_in_is_cancelled_when_the_tool_finishes_first(monkeypatch):
    G, s, tts, events = lead_session()
    monkeypatch.setattr(G, "LEAD_IN_AFTER_S", 0.5)
    t = asyncio.create_task(s._lead_in("analyze_market", s.generation))
    await asyncio.sleep(0.05)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert tts.said == [] and set(G.LEAD_IN_TOOLS) == {"trading_intelligence", "ask_claude", "analyze_market"}


@pytest.mark.parametrize("spoken,expected", [("gold", "XAUUSD"), ("Gold", "XAUUSD"), ("XAU/USD", "XAUUSD"), ("xauusd", "XAUUSD"), ("silver", "XAGUSD"), ("bitcoin", "BTCUSD"),
                                              ("EUR/USD", "EURUSD"), ("purple elephant currency", None), ("", None), (None, None)])
def test_spoken_instrument_names_resolve_to_tickers_never_guessed(spoken, expected):
    assert IntelligenceService.resolve_instrument(spoken) == expected
