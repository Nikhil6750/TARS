"""Safe, stage-aware diagnostics for Sign in with ChatGPT.

Every record is scrubbed: authorization codes, state, nonce, PKCE verifier/challenge, ID/access/refresh tokens, hints, cookies and
Authorization headers are NEVER stored - only parameter NAMES, HTTP status codes, OAuth error codes and short scrubbed messages.

Stages (in the order a sign-in passes through them):
  PREFLIGHT         local machine: port, browser, listener, clock, reachability
  REQUEST           the authorize request we build vs the documented contract
  BROWSER_AUTH      user authenticates at auth.openai.com (session, login)
  CLIENT_ADMISSION  OpenAI accepts/registers the (dynamic) client for this app/host
  CONSENT           user reviews permissions and approves plan usage
  CALLBACK          browser redirects to our loopback listener
  TOKEN_EXCHANGE    code -> tokens at the token endpoint
  ID_TOKEN          signature / iss / aud / exp / nonce verification
  PLAN_SCOPE        granted scopes include chatgpt.tokens.use.direct
  INFERENCE         Responses API admission (401/403/503 and structured codes)"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

SENSITIVE_PARAMS = {"code", "state", "nonce", "code_verifier", "code_challenge", "id_token", "id_token_hint", "access_token", "refresh_token",
                    "login_hint", "token", "client_secret", "password", "cookie", "authorization"}
SAFE_VALUE_PARAMS = {"error", "error_description", "error_code", "response_type", "code_challenge_method", "scope", "resource", "client_id", "agent_name_hint",
                     "redirect_uri", "screen_hint", "prompt"}
_SECRETISH = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9._\-]{12,}|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]*|oaiapp_[A-Za-z0-9]{6,}|"
                        r"(?:code|state|nonce|verifier|token|cookie)=[^&\s]{6,})", re.I)


def scrub(text: object, limit: int = 300) -> str:
    return _SECRETISH.sub("<redacted>", str(text))[:limit]


def redact_url(url: str) -> str:
    """scheme://host/path plus parameter names; values only for non-sensitive allowlisted parameters."""
    p = urllib.parse.urlparse(url)
    q = urllib.parse.parse_qs(p.query, keep_blank_values=True)
    shown = {}
    for k, v in q.items():
        val = v[0] if v else ""
        if k in SENSITIVE_PARAMS:
            shown[k] = f"<{len(val)} chars, redacted>"
        elif k in SAFE_VALUE_PARAMS:
            shown[k] = ("oaiapp_<id>" if val.startswith("oaiapp_") else scrub(val, 120))
        else:
            shown[k] = f"<{len(val)} chars>"
    return f"{p.scheme}://{p.netloc}{p.path}" + (f"?{json.dumps(shown, sort_keys=True)}" if shown else "")


@dataclass
class Finding:
    stage: str
    verdict: str            # OK | FAILED | BLOCKED | UNKNOWN
    code: str
    summary: str
    evidence: list[str] = field(default_factory=list)
    next_step: str = ""

    def line(self) -> str:
        return f"[{self.stage}] {self.verdict} {self.code}: {self.summary}"


class DiagLog:
    def __init__(self) -> None:
        self.started = time.time()
        self.events: list[dict] = []

    def add(self, stage: str, ok: bool | None, detail: str) -> None:
        self.events.append({"t": round(time.time() - self.started, 2), "stage": stage, "ok": ok, "detail": scrub(detail, 400)})

    def report(self, findings: list[Finding]) -> dict:
        return {"generated_at": datetime.now(timezone.utc).isoformat(), "events": self.events, "findings": [asdict(f) for f in findings]}


# ---- classification of what the browser/callback/token endpoint told us ----------------------------------------------------
def classify_callback(params: dict, *, expected_state: str | None, registering: bool) -> Finding:
    names = sorted(params)
    ev = [f"callback parameter names: {names}"]
    err = params.get("error")
    if expected_state is not None and params.get("state") != expected_state:
        return Finding("CALLBACK", "FAILED", "state_mismatch", "state on the callback does not match this attempt (stale tab, replayed URL, or two concurrent attempts)",
                       ev, "close all OpenAI sign-in tabs, run one attempt only")
    if err == "access_denied":
        return Finding("CONSENT", "FAILED", "access_denied", "the user declined, or ChatGPT plan use is not enabled for this account/workspace", ev,
                       "see 'ChatGPT plan use isn't enabled' in the OpenAI docs; approve the permissions or enable plan usage")
    if err in ("invalid_client", "unauthorized_client", "unsupported_response_type", "invalid_client_metadata", "registration_not_supported"):
        return Finding("CLIENT_ADMISSION", "FAILED", err, "OpenAI did not admit this client/registration", ev + [f"error_description: {scrub(params.get('error_description', ''))}"],
                       "this app/repo is probably not admitted for plan usage: request a client ID / submit OpenAI's interest form")
    if err in ("invalid_request", "invalid_scope", "invalid_request_uri", "invalid_request_object"):
        return Finding("REQUEST", "FAILED", err, "OpenAI rejected a parameter of the authorize request", ev + [f"error_description: {scrub(params.get('error_description', ''))}"],
                       "compare the authorize request with the documented parameter table (siwc_diagnose --validate-request)")
    if err in ("login_required", "interaction_required", "consent_required", "account_selection_required"):
        return Finding("BROWSER_AUTH", "FAILED", err, "OpenAI needs an interactive login/selection that did not complete", ev, "retry in a normal (non-incognito) Chrome profile that is signed in to ChatGPT")
    if err:
        return Finding("UNKNOWN", "FAILED", err, f"OAuth error from the authorization server: {scrub(params.get('error_description', ''))}", ev, "report this code to OpenAI support")
    if not params.get("code"):
        return Finding("CALLBACK", "FAILED", "no_code", "callback arrived without a code or an error", ev, "")
    if registering and (not params.get("client_id") or params.get("client_id") == "dynamic_agent_client"):
        return Finding("CLIENT_ADMISSION", "FAILED", "registration_incomplete", "a new registration must return an issued client_id (oaiapp_...) and did not", ev,
                       "registration was not completed in the browser; treat as not admitted")
    return Finding("CALLBACK", "OK", "callback_received", "callback carries a code and a matching state" + (" and an issued client_id" if registering else ""), ev)


def classify_token_error(status: int, error: str | None) -> Finding:
    ev = [f"HTTP {status}", f"error: {scrub(error or '')}"]
    if error == "invalid_client":
        return Finding("TOKEN_EXCHANGE", "FAILED", error, "token endpoint does not accept this client_id (never exchange with dynamic_agent_client; use the issued oaiapp_* id)", ev,
                       "verify the issued client_id from the callback was the one used")
    if error == "invalid_grant":
        return Finding("TOKEN_EXCHANGE", "FAILED", error, "authorization code/PKCE/redirect_uri mismatch, or code already used or expired", ev,
                       "start a fresh authorization; check the redirect_uri and resource are identical to the authorize request")
    if status in (401, 403):
        return Finding("TOKEN_EXCHANGE", "FAILED", f"http_{status}", "token endpoint refused the request", ev, "")
    return Finding("TOKEN_EXCHANGE", "FAILED", error or f"http_{status}", "token endpoint error", ev, "retry with backoff; if persistent contact OpenAI")


_PAGE_HINTS = (
    (re.compile(r"/(log-?in|sign-?in|authorize/resume|u/login)", re.I), ("BROWSER_AUTH", "UNKNOWN", "login_page", "the browser is at OpenAI's login step: authentication has not completed yet")),
    (re.compile(r"/(consent|authorize/consent)", re.I), ("CONSENT", "UNKNOWN", "consent_page", "the browser is at the consent step")),
    (re.compile(r"/(error|session|expired|ended)", re.I), ("BROWSER_AUTH", "FAILED", "error_page", "OpenAI's own error page was shown inside the authentication flow (before any redirect to the loopback callback)")),
)


def classify_page_url(url: str) -> Finding:
    """Classify the address-bar URL of the page the user's browser ended on. Heuristic, conservative: unknown stays unknown."""
    p = urllib.parse.urlparse(url.strip())
    q = {k: v[0] for k, v in urllib.parse.parse_qs(p.query).items()}
    ev = [f"final page: {redact_url(url)}"]
    if p.hostname == "127.0.0.1":
        return Finding("CALLBACK", "UNKNOWN", "loopback_reached", "the browser DID reach the loopback callback; inspect the callback result instead", ev)
    if p.hostname not in ("auth.openai.com", "chatgpt.com", "auth0.openai.com", "openai.com"):
        return Finding("BROWSER_AUTH", "UNKNOWN", "unexpected_host", "the browser ended on an unexpected host", ev)
    if q.get("error"):
        return classify_callback(q, expected_state=None, registering=False)
    for rx, (stage, verdict, code, summary) in _PAGE_HINTS:
        if rx.search(p.path):
            return Finding(stage, verdict, code, summary, ev, "if this is an error page, retry once in a fresh Chrome window; if it repeats the request is being rejected inside OpenAI's flow")
    return Finding("BROWSER_AUTH", "UNKNOWN", "inside_openai_flow", "the flow stopped inside auth.openai.com before redirecting to the loopback callback: authentication, "
                   "client admission or consent - OpenAI's page URL carries no machine-readable reason", ev,
                   "this cannot be narrowed further from the client side; it needs OpenAI-side information (admission of this app) or a retry with a signed-in profile")


def classify_timeout(listener_hits: list[dict], preflight_ok: bool, browser_launched: bool) -> Finding:
    ev = [f"listener received {len(listener_hits)} request(s)", f"preflight_ok={preflight_ok}", f"browser_launched={browser_launched}"]
    ev += [f"hit: {h['path']} params={h.get('param_names')}" for h in listener_hits[:5]]
    cb = [h for h in listener_hits if h["path"] == "/auth/callback"]
    if cb:
        return classify_callback(cb[-1].get("safe_params", {}), expected_state=None, registering=True)
    if listener_hits:
        return Finding("CALLBACK", "FAILED", "wrong_path", "something reached the loopback listener but not /auth/callback (path must match exactly)", ev)
    if not browser_launched:
        return Finding("BROWSER_AUTH", "FAILED", "browser_not_launched", "the system browser could not be started", ev, "open the printed URL manually")
    return Finding("BROWSER_AUTH", "UNKNOWN", "no_redirect_to_loopback",
                   "nothing ever reached the loopback listener, so the flow stopped inside auth.openai.com (browser authentication, client admission or consent). "
                   "The callback and token-exchange stages were never reached; those are NOT the failure.", ev,
                   "run: python -m reasoning.siwc_diagnose --classify-url \"<address-bar URL of the page Chrome ended on>\"")
