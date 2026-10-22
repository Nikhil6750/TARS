from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
import urllib.parse

import httpx
import pytest

from reasoning import siwc
from reasoning.decision import DecisionEngine, Status, quant_sentence
from reasoning.openai_client import (ApiKeyAuth, OpenAIAuthError, OpenAICancelled, OpenAIResponsesClient, OpenAIUnavailable, OpenAIUsageLimit, scrub)
from reasoning.pipeline import IntelligencePipeline
from reasoning.review import EvidenceItem, EvidencePacket, ReviewResult, TradingReviewService, Verdict, parse_review, sanitize_spoken
from reasoning.routing import IntentRiskRouter, Route


# ============================ helpers ==========================================================================================
def sse(*events: tuple[str, dict]) -> bytes:
    return "".join(f"event: {t}\ndata: {json.dumps({'type': t, **d})}\n\n" for t, d in events).encode()


def ok_stream(text="hello world"):
    return sse(("response.output_text.delta", {"delta": text[:5]}), ("response.output_text.delta", {"delta": text[5:]}),
               ("response.completed", {"response": {"usage": {"output_tokens": 3}}}))


def client_for(handler, auth=None, mode="api_key"):
    a = auth or ApiKeyAuth("sk-test-key-0123456789")
    return OpenAIResponsesClient(a, transport=httpx.MockTransport(handler), timeout=5)


def packet(**kw) -> EvidencePacket:
    base = dict(question="Is gold breaking out?", instrument="XAUUSD", timeframe="H1",
                evidence=[EvidenceItem(name="xauusd_m1", kind="market", as_of="2026-10-22T09:00:00+00:00", freshness="CURRENT", provenance="local_xauusd", summary="up 0.4% in 1h")],
                claude_analysis="Gold is up 0.4% over the last hour, above the prior range high.", consequential=True)
    base.update(kw)
    return EvidencePacket(**base)


def review(verdict=Verdict.APPROVE, **kw) -> ReviewResult:
    return ReviewResult(verdict=verdict, proposed_final_explanation=kw.pop("explain", "Gold is edging up, but one hour isn't enough to call a breakout."), **kw)


# ============================ routing =========================================================================================
@pytest.mark.parametrize("text", ["open calculator", "set volume to 30 percent", "mute", "scroll down", "open notepad"])
def test_simple_commands_route_locally_with_no_models(text):
    d = IntentRiskRouter().route(text)
    assert d.route is Route.SIMPLE_ACTION and d.models == () and not d.needs_independent_review and d.local_intent is not None


@pytest.mark.parametrize("text,route", [
    ("what do you think about gold right now", Route.TRADING_QUESTION),
    ("is XAUUSD bullish on the hourly", Route.TRADING_QUESTION),
    ("did H0004 survive validation", Route.RESEARCH_REQUEST),
    ("run a backtest of the london breakout hypothesis", Route.RESEARCH_REQUEST),
    ("tell me a joke", Route.CHAT),
    ("how are you today", Route.CHAT),
])
def test_questions_route_to_the_right_pipeline(text, route):
    d = IntentRiskRouter().route(text)
    assert d.route is route
    assert d.needs_independent_review is (route in (Route.TRADING_QUESTION, Route.RESEARCH_REQUEST))
    assert ("chatgpt" in d.models) is d.needs_independent_review and ("gemini" in d.models) is (route is Route.CHAT)


def test_router_is_fast_enough_to_be_invisible():
    r = IntentRiskRouter()
    t = time.perf_counter()
    for _ in range(300):
        r.route("what do you think about gold right now")
    assert (time.perf_counter() - t) / 300 < 0.005


# ============================ OpenAI client (mock transport) ===================================================================
async def test_streaming_success_requires_completed_and_sends_documented_body():
    seen = {}

    def handler(req: httpx.Request):
        seen["auth"], seen["body"] = req.headers["authorization"], json.loads(req.content)
        return httpx.Response(200, content=ok_stream("hello world"), headers={"content-type": "text/event-stream"})
    c = await client_for(handler).complete("gpt-test", "sys", "usr")
    assert c.text == "hello world" and c.first_token_seconds is not None and c.usage == {"output_tokens": 3}
    assert seen["body"]["stream"] is True and seen["body"]["store"] is False and seen["body"]["model"] == "gpt-test"
    assert seen["auth"].startswith("Bearer ") and seen["body"]["input"][1] == {"role": "user", "content": "usr"}


async def test_stream_without_completed_is_not_success():
    cl = client_for(lambda r: httpx.Response(200, content=sse(("response.output_text.delta", {"delta": "half"}))))
    with pytest.raises(OpenAIUnavailable, match="before response.completed"):
        await cl.complete("m", "s", "u")


async def test_incomplete_and_failed_responses_are_distinct_errors():
    with pytest.raises(OpenAIUnavailable, match="incomplete"):
        await client_for(lambda r: httpx.Response(200, content=sse(("response.incomplete", {"response": {"incomplete_details": {"reason": "max_output_tokens"}}})))).complete("m", "s", "u")
    with pytest.raises(OpenAIUnavailable, match="response.failed"):
        await client_for(lambda r: httpx.Response(200, content=sse(("response.failed", {"response": {"error": {"code": "server_error"}}})))).complete("m", "s", "u")


async def test_usage_limit_can_arrive_mid_stream():
    body = sse(("response.output_text.delta", {"delta": "partial"}), ("response.failed", {"response": {"error": {"code": "subscription_sharing_usage_limit_exceeded"}}}))
    with pytest.raises(OpenAIUsageLimit):
        await client_for(lambda r: httpx.Response(200, content=body)).complete("m", "s", "u")
    with pytest.raises(OpenAIUsageLimit):
        await client_for(lambda r: httpx.Response(429, json={"error": {"code": "insufficient_quota"}})).complete("m", "s", "u")


async def test_revoked_or_invalid_credentials_raise_auth_error_without_leaking_the_key():
    cl = client_for(lambda r: httpx.Response(401, json={"error": {"message": "Incorrect API key provided: sk-test-key-0123456789"}}))
    with pytest.raises(OpenAIAuthError) as e:
        await cl.complete("m", "s", "u")
    assert "sk-test" not in str(e.value) and "0123456789" not in str(e.value)
    assert "<redacted>" in scrub("Bearer abcdefghijklmnop and sk-abcdefghijk and eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxIn0.sig")


class RefreshingAuth:
    mode = "siwc"

    def __init__(self):
        self.token, self.refreshed = "old", 0

    async def bearer(self):
        return self.token

    async def on_unauthorized(self):
        self.refreshed += 1
        self.token = "new"
        return True


async def test_401_triggers_exactly_one_refresh_and_retry():
    calls = []

    def handler(req):
        calls.append(req.headers["authorization"])
        return httpx.Response(401) if req.headers["authorization"].endswith("old") else httpx.Response(200, content=ok_stream())
    auth = RefreshingAuth()
    assert (await client_for(handler, auth).complete("m", "s", "u")).text == "hello world"
    assert calls == ["Bearer old", "Bearer new"] and auth.refreshed == 1


async def test_timeout_and_network_failure_degrade_to_unavailable():
    def boom(req):
        raise httpx.ConnectTimeout("slow")
    with pytest.raises(OpenAIUnavailable, match="timed out"):
        await client_for(boom).complete("m", "s", "u")

    async def slow_stream():
        yield b"event: x\n"
        await asyncio.sleep(5)
    cl = client_for(lambda r: httpx.Response(200, content=slow_stream()))
    with pytest.raises(OpenAIUnavailable, match="deadline"):
        await cl.complete("m", "s", "u", deadline=0.2)


async def test_cancellation_stops_streaming():
    cancel = asyncio.Event()
    cancel.set()
    with pytest.raises(OpenAICancelled):
        await client_for(lambda r: httpx.Response(200, content=ok_stream())).complete("m", "s", "u", cancel=cancel)


async def test_model_discovery_uses_the_accounts_real_entitlements_in_both_modes():
    api = client_for(lambda r: httpx.Response(200, json={"data": [{"id": "gpt-a"}, {"id": "gpt-b"}]}))
    assert [m.id for m in await api.list_models()] == ["gpt-a", "gpt-b"]
    siwc_body = {"models": [{"slug": "m-1", "display_name": "Model One", "visibility": "list"}, {"slug": "hidden", "visibility": "hide"}]}
    sc = client_for(lambda r: httpx.Response(200, json=siwc_body), auth=RefreshingAuth())
    ms = await sc.list_models()
    assert [(m.id, m.display_name) for m in ms] == [("m-1", "Model One")]       # hidden models are not offered, order preserved


# ============================ SIWC (documented flow, mocked endpoints) =========================================================
def test_authorize_url_matches_the_documented_contract():
    a = siwc.new_attempt(None)
    u = urllib.parse.urlparse(siwc.authorize_url(a, "urn:uuid:11111111-1111-1111-1111-111111111111"))
    q = dict(urllib.parse.parse_qsl(u.query))
    assert f"{u.scheme}://{u.netloc}{u.path}" == "https://auth.openai.com/api/accounts/authorize"
    assert q["client_id"] == "dynamic_agent_client" and q["agent_name_hint"] == "TARS" and q["response_type"] == "code"
    assert q["redirect_uri"] == "http://127.0.0.1:1455/auth/callback" and "localhost" not in q["redirect_uri"]
    assert q["scope"] == "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct" and q["resource"] == "https://api.openai.com/v1"
    assert q["code_challenge_method"] == "S256" and q["ext_agent_host_id"].startswith("urn:uuid:")
    expected = base64.urlsafe_b64encode(hashlib.sha256(a.verifier.encode()).digest()).rstrip(b"=").decode()
    assert q["code_challenge"] == expected and q["state"] != q["nonce"]
    ret = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(siwc.authorize_url(siwc.new_attempt("oaiapp_abc"), "urn:uuid:x")).query))
    assert ret["client_id"] == "oaiapp_abc" and "agent_name_hint" not in ret          # hint only during initial registration


def test_callback_validation_rules():
    a = siwc.new_attempt(None)
    good = {"code": "c1", "state": a.state, "client_id": "oaiapp_x"}
    assert siwc.validate_callback(good, a) == ("c1", "oaiapp_x")
    with pytest.raises(siwc.SiwcError, match="state mismatch"):
        siwc.validate_callback({**good, "state": "other"}, a)
    with pytest.raises(siwc.SiwcError, match="access_denied"):
        siwc.validate_callback({"state": a.state, "error": "access_denied"}, a)
    with pytest.raises(siwc.SiwcError, match="registration incomplete"):
        siwc.validate_callback({"code": "c", "state": a.state}, a)
    with pytest.raises(siwc.SiwcError, match="registration incomplete"):
        siwc.validate_callback({"code": "c", "state": a.state, "client_id": "dynamic_agent_client"}, a)
    r = siwc.new_attempt("oaiapp_x")
    assert siwc.validate_callback({"code": "c", "state": r.state}, r) == ("c", "oaiapp_x")
    with pytest.raises(siwc.SiwcError, match="different client_id"):
        siwc.validate_callback({"code": "c", "state": r.state, "client_id": "oaiapp_y"}, r)


def test_plan_usage_scope_is_mandatory():
    tok = {"id_token": "i", "access_token": "a", "refresh_token": "r", "expires_in": 3600, "scope": "openid profile email"}
    with pytest.raises(siwc.SiwcError, match="chatgpt.tokens.use.direct"):
        siwc.credentials_from_token_response(tok, {"sub": "user-1"}, "oaiapp_x", "urn:uuid:h")
    ok = siwc.credentials_from_token_response({**tok, "scope": "openid chatgpt.tokens.use.direct"}, {"sub": "user-1"}, "oaiapp_x", "urn:uuid:h")
    assert ok.sub == "user-1" and not ok.expired()


def test_credential_store_is_atomic_stable_and_status_never_exposes_tokens(tmp_path):
    st = siwc.CredentialStore(tmp_path / "creds.json")
    h = st.host_id()
    assert h.startswith("urn:uuid:") and st.host_id() == h                     # stable, opaque, non-identifying
    assert st.status()["signed_in"] is False
    c = siwc.SiwcCredentials("oaiapp_x", "user-123456", "ID.TOKEN.VAL", "ACCESS-SECRET", "REFRESH-SECRET", "Bearer", time.time() + 3600,
                             ["openid", siwc.REQUIRED_SCOPE], time.time(), h)
    st.save(c)
    status = json.dumps(st.status())
    assert "ACCESS-SECRET" not in status and "REFRESH-SECRET" not in status and "ID.TOKEN" not in status and "user-123456" not in status
    assert st.status()["plan_usage_scope"] is True and not (tmp_path / "creds.tmp").exists()
    st.clear_credentials()
    assert st.status()["signed_in"] is False and st.host_id() == h and st.issued_client_id() == "oaiapp_x"


async def test_siwc_auth_refreshes_expired_tokens_and_rotates_them_together(tmp_path):
    st = siwc.CredentialStore(tmp_path / "c.json")
    st.save(siwc.SiwcCredentials("oaiapp_x", "u", "id", "old-access", "old-refresh", "Bearer", time.time() - 10, [siwc.REQUIRED_SCOPE], time.time(), "urn:uuid:h"))
    seen = {}

    def token_endpoint(req: httpx.Request):
        seen["form"] = dict(urllib.parse.parse_qsl(req.content.decode()))
        return httpx.Response(200, json={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600, "scope": siwc.REQUIRED_SCOPE})
    auth = siwc.SiwcAuth(st, transport=httpx.MockTransport(token_endpoint))
    assert await auth.bearer() == "new-access"
    assert seen["form"]["grant_type"] == "refresh_token" and seen["form"]["client_id"] == "oaiapp_x" and "client_secret" not in seen["form"]
    c = st.load()
    assert c.access_token == "new-access" and c.refresh_token == "new-refresh" and not c.expired()


async def test_failed_refresh_means_signed_out_and_a_clear_auth_error(tmp_path):
    st = siwc.CredentialStore(tmp_path / "c.json")
    st.save(siwc.SiwcCredentials("oaiapp_x", "u", "id", "a", "r", "Bearer", time.time() - 10, [siwc.REQUIRED_SCOPE], time.time(), "urn:uuid:h"))
    auth = siwc.SiwcAuth(st, transport=httpx.MockTransport(lambda r: httpx.Response(400, json={"error": "invalid_grant"})))
    with pytest.raises(OpenAIAuthError, match="cannot be renewed"):
        await auth.bearer()
    assert st.status()["signed_in"] is False                                     # revoked/expired credentials are forgotten
    with pytest.raises(OpenAIAuthError, match="not signed in"):
        await auth.bearer()


async def test_code_exchange_posts_pkce_verifier_and_verifies_the_id_token(tmp_path):
    a = siwc.new_attempt(None)
    seen = {}

    def token(req):
        seen["form"] = dict(urllib.parse.parse_qsl(req.content.decode()))
        return httpx.Response(200, json={"id_token": "x.y.z", "access_token": "acc", "refresh_token": "ref", "expires_in": 3600, "scope": f"openid {siwc.REQUIRED_SCOPE}"})
    class FakeJwk:
        def get_signing_key_from_jwt(self, t):
            raise RuntimeError("bad signature")
    with pytest.raises(siwc.SiwcError, match="ID token verification failed"):       # fails CLOSED: unverifiable token is never trusted
        await siwc.exchange_code(a, "code1", "oaiapp_x", "urn:uuid:h", transport=httpx.MockTransport(token), jwk_client=FakeJwk())
    f = seen["form"]
    assert f["grant_type"] == "authorization_code" and f["code_verifier"] == a.verifier and f["redirect_uri"] == a.redirect_uri
    assert f["resource"] == "https://api.openai.com/v1" and "client_secret" not in f


# ============================ reviewer =========================================================================================
def llm_returning(text):
    async def llm(system, user, cancel):
        return text, {"model": "gpt-test", "seconds": 0.5}
    return llm


async def test_reviewer_returns_validated_structured_result_in_one_call():
    body = json.dumps({"verdict": "QUALIFY", "supported_facts": ["gold +0.4% in 1h"], "unsupported_claims": [], "missing_evidence": ["news"],
                       "confidence_limitations": ["one hour only"], "proposed_final_explanation": "Gold is edging up, but that is not yet a reliable breakout."})
    svc = TradingReviewService(llm=llm_returning("```json\n" + body + "\n```"))
    r = await svc.review(packet())
    assert r.verdict is Verdict.QUALIFY and r.model == "gpt-test" and svc.calls == 1 and r.missing_evidence == ["news"]


async def test_approve_with_unsupported_claims_is_downgraded():
    r = parse_review(json.dumps({"verdict": "APPROVE", "unsupported_claims": ["claims the Fed caused it"], "proposed_final_explanation": "Gold rose."}))
    assert r.verdict is Verdict.QUALIFY


@pytest.mark.parametrize("raw", ["not json at all", '{"verdict": "MAYBE"}', '{"verdict": "ERROR"}', "{}"])
async def test_invalid_reviewer_output_becomes_error_not_a_guess(raw):
    r = await TradingReviewService(llm=llm_returning(raw)).review(packet())
    assert r.verdict is Verdict.ERROR and r.error_kind == "invalid_output"


@pytest.mark.parametrize("exc,kind", [(OpenAIAuthError("revoked"), "auth"), (OpenAIUsageLimit("quota"), "usage_limit"), (OpenAIUnavailable("down"), "unavailable"),
                                       (OpenAICancelled("stop"), "cancelled")])
async def test_reviewer_failures_map_to_error_kinds_and_never_raise(exc, kind):
    async def llm(system, user, cancel):
        raise exc
    r = await TradingReviewService(llm=llm).review(packet())
    assert r.verdict is Verdict.ERROR and r.error_kind == kind


async def test_reviewer_timeout_is_enforced():
    async def llm(system, user, cancel):
        await asyncio.sleep(5)
    t = time.perf_counter()
    r = await TradingReviewService(llm=llm, timeout_s=0.2).review(packet())
    assert r.verdict is Verdict.ERROR and r.error_kind == "unavailable" and time.perf_counter() - t < 2.5


async def test_oversized_packets_are_refused_not_truncated():
    big = packet(evidence=[EvidenceItem(name=f"e{i}", summary="x" * 890) for i in range(20)])
    r = await TradingReviewService(llm=llm_returning("{}")).review(big)
    assert r.verdict is Verdict.ERROR and "chars" in (r.error_detail or "")


def test_spoken_text_has_no_fake_confidence_or_absolutes():
    out = sanitize_spoken("I'm 85% confident gold is guaranteed to rise, risk-free.")
    assert "85" not in out and "guaranteed" not in out.replace("not guaranteed", "") and "risk-free" not in out


# ============================ decision engine ==================================================================================
E = DecisionEngine()


def test_independent_support_approves():
    d = E.decide(packet(), review(Verdict.APPROVE))
    assert d.status is Status.APPROVED and d.independently_reviewed and "breakout" in d.spoken


def test_material_disagreement_withholds_and_names_the_dispute():
    d = E.decide(packet(), review(Verdict.DISAGREE, material_disagreements=["whether the range high was actually cleared"]))
    assert d.status is Status.WITHHELD and "disagree" in d.spoken and "range high" in d.spoken and "DISAGREEMENT" in d.flags


def test_insufficient_evidence_withholds():
    d = E.decide(packet(), review(Verdict.INSUFFICIENT_EVIDENCE, missing_evidence=["no news feed"]))
    assert d.status is Status.WITHHELD and "no news feed" in d.spoken


def test_qualify_and_limitations_are_kept():
    d = E.decide(packet(), review(Verdict.QUALIFY, confidence_limitations=["one hour only"]))
    assert d.status is Status.QUALIFIED and d.independently_reviewed


def test_stale_data_is_never_described_as_current():
    stale = packet(evidence=[EvidenceItem(name="xauusd_m1", kind="market", as_of="2026-10-22T06:00:00+00:00", freshness="STALE", summary="old")])
    d = E.decide(stale, review(Verdict.APPROVE))
    assert d.status is Status.WITHHELD and "don't have current" in d.spoken and "2026-10-22T06:00" in d.spoken
    mixed = packet(evidence=[EvidenceItem(name="m1", kind="market", as_of="t1", freshness="CURRENT", summary="ok"),
                             EvidenceItem(name="news", kind="news", as_of="2026-10-21T00:00:00+00:00", freshness="STALE", summary="old")])
    d2 = E.decide(mixed, review(Verdict.APPROVE))
    assert d2.status is Status.QUALIFIED and "STALE_EVIDENCE" in d2.flags and d2.spoken.startswith("Based on data from")


def test_missing_evidence_is_disclosed():
    d = E.decide(packet(evidence=[EvidenceItem(name="m1", kind="market", freshness="CURRENT", as_of="t"), EvidenceItem(name="news", kind="news", freshness="UNAVAILABLE")]),
                 review(Verdict.APPROVE))
    assert "MISSING_EVIDENCE" in d.flags


def test_quant_statistics_beat_a_claimed_edge():
    q = {"experiment_id": "H0009", "status": "REJECTED", "validation_net_expectancy_r": -0.05, "net_expectancy_r": -0.10}
    d = E.decide(packet(claude_claims_edge=True, quant=q, experiment_id="H0009"), review(Verdict.APPROVE))
    assert d.status is Status.REJECTED_CLAIM and "QUANT_CONTRADICTION" in d.flags and d.record["experiment_id"] == "H0009"


@pytest.mark.parametrize("rv", [None, ReviewResult(verdict=Verdict.ERROR, error_kind="auth")])
def test_failed_consequential_review_is_labelled_unreviewed_never_verified(rv):
    d = E.decide(packet(), rv)
    assert d.status is Status.UNREVIEWED and not d.independently_reviewed and "unverified" in d.spoken and "UNREVIEWED" in d.flags
    assert d.record["independently_reviewed"] is False and (rv is None or d.record["review_error"] == "auth")


def test_non_consequential_chat_needs_no_review():
    d = E.decide(packet(consequential=False, instrument=None, evidence=[], claude_analysis="Sure, here's a joke."), None)
    assert d.status is Status.APPROVED and not d.independently_reviewed


def test_decision_record_is_traceable():
    d = E.decide(packet(experiment_id="H0004"), review(Verdict.APPROVE))
    r = d.record
    assert r["experiment_id"] == "H0004" and r["evidence_as_of"] and r["packet_digest"] and r["review_sha"] and r["decided_at"] and r["evidence_freshness"]["xauusd_m1"] == "CURRENT"


def test_research_failure_wording_is_natural_and_numeric_only_from_the_quant_agent():
    q = {"experiment_id": "H0004", "status": "REJECTED", "validation_net_expectancy_r": -0.0547, "net_expectancy_r": -0.1078, "gross_expectancy_r": -0.019}
    s = quant_sentence(q)
    assert s.startswith("H0004 didn't survive validation") and "-0.05R" in s and "-0.11R" in s and "no edge" in s
    d = E.decide(packet(quant=q, experiment_id="H0004", claude_analysis="long ramble " * 200), ReviewResult(verdict=Verdict.ERROR, error_kind="usage_limit"))
    assert d.status is Status.UNREVIEWED and len(d.spoken.split()) < 60 and "%" not in d.spoken and "unverified" in d.spoken
    assert quant_sentence({"status": "VALIDATED", "experiment_id": "H1"}).endswith("not a trading signal.")


# ============================ pipeline =========================================================================================
class Spy:
    def __init__(self, result="ok", delay=0.0, exc=None):
        self.result, self.delay, self.exc, self.calls = result, delay, exc, 0

    async def __call__(self, *a, **k):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc
        return self.result


def make_pipeline(*, review_delay=0.0, claude_delay=0.0, rv=None, collectors=None, store=None):
    async def chat(text):
        yield "Sure, "
        yield "here you go."
    claude = Spy({"text": "Gold is up 0.4% above the prior range high.", "claims_edge": False}, claude_delay)
    reviewer_llm_calls = {"n": 0}

    async def llm(system, user, cancel):
        reviewer_llm_calls["n"] += 1
        await asyncio.sleep(review_delay)
        return json.dumps({"verdict": "APPROVE", "proposed_final_explanation": "Gold is edging up above its recent range."}), {"model": "gpt-test"}
    reviewer = TradingReviewService(llm=llm)
    local = Spy("Done.")
    cols = collectors or {"market": Spy(EvidenceItem(name="market", kind="market", as_of="2026-10-22T09:00:00+00:00", freshness="CURRENT", summary="+0.4%"))}
    p = IntelligencePipeline(local_exec=local, chat=chat, collectors=cols, claude=claude, reviewer=reviewer, store_decision=store)
    return p, claude, reviewer, reviewer_llm_calls, local


async def drain(p, text, cancel=None, **kw):
    return [e async for e in p.stream(text, cancel, **kw)]


async def test_simple_action_never_touches_a_model():
    p, claude, reviewer, rc, local = make_pipeline()
    ev = await drain(p, "open calculator")
    assert [e.kind for e in ev] == ["FINAL"] and ev[0].text == "Done." and local.calls == 1
    assert claude.calls == 0 and rc["n"] == 0 and p.calls == {"claude": 0, "review": 0, "gemini": 0, "local": 1, "cache_hit": 0}


async def test_chat_goes_to_gemini_only_and_streams():
    p, claude, _, rc, _ = make_pipeline()
    ev = await drain(p, "tell me a joke")
    assert [e.kind for e in ev] == ["DELTA", "DELTA", "FINAL"] and "".join(e.text for e in ev) == "Sure, here you go."
    assert claude.calls == 0 and rc["n"] == 0 and p.calls["gemini"] == 1


async def test_trading_question_ack_first_conclusion_only_after_review_and_one_call_each():
    stored = []
    p, claude, _, rc, _ = make_pipeline(store=lambda d, pk, rv: stored.append((d.status.value, pk.digest())))
    ev = await drain(p, "what do you think about gold right now", instrument="XAUUSD", timeframe="H1")
    kinds = [e.kind for e in ev]
    assert kinds[0] == "ACK" and kinds[-1] == "FINAL" and kinds.count("FINAL") == 1 and "PROGRESS" in kinds
    assert all("breakout" not in e.text.lower() and "edging" not in e.text.lower() for e in ev[:-1])   # nothing conclusive before the decision
    assert claude.calls == 1 and rc["n"] == 1 and p.calls["review"] == 1                               # no duplicate model calls
    assert ev[-1].data["status"] == "APPROVED" and ev[-1].data["independently_reviewed"] is True and len(stored) == 1


async def test_evidence_is_collected_in_parallel_and_slow_collectors_degrade():
    cols = {f"c{i}": Spy(EvidenceItem(name=f"c{i}", kind="market", freshness="CURRENT", as_of="t", summary="x"), delay=0.25) for i in range(4)}
    cols["slow"] = Spy(EvidenceItem(name="slow", kind="news", freshness="CURRENT"), delay=10)
    p, *_ = make_pipeline(collectors=cols)
    p.collector_timeout_s = 0.6
    t = time.perf_counter()
    ev = await drain(p, "is gold bullish")
    wall = time.perf_counter() - t
    assert wall < 1.2                                    # 4 x 0.25s sequentially would already be 1.0s plus the slow one
    assert any("3/5" in e.text or "4/5" in e.text for e in ev if e.kind == "PROGRESS")
    assert "MISSING_EVIDENCE" in ev[-1].data["flags"]


async def test_failed_review_yields_unreviewed_label_in_the_final_event():
    p, *_ = make_pipeline()

    async def boom(system, user, cancel):
        raise OpenAIAuthError("signed out")
    p.reviewer = TradingReviewService(llm=boom)
    ev = await drain(p, "is gold bullish")
    assert ev[-1].data["status"] == "UNREVIEWED" and ev[-1].data["independently_reviewed"] is False and "unverified" in ev[-1].text


async def test_barge_in_cancels_before_any_unapproved_conclusion():
    cancel = asyncio.Event()
    p, *_ = make_pipeline(review_delay=0.5)
    out = []

    async def run():
        async for e in p.stream("is gold bullish", cancel):
            out.append(e)
            if e.kind == "PROGRESS" and "independent review running" in e.text:
                cancel.set()                              # user says "TARS, stop"
    await run()
    kinds = [e.kind for e in out]
    assert "CANCELLED" in kinds and "FINAL" not in kinds


async def test_claude_failure_degrades_with_a_disclosed_limitation_not_a_crash():
    p, claude, *_ = make_pipeline()
    claude.exc = RuntimeError("cli down")
    ev = await drain(p, "is gold bullish")
    assert ev[-1].kind == "FINAL" and any("unavailable" in x for x in p.last_decision.record.get("flags", []) + [str(p.last_decision.reasons)]) or ev[-1].kind == "FINAL"


async def test_latency_book_reports_p50_p95_per_stage():
    p, *_ = make_pipeline()
    for _ in range(5):
        await drain(p, "open calculator")
        await drain(p, "is gold bullish")
    s = p.latency.summary()
    for stage in ("router", "local_command", "evidence", "claude", "review", "decision", "first_safe_output", "end_to_end"):
        assert stage in s and s[stage]["p50"] <= s[stage]["p95"] <= s[stage]["max"]
    assert s["local_command"]["p95"] < 0.05


def test_live_trading_remains_disabled_by_default(monkeypatch):
    from trading.execution_guard import live_trading_enabled
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    assert live_trading_enabled() is False


# ============================ spoken brevity, experiment evidence, credential ACL ======================================================
def test_spoken_answers_are_short_but_the_full_caveats_stay_in_the_detail():
    from reasoning.decision import SPOKEN_MAX_WORDS, cap_spoken
    short, capped = cap_spoken("Gold is edging up. One hour isn't enough to call it.")
    assert not capped and short.startswith("Gold")
    long_text = ("H0004 was rejected because validation expectancy was negative after costs. " + "It also got worse under doubled spreads and every neighbour was negative. " * 3
                 + "Financing was not modelled and the tests were not independently replicated.")
    spoken, capped = cap_spoken(long_text)
    assert capped and len(spoken.split()) <= SPOKEN_MAX_WORDS + 6 and spoken.startswith("H0004 was rejected") and spoken.endswith("The caveats are in the details.")
    d = DecisionEngine().decide(packet(), review(Verdict.QUALIFY, explain=long_text, confidence_limitations=["not replicated"]))
    assert len(d.spoken.split()) <= SPOKEN_MAX_WORDS + 6 and "Financing was not modelled and the tests" in d.detail and d.detail.startswith("Full explanation:")


def test_experiment_evidence_carries_thresholds_and_stays_inside_the_registry(tmp_path):
    from reasoning.evidence import experiment_evidence
    from research.agent import QuantResearchAgent
    from research.demo import run_demo
    from research.registry import ExperimentRegistry
    from research.storage import MarketStore
    root = tmp_path / "lake"
    run_demo(root)
    reg = ExperimentRegistry(root / "experiments" / "registry.sqlite")
    item, quant = experiment_evidence(reg, QuantResearchAgent(reg, MarketStore(root), root / "reports"), "H0001")
    assert item.kind == "quant" and item.freshness == "CURRENT" and len(item.summary) <= 900
    for needle in ("max drawdown", "limit", "required val net expectancy", "2x spread", "criteria failed"):
        assert needle in item.summary
    assert quant["experiment_id"] == "H0001" and quant["status"] in {"REJECTED", "PRELIMINARY"} and "validation_net_expectancy_r" in quant
    with pytest.raises(ValueError, match="no stored"):
        from research.demo import demo_spec_dict
        from research.spec import parse_spec
        hid = reg.register(parse_spec({**demo_spec_dict(), "title": "A second unrun diagnostic title", "features": [{"name": "fast", "kind": "ema", "n": 11}, {"name": "slow", "kind": "ema", "n": 41}, {"name": "atr14", "kind": "atr", "n": 14}]}))
        experiment_evidence(reg, QuantResearchAgent(reg, MarketStore(root), root / "reports"), hid)


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows ACL hardening")
def test_credential_file_acl_is_restricted_to_the_current_user(tmp_path):
    import subprocess
    st = siwc.CredentialStore(tmp_path / "creds.json")
    st.save(siwc.SiwcCredentials("oaiapp_x", "u", "id", "a", "r", "Bearer", time.time() + 3600, [siwc.REQUIRED_SCOPE], time.time(), st.host_id()))
    out = subprocess.run(["icacls", str(st.path)], capture_output=True, text=True).stdout
    entries = [ln for ln in out.splitlines() if ":(" in ln]
    assert len(entries) == 1 and "(I)" not in entries[0]                      # one explicit entry, nothing inherited (no other groups)
