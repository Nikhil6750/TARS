from __future__ import annotations

import json
import socket
import urllib.parse

import httpx
import pytest

from reasoning import siwc, siwc_diagnose
from reasoning.openai_client import (ApiKeyAuth, OpenAIAuthError, OpenAINotEligible, OpenAIResponsesClient, OpenAIUnavailable, OpenAIUnsupported, OpenAIUsageLimit)
from reasoning.review import ReviewResult, TradingReviewService, Verdict
from reasoning.siwc_diag import (DiagLog, classify_callback, classify_page_url, classify_timeout, classify_token_error, redact_url, scrub)

SECRETS = ["AUTH-CODE-123456", "STATE-VALUE-abcdef", "NONCE-VALUE-abcdef", "VERIFIER-abcdefghijklmnop", "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.signaturepart", "sk-abcdefghijklmnop",
           "REFRESH-TOKEN-xyz"]


def no_secrets(text: str) -> bool:
    return not any(s in text for s in SECRETS)


# ============================ redaction ============================================================================================
def test_redact_url_hides_every_sensitive_value_but_keeps_names_and_error_codes():
    url = ("https://auth.openai.com/api/accounts/authorize?client_id=oaiapp_ABCDEF123456&code=AUTH-CODE-123456&state=STATE-VALUE-abcdef&nonce=NONCE-VALUE-abcdef"
           "&code_challenge=CHALLENGE-abcdefghijklmnop&id_token_hint=eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.signaturepart&login_hint=me%40example.com&error=access_denied&scope=openid+email")
    r = redact_url(url)
    assert no_secrets(r) and "me@example.com" not in r and "ABCDEF123456" not in r and "CHALLENGE-abcdef" not in r
    for name in ("code", "state", "nonce", "id_token_hint", "login_hint", "client_id", "error", "scope"):
        assert name in r
    assert "access_denied" in r and "oaiapp_<id>" in r


def test_scrub_removes_credentials_embedded_in_free_text():
    shaped = ["eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.signaturepart", "sk-abcdefghijklmnop", "oaiapp_ABCDEF123456"]
    for s in shaped:
        assert s not in scrub(f"failure while sending {s} to the endpoint")
    for kv in ("code=AUTH-CODE-123456", "state=STATE-VALUE-abcdef", "nonce=NONCE-VALUE-abcdef", "token=REFRESH-TOKEN-xyz"):
        assert kv.split("=")[1] not in scrub(f"callback {kv} arrived")
    assert "<redacted>" in scrub("Authorization: Bearer abcdefghijklmnopqrstuv")


def test_diag_log_never_stores_secrets():
    log = DiagLog()
    log.add("REQUEST", True, "built https://x?code=AUTH-CODE-123456&state=STATE-VALUE-abcdef with sk-abcdefghijklmnop")
    assert no_secrets(json.dumps(log.report([])))


# ============================ stage classification ====================================================================================
A = siwc.new_attempt(None)


@pytest.mark.parametrize("params,stage,code", [
    ({"error": "access_denied", "state": A.state}, "CONSENT", "access_denied"),
    ({"error": "invalid_client", "state": A.state}, "CLIENT_ADMISSION", "invalid_client"),
    ({"error": "unauthorized_client", "state": A.state}, "CLIENT_ADMISSION", "unauthorized_client"),
    ({"error": "invalid_request", "state": A.state}, "REQUEST", "invalid_request"),
    ({"error": "invalid_scope", "state": A.state}, "REQUEST", "invalid_scope"),
    ({"error": "login_required", "state": A.state}, "BROWSER_AUTH", "login_required"),
    ({"error": "server_error", "state": A.state}, "UNKNOWN", "server_error"),
    ({"code": "c", "state": "WRONG"}, "CALLBACK", "state_mismatch"),
    ({"code": "c", "state": A.state}, "CLIENT_ADMISSION", "registration_incomplete"),
    ({"state": A.state}, "CALLBACK", "no_code"),
])
def test_callback_classification(params, stage, code):
    f = classify_callback(params, expected_state=A.state, registering=True)
    assert (f.stage, f.code) == (stage, code) and f.verdict == "FAILED"


def test_good_callback_is_ok():
    f = classify_callback({"code": "c", "state": A.state, "client_id": "oaiapp_x"}, expected_state=A.state, registering=True)
    assert f.verdict == "OK" and f.stage == "CALLBACK"


def test_token_errors_name_the_stage_and_the_cause():
    inv = classify_token_error(400, "invalid_client")
    assert inv.stage == "TOKEN_EXCHANGE" and "dynamic_agent_client" in inv.summary
    assert "redirect_uri" in classify_token_error(400, "invalid_grant").next_step


@pytest.mark.parametrize("url,stage,code", [
    ("http://127.0.0.1:1455/auth/callback?error=access_denied", "CALLBACK", "loopback_reached"),
    ("https://auth.openai.com/log-in?next=abc", "BROWSER_AUTH", "login_page"),
    ("https://auth.openai.com/error?reason=session", "BROWSER_AUTH", "error_page"),
    ("https://auth.openai.com/api/accounts/authorize?error=invalid_client", "CLIENT_ADMISSION", "invalid_client"),
    ("https://auth.openai.com/api/accounts/authorize/continue", "BROWSER_AUTH", "inside_openai_flow"),
    ("https://example.com/whatever", "BROWSER_AUTH", "unexpected_host"),
])
def test_page_url_classification_is_conservative(url, stage, code):
    f = classify_page_url(url)
    assert (f.stage, f.code) == (stage, code)
    assert no_secrets(json.dumps(f.__dict__))


def test_timeout_with_no_loopback_hits_points_inside_openai_flow_and_clears_later_stages():
    f = classify_timeout([], preflight_ok=True, browser_launched=True)
    assert f.stage == "BROWSER_AUTH" and f.code == "no_redirect_to_loopback"
    assert "NOT the failure" in f.summary and "siwc_diagnose --classify-url" in f.next_step
    assert classify_timeout([], True, False).code == "browser_not_launched"
    assert classify_timeout([{"path": "/favicon.ico", "param_names": []}], True, True).code == "wrong_path"
    f2 = classify_timeout([{"path": "/auth/callback", "param_names": ["error"], "safe_params": {"error": "access_denied"}}], True, True)
    assert f2.stage == "CONSENT"


# ============================ instrumented login() with a simulated browser ====================================================
PORT = 14561


def browser_that(callback_query=None, delay=0.0):
    """A fake 'system browser': builds a callback from the real authorize URL's state, like auth.openai.com would."""
    seen = {}

    def opener(url):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        seen["query"] = q
        if callback_query is None:
            return True
        params = {k: (v.replace("{state}", q["state"]) if isinstance(v, str) else v) for k, v in callback_query.items()}
        redirect = q["redirect_uri"]
        httpx.get(redirect + "?" + urllib.parse.urlencode(params), timeout=5)
        return True
    opener.seen = seen
    return opener


@pytest.fixture(autouse=True)
def offline_preflight(monkeypatch):
    monkeypatch.setattr(siwc, "_preflight", lambda log, port: (True, []))


async def attempt(tmp_path, opener, timeout=1.5, **kw):
    store = siwc.CredentialStore(tmp_path / "c.json")
    return await siwc.login(store, timeout_s=timeout, port=PORT, opener=opener, **kw), store


async def test_user_declines_consent_is_classified_as_consent_not_a_timeout(tmp_path):
    with pytest.raises(siwc.SiwcLoginFailure) as e:
        await attempt(tmp_path, browser_that({"error": "access_denied", "state": "{state}"}))
    assert (e.value.finding.stage, e.value.finding.code) == ("CONSENT", "access_denied")
    assert no_secrets(json.dumps(e.value.report))


async def test_nothing_reaching_loopback_is_reported_as_failure_inside_openai_flow(tmp_path):
    with pytest.raises(siwc.SiwcLoginFailure) as e:
        await attempt(tmp_path, browser_that(None), timeout=1.0)
    f = e.value.finding
    assert (f.stage, f.code) == ("BROWSER_AUTH", "no_redirect_to_loopback") and "listener received 0 request" in " ".join(f.evidence)
    stages = [ev["stage"] for ev in e.value.report["events"]]
    assert "CALLBACK" in stages                                            # the listener self-test ran and is on record
    assert any(ev["stage"] == "CALLBACK" and ev["ok"] is True for ev in e.value.report["events"])      # so the callback stage is proven healthy


async def test_state_mismatch_is_a_callback_failure(tmp_path):
    with pytest.raises(siwc.SiwcLoginFailure) as e:
        await attempt(tmp_path, browser_that({"code": "AUTH-CODE-123456", "state": "WRONG-STATE", "client_id": "oaiapp_x"}))
    assert e.value.finding.code == "state_mismatch" and no_secrets(json.dumps(e.value.report))


async def test_port_in_use_is_reported_before_the_browser_opens(tmp_path):
    s = socket.socket()
    s.bind(("127.0.0.1", PORT))
    s.listen(1)
    opened = []
    try:
        with pytest.raises(siwc.SiwcLoginFailure) as e:
            await attempt(tmp_path, lambda u: opened.append(u) or True)
    finally:
        s.close()
    assert e.value.finding.code == "port_in_use" and not opened


async def test_token_exchange_failure_is_attributed_to_the_token_stage(tmp_path, monkeypatch):
    async def deny(url, data, transport=None, timeout=20.0):
        raise siwc.TokenEndpointError(400, "invalid_grant")
    monkeypatch.setattr(siwc, "_post_form", deny)
    with pytest.raises(siwc.SiwcLoginFailure) as e:
        await attempt(tmp_path, browser_that({"code": "AUTH-CODE-123456", "state": "{state}", "client_id": "oaiapp_issued1"}))
    assert (e.value.finding.stage, e.value.finding.code) == ("TOKEN_EXCHANGE", "invalid_grant") and no_secrets(json.dumps(e.value.report))


async def test_missing_plan_scope_is_attributed_to_plan_scope(tmp_path, monkeypatch):
    async def tokens(url, data, transport=None, timeout=20.0):
        return {"id_token": "i.d.t", "access_token": "a", "refresh_token": "r", "expires_in": 3600, "scope": "openid profile email"}
    monkeypatch.setattr(siwc, "_post_form", tokens)
    monkeypatch.setattr(siwc, "verify_id_token", lambda *a, **k: {"sub": "u1"})
    with pytest.raises(siwc.SiwcLoginFailure) as e:
        await attempt(tmp_path, browser_that({"code": "c", "state": "{state}", "client_id": "oaiapp_issued1"}))
    assert e.value.finding.stage == "PLAN_SCOPE"


async def test_full_success_path_stores_credentials_and_never_prints_secrets(tmp_path, monkeypatch, capsys):
    async def tokens(url, data, transport=None, timeout=20.0):
        assert data["client_id"] == "oaiapp_issued1" and data["grant_type"] == "authorization_code" and data["resource"] == siwc.RESOURCE
        return {"id_token": "i.d.t", "access_token": "ACCESS-SECRET-1", "refresh_token": "REFRESH-TOKEN-xyz", "expires_in": 3600, "scope": f"openid {siwc.REQUIRED_SCOPE}"}
    monkeypatch.setattr(siwc, "_post_form", tokens)
    monkeypatch.setattr(siwc, "verify_id_token", lambda *a, **k: {"sub": "user-1", "email": "me@example.com"})
    opener = browser_that({"code": "AUTH-CODE-123456", "state": "{state}", "client_id": "oaiapp_issued1"})
    creds, store = await attempt(tmp_path, opener)
    assert creds.client_id == "oaiapp_issued1" and store.status()["plan_usage_scope"] is True and store.issued_client_id() == "oaiapp_issued1"
    out = capsys.readouterr().out
    assert no_secrets(out) and "ACCESS-SECRET-1" not in out and opener.seen["query"]["client_id"] == "dynamic_agent_client"


async def test_returning_signin_uses_issued_client_hints_and_redacts_them_from_output(tmp_path, monkeypatch, capsys):
    store = siwc.CredentialStore(tmp_path / "c.json")
    store.save(siwc.SiwcCredentials("oaiapp_issued1", "user-1", "RETAINED.ID.TOKEN", "a", "r", "Bearer", 0, [siwc.REQUIRED_SCOPE], 0, store.host_id(), email_hint="me@example.com"))
    opener = browser_that(None)
    with pytest.raises(siwc.SiwcLoginFailure):
        await siwc.login(store, timeout_s=0.6, port=PORT, opener=opener)
    q = opener.seen["query"]
    assert q["client_id"] == "oaiapp_issued1" and "agent_name_hint" not in q and q["id_token_hint"] == "RETAINED.ID.TOKEN" and q["login_hint"] == "me@example.com"
    out = capsys.readouterr().out
    assert "RETAINED.ID.TOKEN" not in out and "me@example.com" not in out


# ============================ request contract validator ==============================================================================
def _url(**over):
    a = siwc.new_attempt(None)
    host = "urn:uuid:3f1c9c52-8a53-4b06-9d8e-2a6f6f0d7a11"
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(siwc.authorize_url(a, host)).query))
    q.update(over)
    q = {k: v for k, v in q.items() if v is not None}
    return siwc.AUTHORIZE_URL + "?" + urllib.parse.urlencode(q), host


def test_validator_accepts_what_tars_sends():
    url, host = _url()
    assert all(ok for _, ok, _ in siwc_diagnose.validate_request(url, host))


@pytest.mark.parametrize("over,failing", [
    ({"redirect_uri": "http://localhost:1455/auth/callback"}, "redirect_uri"),
    ({"redirect_uri": "http://127.0.0.1:1455/callback"}, "redirect_uri"),
    ({"scope": "openid profile email"}, "scope"),
    ({"resource": "https://api.openai.com"}, "resource"),
    ({"code_challenge_method": "plain"}, "code_challenge_method"),
    ({"ext_agent_host_id": None}, "ext_agent_host_id"),
    ({"ext_agent_host_id": "urn:uuid:not-a-uuid"}, "ext_agent_host_id"),
    ({"agent_name_hint": None}, "agent_name_hint"),
    ({"client_id": "oaiapp_issued1"}, "agent_name_hint_omitted_on_reauth"),
])
def test_validator_flags_deviations_from_the_documented_table(over, failing):
    url, host = _url(**over)
    bad = {n for n, ok, _ in siwc_diagnose.validate_request(url, host) if not ok}
    assert failing in bad


def test_eligibility_evidence_records_public_unlicensed_repo(monkeypatch):
    monkeypatch.setattr(siwc_diagnose.shutil, "which", lambda n: "gh")

    class R:
        returncode, stdout = 0, json.dumps({"visibility": "PUBLIC", "licenseInfo": None, "nameWithOwner": "o/r"})
    monkeypatch.setattr(siwc_diagnose.subprocess, "run", lambda *a, **k: R())
    e = siwc_diagnose.eligibility_evidence()
    assert e["repository"]["visibility"] == "PUBLIC" and e["repository"]["license"] is None and "WITHOUT a license" in e["assessment"]


def test_token_probe_uses_only_synthetic_values(monkeypatch):
    seen = {}

    def fake_post(url, data=None, timeout=None, **k):
        seen["data"] = data
        return httpx.Response(400, json={"error": "invalid_grant"}, request=httpx.Request("POST", url))
    monkeypatch.setattr(siwc_diagnose.httpx, "post", fake_post)
    f = siwc_diagnose.probe_token_endpoint()
    assert f.verdict == "OK" and seen["data"]["code"] == "diagnostic-invalid" and seen["data"]["client_id"] == "oaiapp_diagnostic_probe"


# ============================ sign-out / revocation ===================================================================================
async def test_sign_out_revokes_with_the_documented_request_then_clears(tmp_path):
    store = siwc.CredentialStore(tmp_path / "c.json")
    store.save(siwc.SiwcCredentials("oaiapp_issued1", "u", "id", "acc", "REFRESH-TOKEN-xyz", "Bearer", 0, [siwc.REQUIRED_SCOPE], 0, "urn:uuid:h"))
    calls = []

    def handler(req: httpx.Request):
        calls.append(req)
        if req.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json={"revocation_endpoint": "https://auth.openai.com/api/accounts/oauth/revoke"})
        return httpx.Response(200)
    out = await siwc.sign_out(store, transport=httpx.MockTransport(handler))
    form = dict(urllib.parse.parse_qsl(calls[-1].content.decode()))
    assert form == {"token": "REFRESH-TOKEN-xyz", "token_type_hint": "refresh_token", "client_id": "oaiapp_issued1"}
    assert out["remote_revocation_confirmed"] is True and store.status()["signed_in"] is False and store.issued_client_id() == "oaiapp_issued1"


async def test_sign_out_without_confirmed_revocation_says_so_but_still_clears(tmp_path):
    store = siwc.CredentialStore(tmp_path / "c.json")
    store.save(siwc.SiwcCredentials("oaiapp_issued1", "u", "id", "acc", "r", "Bearer", 0, [siwc.REQUIRED_SCOPE], 0, "urn:uuid:h"))

    def handler(req):
        return httpx.Response(200, json={"revocation_endpoint": "https://x/revoke"}) if "openid" in req.url.path else httpx.Response(503)
    out = await siwc.sign_out(store, transport=httpx.MockTransport(handler))
    assert out["remote_revocation_confirmed"] is False and "NOT confirmed" in out["note"] and store.status()["signed_in"] is False


# ============================ inference route constraints from the official docs ======================================================
async def test_plan_usage_route_omits_unsupported_fields_but_api_key_route_keeps_them():
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        return httpx.Response(200, content=b'event: response.completed\ndata: {"type":"response.completed","response":{}}\n\n')

    class SiwcLike:
        mode = "siwc"

        async def bearer(self):
            return "tok"

        async def on_unauthorized(self):
            return False
    await OpenAIResponsesClient(SiwcLike(), transport=httpx.MockTransport(handler)).complete("m", "s", "u", max_output_tokens=500)
    await OpenAIResponsesClient(ApiKeyAuth("sk-test-0123456789"), transport=httpx.MockTransport(handler)).complete("m", "s", "u", max_output_tokens=500)
    siwc_body, key_body = bodies
    for banned in ("max_output_tokens", "temperature", "top_p", "metadata", "previous_response_id", "user", "background", "truncation"):
        assert banned not in siwc_body
    assert siwc_body["store"] is False and siwc_body["stream"] is True and all(m["role"] != "system" for m in siwc_body["input"])
    assert key_body["max_output_tokens"] == 500


@pytest.mark.parametrize("status,code,exc", [
    (403, "subscription_sharing_user_not_eligible", OpenAINotEligible),
    (429, "subscription_sharing_usage_limit_exceeded", OpenAIUsageLimit),
    (503, "subscription_sharing_usage_unavailable", OpenAIUnavailable),
    (503, "subscription_sharing_user_unavailable", OpenAIUnavailable),
    (400, "subscription_sharing_unsupported_capability", OpenAIUnsupported),
    (403, "subscription_sharing_route_not_supported", OpenAIUnsupported),
    (401, "subscription_sharing_invalid_user", OpenAIAuthError),
    (403, "chatpass_v2_scope_not_authorized", OpenAIAuthError),
    (403, "chatpass_v2_invalid_authorization_context", OpenAIAuthError),
])
async def test_every_documented_structured_error_maps_to_its_recovery_class(status, code, exc):
    cl = OpenAIResponsesClient(ApiKeyAuth("sk-test-0123456789"), transport=httpx.MockTransport(lambda r: httpx.Response(status, json={"error": {"code": code, "param": "temperature"}})))
    with pytest.raises(exc):
        await cl.complete("m", "s", "u")


async def test_bare_403_admission_refusal_is_not_retried_as_a_transient_error():
    cl = OpenAIResponsesClient(ApiKeyAuth("sk-test-0123456789"), transport=httpx.MockTransport(lambda r: httpx.Response(403, json={"detail": "region not permitted"})))
    with pytest.raises(OpenAINotEligible):
        await cl.complete("m", "s", "u")


@pytest.mark.parametrize("exc,kind", [(OpenAINotEligible("x"), "not_eligible"), (OpenAIUnsupported("x"), "unsupported")])
async def test_reviewer_surfaces_new_error_kinds(exc, kind):
    async def llm(system, user, cancel):
        raise exc
    from tests.test_reasoning_phase3d import packet
    r = await TradingReviewService(llm=llm).review(packet())
    assert r.verdict is Verdict.ERROR and r.error_kind == kind
