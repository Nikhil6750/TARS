# Handoff — Codex

Owned directories: `tests/`, `tools/` (contract and acceptance
verification, integration/quality harness). Only Codex edits this file. See
[AGENTS.md](../../../AGENTS.md) for the full handoff protocol.

Update this file at the end of every session, using the template below.
Keep only the latest handoff at the top; older entries may be kept below a
`---` separator for history, but are not required reading for the next
session (`CURRENT_STATE.md` is authoritative for that).

---

## Latest handoff

**Branch**: `feature/v1-quality-contracts`
**Commit SHA**: `7f4dc79de210ec2c8babe54d677c2ecc1c1dd3b3` (last implementation commit before this handoff)
**Work completed**:

- Added exact-pinned, reproducible JSON Schema generation for strict Pydantic v2
  models and TypeScript declarations, plus byte-level drift verification.
- Added six valid trading lifecycle fixtures and seven focused malformed
  fixtures covering every requested contract failure.
- Added a standalone public HTTP/WebSocket client with event, state, history,
  invalidation, assistant, and grounding actions. It imports no app internals.
- Added a process-owning acceptance runner with bounded readiness polling,
  isolated SQLite state, zero paid keys, invalid-vault startup, complete process
  tree cleanup, and secret-sentinel log scanning.
- Added 15 black-box acceptance/security checks, including two WebSocket clients,
  reconnect, iPhone/desktop Chromium viewports, local voice-provider status, and
  absence of execution endpoints.
- Recorded BLOCKER/WARNING/PASS evidence in `tests/INDEPENDENT_REVIEW.md`.

**Files changed**: `tests/` (contracts, fixtures, unit, acceptance, review,
dependency lock) and `tools/` (code generation, generated models/types,
standalone client, acceptance runner, security checks); this handoff file only
under shared coordination docs. Canonical schemas and product source were not
modified.

**Interfaces exposed**:

- `python tools/generate_contracts.py [--check]`
- `python tools/tars_test_client.py {health,send-event,active,history,listen,invalidate,ask,verify-grounded}`
- `python tools/run_acceptance.py --backend-command ... --frontend-command ...`
- Route overrides: `TARS_HEALTH_PATH`, `TARS_EVENTS_PATH`, `TARS_ACTIVE_PATH`,
  `TARS_HISTORY_PATH`, `TARS_INVALIDATE_PATH`, `TARS_ASSISTANT_PATH`,
  `TARS_WEBSOCKET_PATH`, `TARS_VOICE_STATUS_PATH`.

**Tests run**:

- `python tools/generate_contracts.py --check` — PASS.
- `python -m pytest tests -q` — 40 passed, 15 skipped; skips require live apps.
- `python -m pip check` — PASS, no broken requirements.
- `npm audit --prefix tools/codegen --audit-level=high` — PASS, zero vulnerabilities.
- Playwright Chromium smoke render at 390x844 — PASS.
- `python -m pytest tests/acceptance --collect-only -q` with acceptance enabled —
  15 tests collected.

**Known limitations**:

- Live backend/frontend acceptance is BLOCKED until both feature branches are
  integrated; no product PASS is claimed yet.
- Shared contracts do not define route paths/envelopes or lifecycle semantic
  relationships; see the warnings in `tests/INDEPENDENT_REVIEW.md`.
- CI target remains coordinator-PROPOSED, so no workflow file was added.

**Exact dependencies required from other agents**:

- Claude Code: expose health, event submit/active/history/invalidate, assistant,
  WebSocket, voice-status, and OpenAPI surfaces; provide route override values if
  defaults differ. Invalidation must broadcast a schema-valid
  `SETUP_INVALIDATED` event correlated by `event_id`; reject >1 MiB requests and
  avoid secret reflection/logging.
- Antigravity: provide the frontend launch command/URL; the shared UI must render
  without console/page errors or horizontal overflow at 390x844 and 1440x900.
- Coordinator: integrate both branches, choose concrete route mappings and a CI
  target, then authorize/run the full harness.

**Next recommended action**: Merge the three Wave 1 branches into an integration
branch, install the pinned test requirements plus Playwright Chromium, map any
non-default routes through environment variables, and run
`tools/run_acceptance.py` with both launch commands. Treat any failed case as an
integration blocker rather than weakening the check.
