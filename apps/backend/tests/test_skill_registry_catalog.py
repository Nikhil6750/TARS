from __future__ import annotations

import gzip
import json

import httpx
import pytest

from skill_registry.catalog import (
    CatalogFetchError,
    fetch_hermes_catalog,
    load_catalog_from_gzip,
    record_content_hash,
    save_gzip_snapshot,
    validate_catalog_schema,
)

_VALID_RECORD = {
    "name": "example",
    "description": "An example skill.",
    "source": "official",
    "identifier": "official/example",
    "trust_level": "builtin",
    "repo": "org/repo",
    "path": "skills/example",
    "tags": ["example"],
    "extra": {},
}


def test_validate_catalog_schema_accepts_well_formed_payload():
    payload = {"version": 1, "skill_count": 1, "skills": [_VALID_RECORD]}
    skills = validate_catalog_schema(payload)
    assert skills == [_VALID_RECORD]


def test_validate_catalog_schema_rejects_non_dict_root():
    with pytest.raises(CatalogFetchError):
        validate_catalog_schema([_VALID_RECORD])  # type: ignore[arg-type]


def test_validate_catalog_schema_rejects_empty_skills():
    with pytest.raises(CatalogFetchError):
        validate_catalog_schema({"skills": []})


def test_validate_catalog_schema_rejects_mismatched_declared_count():
    with pytest.raises(CatalogFetchError):
        validate_catalog_schema({"skill_count": 5, "skills": [_VALID_RECORD]})


def test_validate_catalog_schema_rejects_record_missing_required_key():
    bad_record = dict(_VALID_RECORD)
    del bad_record["trust_level"]
    with pytest.raises(CatalogFetchError, match="missing keys"):
        validate_catalog_schema({"skills": [bad_record]})


def test_validate_catalog_schema_rejects_empty_identifier():
    bad_record = dict(_VALID_RECORD)
    bad_record["identifier"] = "  "
    with pytest.raises(CatalogFetchError, match="invalid identifier"):
        validate_catalog_schema({"skills": [bad_record]})


async def test_fetch_hermes_catalog_rejects_non_200(monkeypatch):
    class _FakeResponse:
        status_code = 500
        content = b"{}"

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return _FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient())

    with pytest.raises(CatalogFetchError, match="HTTP 500"):
        await fetch_hermes_catalog()


async def test_fetch_hermes_catalog_rejects_invalid_json(monkeypatch):
    class _FakeResponse:
        status_code = 200
        content = b"not json"

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return _FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient())

    with pytest.raises(CatalogFetchError, match="not valid JSON"):
        await fetch_hermes_catalog()


async def test_fetch_hermes_catalog_accepts_valid_payload(monkeypatch):
    body = json.dumps({"version": 1, "skill_count": 1, "skills": [_VALID_RECORD]}).encode()

    class _FakeResponse:
        status_code = 200
        content = body

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return _FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient())

    payload = await fetch_hermes_catalog()
    assert payload.record_count == 1
    assert payload.acquisition_method == "hosted_primary"
    assert len(payload.sha256) == 64


def test_gzip_roundtrip_preserves_records(tmp_path):
    payload_dict = {"version": 1, "skill_count": 1, "skills": [_VALID_RECORD]}
    raw = json.dumps(payload_dict).encode("utf-8")

    from skill_registry.catalog import CatalogPayload

    payload = CatalogPayload(
        version=1,
        generated_at="now",
        record_count=1,
        skills=[_VALID_RECORD],
        raw_bytes=raw,
        sha256="abc",
        acquisition_method="hosted_primary",
        source_url="https://example.test",
    )
    dest = tmp_path / "catalog.json.gz"
    size = save_gzip_snapshot(payload, dest)
    assert size > 0
    assert dest.exists()
    assert gzip.decompress(dest.read_bytes()) == raw

    loaded = load_catalog_from_gzip(dest)
    assert loaded.record_count == 1
    assert loaded.acquisition_method == "local_fallback"
    assert loaded.skills == [_VALID_RECORD]


def test_load_catalog_from_gzip_rejects_invalid_schema(tmp_path):
    dest = tmp_path / "bad.json.gz"
    dest.write_bytes(gzip.compress(json.dumps({"skills": []}).encode()))
    with pytest.raises(CatalogFetchError):
        load_catalog_from_gzip(dest)


def test_record_content_hash_is_stable_and_order_independent():
    a = {"identifier": "x", "tags": ["a", "b"]}
    b = {"tags": ["a", "b"], "identifier": "x"}
    assert record_content_hash(a) == record_content_hash(b)

    c = {"identifier": "x", "tags": ["a", "c"]}
    assert record_content_hash(a) != record_content_hash(c)
