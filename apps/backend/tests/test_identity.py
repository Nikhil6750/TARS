"""Backend build and process identity (task P1-C1).

The point of this identity is that a supervisor can refuse to adopt a
backend it does not recognise, so these tests are about the two properties
that make it usable for that: an instance id that is stable within a process
and unique across processes, and a build id that actually changes when the
code does.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from app import identity as ident

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parents[1]


# ---- instance id -------------------------------------------------------


def test_instance_id_is_stable_within_a_process():
    assert ident.instance_id() == ident.instance_id()
    assert ident.identity()["instance_id"] == ident.instance_id()


def test_instance_id_differs_between_processes():
    """Two interpreters must not agree, or the supervisor could not tell a
    restarted backend from the one it started."""
    code = (
        f"import sys; sys.path.insert(0, r'{BACKEND_DIR}'); "
        "from app.identity import instance_id; print(instance_id())"
    )
    ids = set()
    for _ in range(2):
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=True
        )
        ids.add(out.stdout.strip())
    assert len(ids) == 2, f"instance ids collided across processes: {ids}"
    assert all(len(i) == 16 for i in ids)


def test_instance_id_is_not_derived_from_the_build():
    """An orphan of the *same* build must still be distinguishable, which it
    is not if the instance id is a function of the code."""
    assert ident.instance_id() != ident.build_identity()[0]


# ---- build id ----------------------------------------------------------


def test_build_identity_reports_its_own_source():
    build_id, source = ident.build_identity()
    assert build_id
    assert source in {"git", "version", "env"}


def test_build_id_tracks_the_commit_when_a_repository_is_readable():
    """Where git is available the build id must be the commit, because that
    is the only value that changes whenever the code does."""
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("no repository here; the version fallback is covered below")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=10, check=False,
    )
    if head.returncode != 0:
        pytest.skip("git present but HEAD is unreadable")
    build_id, source = ident.build_identity()
    assert source == "git"
    assert head.stdout.strip().startswith(build_id)


def test_build_id_differs_across_builds(monkeypatch):
    """Two different commits must not produce the same build id -- the whole
    point is that an older build is recognisable as older."""
    ident.build_identity.cache_clear()
    try:
        monkeypatch.setattr(ident, "_git_commit", lambda: "a" * 40)
        first = ident.build_identity()
        ident.build_identity.cache_clear()
        monkeypatch.setattr(ident, "_git_commit", lambda: "b" * 40)
        second = ident.build_identity()
        assert first[0] != second[0]
        assert first[1] == second[1] == "git"
    finally:
        ident.build_identity.cache_clear()


def test_build_id_falls_back_to_the_version_without_a_repository(monkeypatch):
    """An installed build has no .git. It must still answer, and must say
    that what it answered is a version rather than a commit."""
    ident.build_identity.cache_clear()
    try:
        monkeypatch.delenv("TARS_BUILD_ID", raising=False)
        monkeypatch.setattr(ident, "_git_commit", lambda: None)
        build_id, source = ident.build_identity()
        assert build_id == ident.APP_VERSION
        assert source == "version", "a version must never be reported as a commit"
    finally:
        ident.build_identity.cache_clear()


def test_an_explicit_build_id_wins(monkeypatch):
    """Packaging can stamp the build it produced."""
    ident.build_identity.cache_clear()
    try:
        monkeypatch.setenv("TARS_BUILD_ID", "packaged-123")
        assert ident.build_identity() == ("packaged-123", "env")
    finally:
        ident.build_identity.cache_clear()


def test_identity_never_raises_when_git_is_broken(monkeypatch):
    """Health is what the supervisor polls; it must not fail over a missing
    or broken git."""
    ident.build_identity.cache_clear()
    try:
        def boom(*_a, **_k):
            raise OSError("git is not installed")
        monkeypatch.setattr(subprocess, "run", boom)
        block = ident.identity()
        assert block["build_id"]
        assert block["instance_id"]
    finally:
        ident.build_identity.cache_clear()


# ---- drift against the desktop shell -----------------------------------


def test_app_version_matches_the_desktop_shell():
    """A supervisor comparing its own version against the backend's is
    comparing these two numbers, so they must not drift apart."""
    conf = REPO_ROOT / "apps" / "web" / "src-tauri" / "tauri.conf.json"
    if not conf.exists():
        pytest.skip("apps/web is not present in this checkout")
    declared = json.loads(conf.read_text(encoding="utf-8")).get("version")
    assert declared == ident.APP_VERSION, (
        "apps/backend/app/identity.py APP_VERSION and tauri.conf.json version "
        f"disagree ({ident.APP_VERSION} vs {declared})"
    )
    assert ident.shell_version() == ident.APP_VERSION


# ---- the health payload ------------------------------------------------


def test_health_serves_the_identity_block(client):
    body = client.get("/api/v1/health").json()
    block = body["identity"]
    assert block["instance_id"] == ident.instance_id()
    assert block["build_id"] == ident.build_identity()[0]
    assert block["build_source"] == ident.build_identity()[1]
    assert block["app_version"] == ident.APP_VERSION
    assert block["started_at"]


def test_identity_is_additive_and_breaks_no_existing_consumer(client):
    """The supervisor's health poll and the frontend both read this payload;
    adding identity must not disturb what they already read."""
    body = client.get("/api/v1/health").json()
    for field in (
        "status", "tars_env", "database", "assistant_provider", "stt_provider",
        "tts_provider", "wake_word_provider", "live_trading_enabled",
        "voice_mode", "active_voice_lane", "gemini_live_health",
    ):
        assert field in body, f"identity must not displace {field}"
    assert body["status"] == "ok"
    assert body["live_trading_enabled"] is False


def test_both_health_routes_carry_identity(client):
    """`/health` is the alias the shell may use; it must not be the one
    route that cannot be identified."""
    assert client.get("/health").json()["identity"]["instance_id"] == ident.instance_id()


# ---- adoption compatibility (coordinator point 5) ----------------------


def test_identity_declares_what_kind_of_process_this_is():
    """A supervisor that polls a port and gets 200 has learned almost
    nothing. This marker is how it knows a TARS backend answered and not an
    unrelated server, which is what stops it adopting an arbitrary process."""
    assert ident.identity()["service"] == ident.SERVICE_NAME
    assert ident.SERVICE_NAME == "tars-backend"


def test_identity_declares_the_api_contract_it_speaks():
    assert ident.identity()["contract_version"] == ident.CONTRACT_VERSION
    assert isinstance(ident.CONTRACT_VERSION, int)


def test_a_matching_backend_is_adoptable():
    assert ident.is_compatible(
        service=ident.SERVICE_NAME, contract_version=ident.CONTRACT_VERSION
    )


@pytest.mark.parametrize(
    "service,contract",
    [
        ("nginx", 1),              # something else entirely on the port
        ("tars-frontend", 1),      # a TARS thing, but not the backend
        (None, 1),                 # no declaration at all
        ("", 1),                   # empty declaration
        ("tars-backend", 0),       # older contract
        ("tars-backend", 99),      # newer, unknown contract
        ("tars-backend", None),    # contract not declared
        ("tars-backend", "1"),     # string where an int is required
    ],
)
def test_anything_that_does_not_match_is_not_adoptable(service, contract):
    """The failure mode this guards against is adopting an orphan from an
    older build, or worse, a process belonging to someone else."""
    assert not ident.is_compatible(service=service, contract_version=contract)


def test_compatibility_does_not_depend_on_the_build():
    """Compatibility and identity are different questions. A backend built
    from a newer commit that still speaks the same contract must remain
    adoptable, or every rebuild would look like an incompatible backend."""
    assert ident.is_compatible(
        service=ident.SERVICE_NAME, contract_version=ident.CONTRACT_VERSION
    )
    # Nothing in the rule reads build_id or instance_id.
    import inspect

    rule = inspect.getsource(ident.is_compatible)
    assert "build_id" not in rule
    assert "instance_id" not in rule


def test_health_carries_everything_a_supervisor_needs_to_decide(client):
    block = client.get("/api/v1/health").json()["identity"]
    assert ident.is_compatible(
        service=block["service"], contract_version=block["contract_version"]
    )
    # And enough to tell one process from another once adopted.
    assert block["instance_id"]
    assert block["build_id"]
