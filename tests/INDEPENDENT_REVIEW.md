# Independent contract and acceptance review

Evidence baseline: `feature/v1-quality-contracts` at implementation commit
`7f4dc79de210ec2c8babe54d677c2ecc1c1dd3b3`, based on
`ad6f4bf6bd4dcb5c4039450dc8b8540ce63108e7`.

## BLOCKER

- Full application acceptance evidence cannot exist on this isolated branch:
  `apps/backend/` and `apps/web/` are absent by design. The 15 black-box cases
  collect and the harness is executable, but they remain gated until the Claude
  and Antigravity branches are integrated and their launch commands are known.
  No backend/frontend PASS is claimed without that run.

## WARNING

- Public route names, response envelopes, error statuses, and the voice-status
  surface are not frozen in shared contracts. The driver exposes environment
  overrides for every route, but integration must provide one mapping.
- `trading-event.schema.json` requires only seven fields, while
  `MASTER_SPEC.md` calls all 17 listed fields "minimum fields." Optional numeric,
  direction, reason, warning, strategy, and expiry fields can therefore be
  omitted by a schema-valid producer.
- The schemas validate shape, not lifecycle semantics. For example, they do not
  constrain `SETUP_VALID` to `validation_status=VALID`, or system states to
  null direction/prices. They also do not enforce the assistant description
  that `role=system` implies `input_mode=text`.
- The schemas have no string, array, or total payload size ceilings. The
  acceptance suite therefore requires the HTTP boundary to reject a payload
  above 1 MiB even though canonical schema validation alone permits it.
- Generated TypeScript declarations provide compile-time shape only. A frontend
  must still validate untrusted WebSocket JSON at runtime against the canonical
  schema (or demonstrate equivalent behavior) before the consumer can PASS.
- CI workflow wiring remains coordinator-PROPOSED in `TASKS.md`. The drift test
  is machine-executable now, but no unapproved `.github/workflows` target was
  added.

## PASS

- Both canonical files are valid JSON Schema Draft 2020-12 and remained
  byte-for-byte unchanged.
- Exact-pinned generators produce deterministic Pydantic v2 and TypeScript
  artifacts; temporary regeneration detects file-set and content drift.
- Generated Pydantic models use strict scalar types. All six valid lifecycle
  fixtures pass both canonical and generated-model validation; all seven
  single-fault malformed fixtures fail both.
- The standalone client imports no backend/frontend internals and applies
  bounded HTTP/WebSocket timeouts while supporting health, event submission,
  active/history queries, two-way invalidation workflow, assistant requests,
  and grounded-answer verification.
- The acceptance suite machine-checks startup evidence, health, two simultaneous
  clients, persistence, active-state transition, invalidation propagation,
  malformed rejection, missing-data grounding, zero paid keys, local voice path,
  disconnect/reconnect, iPhone/desktop viewports, and absence of live execution
  endpoints.
- Negative checks cover unexpected fields, a 1 MiB oversized payload, malformed
  assistant requests, secret reflection/logging, an invalid vault directory,
  and execution-like OpenAPI operations.
- Local evidence: `40 passed, 15 skipped` (skips are the explicitly gated live
  application checks), contract drift clean, `pip check` clean, npm audit reports
  zero vulnerabilities, and headless Chromium rendered successfully at the
  iPhone viewport.
