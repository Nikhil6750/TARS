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

    def expired(self, skew: float = REFRESH_SKEW_S) -> bool:
        return time.time() >= self.expires_at - skew


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


# ---- PKCE / URLs -------------------------------------------------------------------------------------------------------
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


def authorize_url(a: AuthAttempt, host_id: str) -> str:
    q = {"client_id": a.client_id, "ext_agent_host_id": host_id, "response_type": "code", "redirect_uri": a.redirect_uri, "scope": SCOPE,
         "resource": RESOURCE, "state": a.state, "nonce": a.nonce, "code_challenge_method": "S256", "code_challenge": a.challenge}
    if a.registering:
        q["agent_name_hint"] = AGENT_NAME     # only during initial dynamic registration
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(q)


class _Callback(BaseHTTPRequestHandler):
    result: dict = {}

    def do_GET(self):  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        if u.path != CALLBACK_PATH:
            self.send_response(404)
            self.end_headers()
            return
        type(self).result = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
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
        raise SiwcError(f"token endpoint HTTP {r.status_code}" + (f" ({err})" if isinstance(err, str) and err else ""))
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
                           now + float(tok.get("expires_in", 3600)), scopes, now, host_id, email_hint=claims.get("email", ""))


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
                           tok.get("token_type", creds.token_type), now + float(tok.get("expires_in", 3600)), scopes, now, creds.ext_agent_host_id, creds.email_hint)


# ---- interactive login (needs a human + browser) ---------------------------------------------------------------------------
async def login(store: CredentialStore | None = None, *, open_browser: bool = True, timeout_s: float = 300.0, jwk_client=None) -> SiwcCredentials:
    store = store or CredentialStore()
    host_id = store.host_id()
    a = new_attempt(store.issued_client_id())
    server = HTTPServer((LOOPBACK_HOST, LOOPBACK_PORT), _Callback)   # listener starts BEFORE the browser opens
    _Callback.result = {}
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    try:
        url = authorize_url(a, host_id)
        print("Open this URL to sign in with ChatGPT (consent required):\n" + url)
        if open_browser:
            webbrowser.open(url)
        t0 = time.time()
        while not _Callback.result and time.time() - t0 < timeout_s:
            await asyncio.sleep(0.25)
        if not _Callback.result:
            raise SiwcError("sign-in timed out")
        code, client_id = validate_callback(_Callback.result, a)
    finally:
        server.shutdown()
    creds = await exchange_code(a, code, client_id, host_id, jwk_client=jwk_client)
    store.save(creds)
    return creds


class SiwcAuth:
    """AuthSource backed by stored SIWC credentials with serialized, rotating refresh."""
    mode = "siwc"

    def __init__(self, store: CredentialStore | None = None, transport=None) -> None:
        self.store, self._transport, self._lock = store or CredentialStore(), transport, asyncio.Lock()

    async def bearer(self) -> str:
        from .openai_client import OpenAIAuthError
        async with self._lock:
            c = self.store.load()
            if c is None:
                raise OpenAIAuthError("not signed in with ChatGPT")
            if c.expired():
                try:
                    c = await refresh_credentials(c, transport=self._transport)
                except SiwcError as exc:
                    self.store.clear_credentials()
                    raise OpenAIAuthError(f"ChatGPT session expired and could not be refreshed: {exc}") from exc
                self.store.save(c)
            return c.access_token

    async def on_unauthorized(self) -> bool:
        async with self._lock:
            c = self.store.load()
            if c is None:
                return False
            try:
                self.store.save(await refresh_credentials(c, transport=self._transport))
                return True
            except SiwcError:
                self.store.clear_credentials()    # revoked: forget it so status() tells the truth
                return False


def sign_out(store: CredentialStore | None = None) -> None:
    """Forget local credentials. NOTE: the docs reviewed do not describe a token-revocation endpoint, so revocation server-side
    must be done by the user in their ChatGPT account settings."""
    (store or CredentialStore()).clear_credentials()


if __name__ == "__main__":   # python -m reasoning.siwc  -> interactive sign-in
    c = asyncio.run(login())
    print(CredentialStore().status())
