"""'Sign in with ChatGPT' (plan usage) for locally hosted apps - implemented from the OpenAI docs (siwc/token-sharing-open-source):

  OIDC + PKCE(S256) authorization-code flow, system browser, loopback redirect http://127.0.0.1:<port>/auth/callback,
  first sign-in registers dynamically with client_id=dynamic_agent_client (+agent_name_hint) and keeps the ISSUED oaiapp_* id,
  scope `openid profile email offline_access resource.invoke chatgpt.tokens.use.direct`, resource https://api.openai.com/v1,
  ext_agent_host_id = stable opaque urn:uuid:..., token exchange at /api/accounts/oauth/token (form-encoded, no client secret),
  ID-token verification (signature/iss/aud/exp/nonce), identity = `sub`, and the granted scope MUST include chatgpt.tokens.use.direct.

Explicitly NOT done: no scraping of chatgpt.com, no cookies, no undocumented endpoints in the sign-in path. Two details the docs do not
specify are marked UNVERIFIED below (OIDC discovery location for the JWKS, and the refresh-token request shape) and fail closed.
Credentials live OUTSIDE the repository (%LOCALAPPDATA%/TARS/openai_siwc.json), are written atomically, and are never logged."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.parse
import uuid
import webbrowser
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx

ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = f"{ISSUER}/api/accounts/authorize"
TOKEN_URL = f"{ISSUER}/api/accounts/oauth/token"
OIDC_DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"   # UNVERIFIED: standard OIDC location; docs only say 'OpenAI's published JWKS'
RESOURCE = "https://api.openai.com/v1"
SCOPE = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
REQUIRED_SCOPE = "chatgpt.tokens.use.direct"
LOOPBACK_HOST, LOOPBACK_PORT, CALLBACK_PATH = "127.0.0.1", 1455, "/auth/callback"
AGENT_NAME = "TARS"
REFRESH_SKEW_S = 120


class SiwcError(RuntimeError):
    pass


class TokenEndpointError(SiwcError):
    def __init__(self, status: int, error: str) -> None:
        super().__init__(f"token endpoint HTTP {status}" + (f" ({error})" if error else ""))
        self.status, self.error = status, error


class SiwcLoginFailure(SiwcError):
    """Sign-in failed; `.finding` names the stage and `.report` is a fully scrubbed diagnostic record."""

    def __init__(self, finding, report) -> None:
        super().__init__(finding.line())
        self.finding, self.report = finding, report


def default_store_path() -> Path:
    override = os.environ.get("TARS_SIWC_STORE")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.local/share")
    return Path(base) / "TARS" / "openai_siwc.json"


@dataclass
class SiwcCredentials:
    client_id: str
    sub: str
    id_token: str
    access_token: str
    refresh_token: str
    token_type: str
    expires_at: float
    scopes: list[str]
    saved_at: float
    ext_agent_host_id: str
    email_hint: str = ""
    earliest_refresh_at: float = 0.0     # token response field: do not refresh before this instant (0 = unknown)

    def expired(self, skew: float = REFRESH_SKEW_S) -> bool:
        """True when the access token should be renewed: near/after expiry AND not before earliest_refresh_at."""
        now = time.time()
        return now >= self.expires_at - skew and now >= self.earliest_refresh_at

    def hard_expired(self) -> bool:
        return time.time() >= self.expires_at


class CredentialStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else default_store_path()

    def host_id(self) -> str:
        """Stable, opaque, non-identifying host id (urn:uuid:...). Created once, before the first sign-in."""
        doc = self._read()
        if not doc.get("ext_agent_host_id"):
            doc["ext_agent_host_id"] = f"urn:uuid:{uuid.uuid4()}"
            self._write(doc)
        return doc["ext_agent_host_id"]

    def issued_client_id(self) -> str | None:
        return self._read().get("client_id")

    def load(self) -> SiwcCredentials | None:
        doc = self._read().get("credentials")
        return SiwcCredentials(**doc) if doc else None

    def save(self, creds: SiwcCredentials) -> None:
        doc = self._read()
        doc["client_id"] = creds.client_id
        doc["credentials"] = asdict(creds)
        self._write(doc)

    def clear_credentials(self) -> None:
        doc = self._read()
        doc.pop("credentials", None)
        self._write(doc)

    def status(self) -> dict:
        """Non-secret view for UIs and logs."""
        c = self.load()
        if c is None:
            return {"signed_in": False, "client_id": self.issued_client_id(), "reason": "no stored credentials"}
        return {"signed_in": True, "client_id": c.client_id, "account": c.sub[:6] + "…", "scopes": c.scopes, "plan_usage_scope": REQUIRED_SCOPE in c.scopes,
                "access_token_expires_in_s": int(c.expires_at - time.time()), "can_refresh": bool(c.refresh_token)}

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write(self, doc: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)   # atomic
        _restrict_acl(self.path)


def _restrict_acl(path: Path) -> bool:
    """Windows ignores Unix modes: drop inherited ACL entries (e.g. other local groups) and grant only the current user. Best effort; returns success."""
    if os.name != "nt":
        return True
    import getpass
    import subprocess
    try:
        r = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{getpass.getuser()}:(F)"], capture_output=True, text=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False


# ---- PKCE / URLs -------------------------------------------------------------------------------------------------------
# Documented terminal refresh failures (siwc 'Errors and recovery'): the refresh token is unusable, so OAuth must be repeated.
TERMINAL_REFRESH_ERRORS = {"invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired", "refresh_token_invalidated", "refresh_token_reused"}


def parse_instant(value) -> float:
    """earliest_refresh_at may be epoch seconds or an ISO-8601 string; unknown/garbage -> 0 (no restriction)."""
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        from datetime import datetime, timezone
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
    except ValueError:
        return 0.0


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


@dataclass
class AuthAttempt:
    verifier: str
    challenge: str
    state: str
    nonce: str
    redirect_uri: str
    client_id: str
    registering: bool


def new_attempt(client_id: str | None, port: int = LOOPBACK_PORT) -> AuthAttempt:
    verifier = _b64url(secrets.token_bytes(48))
    return AuthAttempt(verifier, _b64url(hashlib.sha256(verifier.encode()).digest()), secrets.token_urlsafe(24), secrets.token_urlsafe(24),
                       f"http://{LOOPBACK_HOST}:{port}{CALLBACK_PATH}", client_id or "dynamic_agent_client", client_id is None)


def authorize_url(a: AuthAttempt, host_id: str, *, id_token_hint: str | None = None, login_hint: str | None = None) -> str:
    q = {"client_id": a.client_id, "ext_agent_host_id": host_id, "response_type": "code", "redirect_uri": a.redirect_uri, "scope": SCOPE,
         "resource": RESOURCE, "state": a.state, "nonce": a.nonce, "code_challenge_method": "S256", "code_challenge": a.challenge}
    if a.registering:
        q["agent_name_hint"] = AGENT_NAME     # only during initial dynamic registration
    elif id_token_hint:                        # returning sign-in: skips the account selector; the hint may be expired
        q["id_token_hint"] = id_token_hint
        if login_hint:
            q["login_hint"] = login_hint
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(q)


class _Callback(BaseHTTPRequestHandler):
    result: dict = {}
    hits: list = []

    def do_GET(self):  # noqa: N802
        from .siwc_diag import SENSITIVE_PARAMS, scrub
        u = urllib.parse.urlparse(self.path)
        params = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        type(self).hits.append({"path": u.path, "t": time.time(), "param_names": sorted(params),
                                "safe_params": {k: ("<present>" if k in SENSITIVE_PARAMS else scrub(v, 120)) for k, v in params.items()}})
        if u.path != CALLBACK_PATH:
            self.send_response(404)
            self.end_headers()
            return
        type(self).result = params
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"TARS: sign-in received. You can close this tab.")

    def log_message(self, *a):   # never log the query string (it carries the authorization code)
        pass


def validate_callback(params: dict, a: AuthAttempt) -> tuple[str, str]:
    """Return (code, client_id). Raises SiwcError on denial, state mismatch, missing/changed client id."""
    if params.get("state") != a.state:
        raise SiwcError("state mismatch (possible CSRF); sign-in aborted")
    if params.get("error"):
        raise SiwcError(f"authorization {params['error']}" + (" (user denied access)" if params["error"] == "access_denied" else ""))
    code = params.get("code")
    if not code:
        raise SiwcError("callback carried no authorization code")
    issued = params.get("client_id")
    if a.registering:
        if not issued or issued == "dynamic_agent_client":
            raise SiwcError("registration incomplete: no issued client_id returned")
        return code, issued
    if issued and issued != a.client_id:
        raise SiwcError("callback supplied a different client_id than the pending request")
    return code, a.client_id


# ---- token exchange / validation / refresh --------------------------------------------------------------------------------
async def _post_form(url: str, data: dict, transport=None, timeout: float = 20.0) -> dict:
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as c:
        r = await c.post(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    if r.status_code >= 400:
        try:
            err = r.json().get("error", "")
        except ValueError:
            err = ""
        raise TokenEndpointError(r.status_code, err if isinstance(err, str) else "")
    return r.json()


def verify_id_token(id_token: str, *, client_id: str, nonce: str | None, jwk_client=None) -> dict:
    """Signature (OpenAI JWKS), iss, aud == issued client_id, exp, nonce. Fails closed if the JWKS cannot be resolved."""
    import jwt
    try:
        if jwk_client is None:
            disc = httpx.get(OIDC_DISCOVERY_URL, timeout=15).json()
            jwk_client = jwt.PyJWKClient(disc["jwks_uri"])
        key = jwk_client.get_signing_key_from_jwt(id_token).key
        claims = jwt.decode(id_token, key, algorithms=["RS256", "ES256"], audience=client_id, issuer=ISSUER, options={"require": ["exp", "iss", "aud", "sub"]})
    except Exception as exc:
        raise SiwcError(f"ID token verification failed: {type(exc).__name__}") from exc
    if nonce is not None and claims.get("nonce") != nonce:
        raise SiwcError("ID token nonce mismatch")
    return claims


def credentials_from_token_response(tok: dict, claims: dict, client_id: str, host_id: str) -> SiwcCredentials:
    scopes = (tok.get("scope") or "").split()
    if REQUIRED_SCOPE not in scopes:
        raise SiwcError(f"granted scopes lack {REQUIRED_SCOPE}: a valid ID token alone does not grant plan usage")
    now = time.time()
    return SiwcCredentials(client_id, claims["sub"], tok["id_token"], tok["access_token"], tok.get("refresh_token", ""), tok.get("token_type", "Bearer"),
                           now + float(tok.get("expires_in", 3600)), scopes, now, host_id, email_hint=claims.get("email", ""),
                           earliest_refresh_at=parse_instant(tok.get("earliest_refresh_at")))


async def exchange_code(a: AuthAttempt, code: str, client_id: str, host_id: str, *, transport=None, jwk_client=None) -> SiwcCredentials:
    tok = await _post_form(TOKEN_URL, {"grant_type": "authorization_code", "client_id": client_id, "code": code, "code_verifier": a.verifier,
                                       "redirect_uri": a.redirect_uri, "resource": RESOURCE}, transport)
    claims = verify_id_token(tok["id_token"], client_id=client_id, nonce=a.nonce, jwk_client=jwk_client)
    return credentials_from_token_response(tok, claims, client_id, host_id)


async def refresh_credentials(creds: SiwcCredentials, *, transport=None) -> SiwcCredentials:
    """UNVERIFIED request shape (docs: refresh tokens rotate; endpoint/format not specified): standard OAuth refresh at the documented
    token endpoint. Any failure means the user must sign in again. Access token, expiry, scopes and refresh token are replaced together."""
    if not creds.refresh_token:
        raise SiwcError("no refresh token; sign in again")
    tok = await _post_form(TOKEN_URL, {"grant_type": "refresh_token", "client_id": creds.client_id, "refresh_token": creds.refresh_token, "resource": RESOURCE}, transport)
    scopes = (tok.get("scope") or " ".join(creds.scopes)).split()
    if REQUIRED_SCOPE not in scopes:
        raise SiwcError("refreshed credentials lost the plan-usage scope")
    now = time.time()
    return SiwcCredentials(creds.client_id, creds.sub, tok.get("id_token", creds.id_token), tok["access_token"], tok.get("refresh_token", creds.refresh_token),
                           tok.get("token_type", creds.token_type), now + float(tok.get("expires_in", 3600)), scopes, now, creds.ext_agent_host_id, creds.email_hint,
                           earliest_refresh_at=parse_instant(tok.get("earliest_refresh_at")))


# ---- interactive login (needs a human + browser) ---------------------------------------------------------------------------
def _preflight(log, port: int) -> tuple[bool, list]:
    """Local, credential-free checks: reachability, clock skew, loopback listener self-test. Returns (ok, findings)."""
    from .siwc_diag import Finding
    findings, ok = [], True
    try:
        r = httpx.get(OIDC_DISCOVERY_URL, timeout=15)
        disc = r.json()
        good = disc.get("authorization_endpoint") == AUTHORIZE_URL and disc.get("token_endpoint") == TOKEN_URL
        log.add("PREFLIGHT", good, f"OIDC discovery HTTP {r.status_code}; endpoints match documented URLs: {good}")
        ok &= good
        date = r.headers.get("date")
        if date:
            from email.utils import parsedate_to_datetime
            skew = abs((parsedate_to_datetime(date).timestamp()) - time.time())
            log.add("PREFLIGHT", skew < 120, f"clock skew vs OpenAI server: {skew:.0f}s")
            if skew >= 120:
                findings.append(Finding("PREFLIGHT", "FAILED", "clock_skew", f"local clock differs from OpenAI by {skew:.0f}s: fix system time", []))
                ok = False
    except Exception as exc:
        log.add("PREFLIGHT", False, f"cannot reach auth.openai.com discovery: {type(exc).__name__}")
        findings.append(Finding("PREFLIGHT", "FAILED", "unreachable", "auth.openai.com is not reachable from this machine", [type(exc).__name__]))
        ok = False
    return ok, findings


async def login(store: CredentialStore | None = None, *, open_browser: bool = True, timeout_s: float = 300.0, jwk_client=None, show_url: bool = False,
                port: int = LOOPBACK_PORT, log=None, opener=webbrowser.open) -> SiwcCredentials:
    from .siwc_diag import DiagLog, Finding, classify_callback, classify_timeout, classify_token_error, redact_url
    store = store or CredentialStore()
    log = log or DiagLog()
    host_id = store.host_id()
    prior = store.load()
    a = new_attempt(store.issued_client_id() if prior else None, port)
    pre_ok, pre_findings = _preflight(log, port)
    try:
        server = HTTPServer((LOOPBACK_HOST, port), _Callback)   # listener starts BEFORE the browser opens
    except OSError as exc:
        f = Finding("PREFLIGHT", "FAILED", "port_in_use", f"cannot bind {LOOPBACK_HOST}:{port} ({exc.__class__.__name__}); another sign-in or tool (e.g. Codex login) holds the port",
                    [], "close the other process and retry; only the port may vary after the first registration")
        log.add("PREFLIGHT", False, f.summary)
        raise SiwcLoginFailure(f, log.report([f]))
    _Callback.result, _Callback.hits = {}, []
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    launched = False
    try:
        # listener self-test with a synthetic request proves the callback stage works on this machine
        try:
            r = httpx.get(f"http://{LOOPBACK_HOST}:{port}{CALLBACK_PATH}?error=selftest", timeout=5)
            log.add("CALLBACK", r.status_code == 200, "loopback listener self-test " + ("passed" if r.status_code == 200 else f"failed HTTP {r.status_code}"))
        except Exception as exc:
            log.add("CALLBACK", False, f"loopback listener self-test failed: {type(exc).__name__}")
        _Callback.result, _Callback.hits = {}, []
        url = authorize_url(a, host_id, id_token_hint=prior.id_token if prior else None, login_hint=prior.email_hint if prior else None)
        log.add("REQUEST", True, "authorize request built: " + redact_url(url))
        print("Opening the system browser for Sign in with ChatGPT (sign in, then approve the permissions).")
        print("Request:", redact_url(url))
        if show_url:
            print("Manual URL:", url if not prior else url.split("&id_token_hint=")[0])
        if open_browser:
            launched = bool(opener(url))
            log.add("BROWSER_AUTH", launched, "system browser launched" if launched else "system browser could not be launched")
        t0 = time.time()
        beat = 0
        while not _Callback.result and time.time() - t0 < timeout_s:
            await asyncio.sleep(0.25)
            if int(time.time() - t0) // 30 > beat:
                beat += 1
                log.add("BROWSER_AUTH", None, f"waiting for the browser to reach the loopback callback ({int(time.time() - t0)}s, listener hits: {len(_Callback.hits)})")
        if not _Callback.result:
            f = classify_timeout(_Callback.hits, pre_ok, launched)
            log.add(f.stage, False, f.summary)
            raise SiwcLoginFailure(f, log.report([*pre_findings, f]))
        f = classify_callback(_Callback.result, expected_state=a.state, registering=a.registering)
        log.add(f.stage, f.verdict == "OK", f.summary)
        if f.verdict != "OK":
            raise SiwcLoginFailure(f, log.report([*pre_findings, f]))
        code, client_id = validate_callback(_Callback.result, a)
    finally:
        server.shutdown()
        server.server_close()
    try:
        creds = await exchange_code(a, code, client_id, host_id, jwk_client=jwk_client)
    except TokenEndpointError as exc:
        f = classify_token_error(exc.status, exc.error)
        log.add(f.stage, False, f.summary)
        raise SiwcLoginFailure(f, log.report([f])) from exc
    except SiwcError as exc:
        stage = "PLAN_SCOPE" if REQUIRED_SCOPE in str(exc) else "ID_TOKEN"
        f = Finding(stage, "FAILED", "validation_failed", str(exc)[:200], [])
        log.add(stage, False, f.summary)
        raise SiwcLoginFailure(f, log.report([f])) from exc
    log.add("TOKEN_EXCHANGE", True, "tokens received, ID token verified, plan-usage scope granted")
    store.save(creds)
    return creds


class SiwcAuth:
    """AuthSource backed by stored SIWC credentials with serialized, rotating refresh.

    Credentials are deleted ONLY when the refresh token is proven unusable (documented terminal codes) AND the access token can no longer be
    used. Network/5xx/early-refresh failures keep the session (docs: 'Don't delete credentials over temporary network or infrastructure failures')."""
    mode = "siwc"

    def __init__(self, store: CredentialStore | None = None, transport=None) -> None:
        self.store, self._transport, self._lock = store or CredentialStore(), transport, asyncio.Lock()
        self.last_refresh: dict = {}      # non-secret telemetry for diagnostics/tests

    async def _refresh_and_save(self, c: SiwcCredentials) -> SiwcCredentials:
        new = await refresh_credentials(c, transport=self._transport)
        self.store.save(new)              # persist the rotated refresh token BEFORE anything else uses it
        self.last_refresh = {"ok": True, "at": time.time()}
        return new

    async def bearer(self) -> str:
        from .openai_client import OpenAIAuthError, OpenAIUnavailable
        async with self._lock:
            c = self.store.load()
            if c is None:
                raise OpenAIAuthError("not signed in with ChatGPT")
            if not c.expired():
                return c.access_token
            try:
                c = await self._refresh_and_save(c)
            except TokenEndpointError as exc:
                self.last_refresh = {"ok": False, "status": exc.status, "error": exc.error, "terminal": exc.error in TERMINAL_REFRESH_ERRORS}
                if exc.error in TERMINAL_REFRESH_ERRORS and c.hard_expired():
                    self.store.clear_credentials()
                    raise OpenAIAuthError(f"ChatGPT session expired and cannot be renewed ({exc.error}); sign in again") from exc
                if c.hard_expired():
                    raise OpenAIUnavailable(f"could not renew the ChatGPT session right now ({exc.error or exc.status}); will retry") from exc
                # still-valid access token: keep using it, keep the credentials, try the refresh again later
            except SiwcError as exc:
                self.last_refresh = {"ok": False, "error": type(exc).__name__}
                if c.hard_expired():
                    raise OpenAIUnavailable(f"could not renew the ChatGPT session right now: {type(exc).__name__}") from exc
            except httpx.HTTPError as exc:
                self.last_refresh = {"ok": False, "error": type(exc).__name__}
                if c.hard_expired():
                    raise OpenAIUnavailable(f"could not reach OpenAI to renew the session: {type(exc).__name__}") from exc
            return c.access_token

    async def on_unauthorized(self) -> bool:
        """The server rejected the access token (401): renew once. True -> caller may retry the request."""
        async with self._lock:
            c = self.store.load()
            if c is None:
                return False
            try:
                await self._refresh_and_save(c)
                return True
            except TokenEndpointError as exc:
                self.last_refresh = {"ok": False, "status": exc.status, "error": exc.error, "terminal": exc.error in TERMINAL_REFRESH_ERRORS}
                if exc.error in TERMINAL_REFRESH_ERRORS:
                    self.store.clear_credentials()      # revoked/expired for real: forget it so status() tells the truth
                return False
            except (SiwcError, httpx.HTTPError) as exc:
                self.last_refresh = {"ok": False, "error": type(exc).__name__}
                return False


async def revoke_refresh_token(creds: SiwcCredentials, *, transport=None, discovery: dict | None = None) -> bool:
    """Docs ('End the renewable session'): POST token, token_type_hint=refresh_token, client_id to the discovery document's revocation_endpoint.
    An empty HTTP 200 is success (also for an already-invalid token). Returns True only when revocation was confirmed."""
    if not creds.refresh_token:
        return True
    try:
        if discovery is None:
            async with httpx.AsyncClient(timeout=15, transport=transport) as c:
                discovery = (await c.get(OIDC_DISCOVERY_URL)).json()
        endpoint = discovery["revocation_endpoint"]
        async with httpx.AsyncClient(timeout=15, transport=transport) as c:
            r = await c.post(endpoint, data={"token": creds.refresh_token, "token_type_hint": "refresh_token", "client_id": creds.client_id})
        return r.status_code == 200
    except Exception:
        return False


async def sign_out(store: CredentialStore | None = None, *, transport=None) -> dict:
    """Stop using the tokens: attempt remote revocation first, then clear the local tokens either way (keeping the client/host mapping)."""
    store = store or CredentialStore()
    creds = store.load()
    confirmed = await revoke_refresh_token(creds, transport=transport) if creds else True
    store.clear_credentials()
    return {"signed_out": True, "remote_revocation_confirmed": confirmed,
            "note": None if confirmed else "remote revocation was NOT confirmed; disconnect the app in ChatGPT Settings to be certain"}


if __name__ == "__main__":   # python -m reasoning.siwc [--show-url] [--timeout 120]  -> interactive sign-in with stage diagnostics
    import argparse
    from .siwc_diag import DiagLog
    ap = argparse.ArgumentParser()
    ap.add_argument("--show-url", action="store_true", help="print the full authorize URL for manual opening")
    ap.add_argument("--timeout", type=float, default=300.0)
    args = ap.parse_args()
    dl = DiagLog()
    try:
        asyncio.run(login(show_url=args.show_url, timeout_s=args.timeout, log=dl))
        print("SIGNED IN:", CredentialStore().status())
    except SiwcLoginFailure as exc:
        print("SIGN-IN FAILED AT STAGE:", exc.finding.line())
        for line in exc.finding.evidence:
            print("  evidence:", line)
        if exc.finding.next_step:
            print("  next step:", exc.finding.next_step)
        out = default_store_path().with_name("siwc_last_diagnostic.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(exc.report, indent=1), encoding="utf-8")
        print("  scrubbed diagnostic written to", out)
        raise SystemExit(2)
