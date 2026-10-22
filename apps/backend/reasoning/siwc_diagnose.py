"""python -m reasoning.siwc_diagnose [--classify-url URL] [--validate-request] [--json PATH]

Credential-free diagnosis of Sign in with ChatGPT, stage by stage. It never prints or stores codes, tokens, PKCE values, cookies or hints.
It does NOT bypass OpenAI's bot protection and does not impersonate any other client: the authorize page itself can only be judged in a real browser."""
from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import sys
import urllib.parse
import uuid
import webbrowser
from pathlib import Path

import httpx

from . import siwc
from .siwc_diag import DiagLog, Finding, classify_page_url, redact_url, scrub

DOC_SCOPE = {"openid", "profile", "email", "offline_access", "resource.invoke", "chatgpt.tokens.use.direct"}


def validate_request(url: str, host_id: str) -> list[tuple[str, bool, str]]:
    """Check an authorize URL against the documented parameter table (docs: siwc/token-sharing-open-source/sign-in, step 2)."""
    u = urllib.parse.urlparse(url)
    q = dict(urllib.parse.parse_qsl(u.query, keep_blank_values=True))
    out: list[tuple[str, bool, str]] = []

    def chk(name, ok, msg):
        out.append((name, bool(ok), msg))
    chk("endpoint", f"{u.scheme}://{u.netloc}{u.path}" == siwc.AUTHORIZE_URL, "https://auth.openai.com/api/accounts/authorize")
    chk("response_type", q.get("response_type") == "code", "must be 'code'")
    registering = q.get("client_id") == "dynamic_agent_client"
    chk("client_id", registering or q.get("client_id", "").startswith("oaiapp_"), "dynamic_agent_client (first time) or the issued oaiapp_* id")
    chk("agent_name_hint", (not registering) or bool(q.get("agent_name_hint")), "required on registration, omitted afterwards")
    chk("agent_name_hint_omitted_on_reauth", registering or "agent_name_hint" not in q, "must be omitted when an issued client_id is used")
    ru = urllib.parse.urlparse(q.get("redirect_uri", ""))
    chk("redirect_uri", ru.scheme == "http" and ru.hostname == "127.0.0.1" and ru.path == "/auth/callback" and "localhost" not in q.get("redirect_uri", ""),
        "http://127.0.0.1:<port>/auth/callback (never localhost; path exactly /auth/callback)")
    chk("scope", set(q.get("scope", "").split()) == DOC_SCOPE, "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct")
    chk("resource", q.get("resource") == "https://api.openai.com/v1", "https://api.openai.com/v1")
    chk("code_challenge_method", q.get("code_challenge_method") == "S256", "S256")
    chk("code_challenge", len(q.get("code_challenge", "")) == 43, "unpadded base64url SHA-256 (43 chars)")
    chk("state", len(q.get("state", "")) >= 16, "fresh random value")
    chk("nonce", len(q.get("nonce", "")) >= 16, "fresh random value")
    hid = q.get("ext_agent_host_id", "")
    uuid_ok = False
    if hid.startswith("urn:uuid:"):
        try:
            uuid_ok = uuid.UUID(hid[9:]).version == 4 and str(uuid.UUID(hid[9:])) == hid[9:]
        except ValueError:
            uuid_ok = False
    chk("ext_agent_host_id", uuid_ok or hid.startswith("urn:ietf:params:oauth:jwk-thumbprint:") or hid.startswith("did:key:"),
        "urn:uuid:<UUIDv4> | urn:ietf:params:oauth:jwk-thumbprint:... | did:key:...; opaque, stable, non-identifying")
    chk("host_id_matches_stored", hid == host_id, "same host id as the persisted one")
    chk("url_length", len(url) < 2000, f"{len(url)} chars (must survive ShellExecute / browser limits)")
    return out


def eligibility_evidence() -> dict:
    """Facts that bear on admission. Docs: plan usage is 'available to all open-source partners and selected private clients'; paid or remotely
    hosted apps use OpenAI's interest form. Nothing here proves or disproves admission; it records what is observable."""
    ev = {"docs_statement": "plan usage is available to all open-source partners and selected private clients (siwc quickstart)",
          "request_client_id_page": "Sign in with ChatGPT is currently offered to a select group of commercial partners; join via the interest form",
          "repository": None}
    gh = shutil.which("gh")
    if gh:
        try:
            r = subprocess.run([gh, "repo", "view", "--json", "visibility,licenseInfo,nameWithOwner"], capture_output=True, text=True, timeout=20)
            if r.returncode == 0:
                d = json.loads(r.stdout)
                ev["repository"] = {"name": d.get("nameWithOwner"), "visibility": d.get("visibility"), "license": (d.get("licenseInfo") or {}).get("spdxId") or None}
        except Exception as exc:
            ev["repository"] = {"error": type(exc).__name__}
    repo = ev["repository"] or {}
    ev["assessment"] = ("public repository WITHOUT a license: by definition not clearly 'open source'; admission to open-source plan usage is unverified"
                        if repo.get("visibility") == "PUBLIC" and not repo.get("license") else
                        "repository has a license" if repo.get("license") else "repository facts unavailable")
    return ev


def probe_token_endpoint() -> Finding:
    """An intentionally invalid exchange (synthetic values only) to prove the token endpoint is reachable and speaks OAuth."""
    try:
        r = httpx.post(siwc.TOKEN_URL, data={"grant_type": "authorization_code", "client_id": "oaiapp_diagnostic_probe", "code": "diagnostic-invalid", "code_verifier": "x" * 43,
                                              "redirect_uri": "http://127.0.0.1:1455/auth/callback", "resource": siwc.RESOURCE}, timeout=20)
        err = ""
        try:
            err = r.json().get("error", "")
        except ValueError:
            pass
        ok = r.status_code == 400 and bool(err)
        return Finding("TOKEN_EXCHANGE", "OK" if ok else "UNKNOWN", f"reachable_http_{r.status_code}",
                       "token endpoint reachable and returns OAuth errors for an invalid exchange" if ok else "token endpoint reachable but unexpected response",
                       [f"error code: {scrub(err)}", f"cloudflare-mitigated: {r.headers.get('cf-mitigated')}"])
    except Exception as exc:
        return Finding("TOKEN_EXCHANGE", "FAILED", "unreachable", f"token endpoint unreachable: {type(exc).__name__}")


def probe_authorize_bot_wall() -> Finding:
    """Records (does not bypass) that scripted clients meet OpenAI's bot verification on the authorize endpoint."""
    try:
        r = httpx.get(siwc.AUTHORIZE_URL + "?client_id=diagnostic", headers={"User-Agent": "TARS-diagnostic/1.0"}, timeout=20, follow_redirects=False)
        wall = r.headers.get("cf-mitigated") == "challenge"
        return Finding("BROWSER_AUTH", "UNKNOWN", "bot_verification" if wall else f"http_{r.status_code}",
                       "authorize endpoint is behind interactive bot verification for non-browser clients: only a real browser can complete (or fail) it" if wall
                       else f"authorize endpoint answered HTTP {r.status_code} to a scripted client")
    except Exception as exc:
        return Finding("BROWSER_AUTH", "FAILED", "unreachable", f"authorize endpoint unreachable: {type(exc).__name__}")


def port_status(port: int) -> Finding:
    s = socket.socket()
    try:
        s.bind((siwc.LOOPBACK_HOST, port))
        return Finding("PREFLIGHT", "OK", "port_free", f"{siwc.LOOPBACK_HOST}:{port} is free for the callback listener")
    except OSError:
        return Finding("PREFLIGHT", "FAILED", "port_in_use", f"{siwc.LOOPBACK_HOST}:{port} is already in use (another sign-in, or Codex login)")
    finally:
        s.close()


def run_diagnosis() -> tuple[list[Finding], DiagLog, dict]:
    log = DiagLog()
    store = siwc.CredentialStore()
    host = store.host_id()
    findings: list[Finding] = [port_status(siwc.LOOPBACK_PORT)]
    try:
        browser = webbrowser.get()
        findings.append(Finding("PREFLIGHT", "OK", "browser_available", f"system browser handler: {type(browser).__name__}"))
    except webbrowser.Error:
        findings.append(Finding("PREFLIGHT", "FAILED", "no_browser", "no system browser handler found"))
    ok, pre = siwc._preflight(log, siwc.LOOPBACK_PORT)
    findings.append(Finding("PREFLIGHT", "OK" if ok else "FAILED", "reachability_clock", "OIDC discovery matches the documented endpoints; clock within 2 minutes" if ok else "see events"))
    findings += pre
    a = siwc.new_attempt(store.issued_client_id() if store.load() else None)
    checks = validate_request(siwc.authorize_url(a, host), host)
    bad = [c for c in checks if not c[1]]
    findings.append(Finding("REQUEST", "OK" if not bad else "FAILED", "documented_contract",
                            "the authorize request matches the documented parameter table" if not bad else "request deviates from the documented table",
                            [f"{n}: expected {m}" for n, ok_, m in bad] or [f"{len(checks)} checks passed"]))
    findings.append(probe_authorize_bot_wall())
    findings.append(probe_token_endpoint())
    elig = eligibility_evidence()
    findings.append(Finding("CLIENT_ADMISSION", "UNKNOWN", "admission_unverifiable_from_client", elig["assessment"],
                            [elig["docs_statement"], elig["request_client_id_page"], f"repository: {elig['repository']}"],
                            "if the browser flow keeps failing inside auth.openai.com, admission is the leading suspect: add an OSI license and/or submit OpenAI's interest form"))
    findings.append(Finding("INFERENCE", "OK" if store.status().get("plan_usage_scope") else "BLOCKED", "signed_in" if store.status().get("signed_in") else "not_signed_in",
                            "stored credentials include the plan-usage scope" if store.status().get("plan_usage_scope") else "no usable ChatGPT sign-in stored"))
    return findings, log, {"store": store.status(), "eligibility": elig}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--classify-url", help="address-bar URL of the page the browser ended on (query values are redacted)")
    ap.add_argument("--validate-request", action="store_true")
    ap.add_argument("--json", help="write the scrubbed report here")
    a = ap.parse_args(argv)
    if a.classify_url:
        f = classify_page_url(a.classify_url)
        print(f.line())
        for e in f.evidence:
            print("  ", e)
        print("  next:", f.next_step or "-")
        return 0
    findings, log, extra = run_diagnosis()
    for f in findings:
        print(f.line())
        for e in f.evidence:
            print("    -", scrub(e, 240))
        if f.next_step and f.verdict in ("FAILED", "UNKNOWN", "BLOCKED"):
            print("    next:", f.next_step)
    if a.json:
        Path(a.json).write_text(json.dumps({**log.report(findings), **extra}, indent=1), encoding="utf-8")
        print("report:", a.json)
    return 1 if any(f.verdict == "FAILED" for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
