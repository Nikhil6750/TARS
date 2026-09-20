"""Fetches and validates the Hermes-aggregated skill catalog (Phase 2).
Never trusts the hosted index without checking HTTP status and JSON shape
first -- a malformed or truncated download must raise, not get silently
normalized into garbage rows.
"""
from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

HERMES_CATALOG_URL = "https://hermes-agent.nousresearch.com/docs/api/skills-index.json"

_REQUIRED_RECORD_KEYS = {
    "name",
    "description",
    "source",
    "identifier",
    "trust_level",
    "repo",
    "path",
    "tags",
    "extra",
}


class CatalogFetchError(RuntimeError):
    """Raised when the catalog can't be fetched or fails schema validation --
    never caught silently in order to fall back to fabricated data."""


@dataclass
class CatalogPayload:
    version: int
    generated_at: str
    record_count: int
    skills: list[dict[str, Any]]
    raw_bytes: bytes
    sha256: str
    acquisition_method: str  # 'hosted_primary' | 'local_fallback'
    source_url: str


def validate_catalog_schema(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Raises CatalogFetchError on any structural problem. Returns the
    `skills` list once validated. A record missing a required key is a
    schema violation for the WHOLE catalog, not silently skipped -- if the
    upstream shape changed, that must surface loudly, not degrade quietly."""
    if not isinstance(payload, dict):
        raise CatalogFetchError("catalog root is not a JSON object")
    skills = payload.get("skills")
    if not isinstance(skills, list) or not skills:
        raise CatalogFetchError("catalog has no non-empty 'skills' array")

    declared_count = payload.get("skill_count")
    if isinstance(declared_count, int) and declared_count != len(skills):
        raise CatalogFetchError(
            f"catalog declares skill_count={declared_count} but skills array has {len(skills)} entries"
        )

    for i, record in enumerate(skills):
        if not isinstance(record, dict):
            raise CatalogFetchError(f"skills[{i}] is not an object")
        missing = _REQUIRED_RECORD_KEYS - record.keys()
        if missing:
            raise CatalogFetchError(f"skills[{i}] (identifier={record.get('identifier')!r}) missing keys: {missing}")
        if not isinstance(record.get("identifier"), str) or not record["identifier"].strip():
            raise CatalogFetchError(f"skills[{i}] has an empty/invalid identifier")

    return skills


async def fetch_hermes_catalog(
    url: str = HERMES_CATALOG_URL, timeout_seconds: float = 60.0
) -> CatalogPayload:
    """Primary path (Phase 2): download the hosted Hermes catalog. Raises
    CatalogFetchError on a non-2xx status or schema mismatch -- callers
    needing the documented fallback (local generation via Hermes's own
    build_skills_index.py) must catch this explicitly, never invent data
    in its place."""
    async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
        try:
            resp = await client.get(url)
        except httpx.HTTPError as exc:
            raise CatalogFetchError(f"could not reach Hermes catalog at {url}: {exc}") from exc

    if resp.status_code != 200:
        raise CatalogFetchError(f"Hermes catalog returned HTTP {resp.status_code} from {url}")

    raw_bytes = resp.content
    try:
        payload = json.loads(raw_bytes)
    except json.JSONDecodeError as exc:
        raise CatalogFetchError(f"Hermes catalog response was not valid JSON: {exc}") from exc

    skills = validate_catalog_schema(payload)
    sha256 = hashlib.sha256(raw_bytes).hexdigest()

    return CatalogPayload(
        version=int(payload.get("version", 1)),
        generated_at=str(payload.get("generated_at", "")),
        record_count=len(skills),
        skills=skills,
        raw_bytes=raw_bytes,
        sha256=sha256,
        acquisition_method="hosted_primary",
        source_url=url,
    )


def load_catalog_from_gzip(path: Path) -> CatalogPayload:
    """Loads the repo-committed compressed snapshot (apps/backend/data/
    catalog/hermes-skills-index.json.gz) -- used by tests and as an offline
    fallback, never silently substituted for a live sync without saying so."""
    raw_bytes = gzip.decompress(path.read_bytes())
    payload = json.loads(raw_bytes)
    skills = validate_catalog_schema(payload)
    sha256 = hashlib.sha256(raw_bytes).hexdigest()
    return CatalogPayload(
        version=int(payload.get("version", 1)),
        generated_at=str(payload.get("generated_at", "")),
        record_count=len(skills),
        skills=skills,
        raw_bytes=raw_bytes,
        sha256=sha256,
        acquisition_method="local_fallback",
        source_url=str(path),
    )


def save_gzip_snapshot(payload: CatalogPayload, dest: Path) -> int:
    """Writes the compressed catalog snapshot to `dest`. Returns the
    compressed size in bytes."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    compressed = gzip.compress(payload.raw_bytes, compresslevel=9)
    dest.write_bytes(compressed)
    return len(compressed)


def record_content_hash(record: dict[str, Any]) -> str:
    """Stable hash of a single catalog record's normalized JSON, used to
    detect whether a re-synced record actually changed."""
    normalized = json.dumps(record, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
