# M2A_INTERFACES.md — Wave 2A Shared Contracts

Companion to [M2A_SPEC.md](M2A_SPEC.md). This is the interface reference for
the objects every Wave 2A stream (Claude Code's skills, Codex's action
runtime, Antigravity's native shell/HUD) programs against. Same rule as V1's
`contracts/`: these are frozen once a stream starts depending on them; a
needed change is recorded in a handoff and escalated, not edited unilaterally.

## Canonical sources

- `contracts/action-request.schema.json` — frozen JSON Schema, source of
  truth for `ActionRequest`.
- `contracts/action-result.schema.json` — frozen JSON Schema, source of
  truth for `ActionResult`.
- `apps/backend/app/action_contracts.py` — hand-written Pydantic v2 mirror
  of both schemas, plus `RiskLevel`, `ActionSource`, `ActionStatus`,
  `ActiveWindowContext`, `WindowBounds`, the `Skill` Protocol, and the
  `BaseSkill` ABC. Validated at runtime against the JSON Schemas via
  `app.contracts.validate_action_request` / `validate_action_result` —
  exactly the same pattern V1 uses for `TradingEvent`/`AssistantMessage`
  (`app/schemas.py` + `app/contracts.py`), so this module cannot silently
  drift from the frozen schema.
- Codegen note: `tools/generate_contracts.py`'s `SCHEMAS` tuple does not
  yet include the two new schemas (adding Python/TypeScript generated
  artifacts under `tools/generated/` is `tools/`-owned, i.e. Codex's call,
  and requires `npm ci --prefix tools/codegen`, deliberately not run as
  part of this base commit per "no unnecessary dependency reinstalls").
  Until that lands, the native shell/HUD (TypeScript/Rust) should treat the
  JSON Schema files as the source of truth for the wire shape and the
  enums below for the closed value sets.

## RiskLevel

```python
class RiskLevel(str, Enum):
    READ_ONLY = "READ_ONLY"            # e.g. list files, search Obsidian, read a note
    LOW_RISK = "LOW_RISK"              # e.g. launch/focus an app, open a URL
    CONFIRM_REQUIRED = "CONFIRM_REQUIRED"  # e.g. state-changing terminal command
    BLOCKED = "BLOCKED"                # e.g. destructive/elevated/system-critical
```

A `Skill.classify_risk(action, arguments)` call is a pure function — no I/O,
no randomness — so the action runtime can call it ahead of `execute()` to
decide whether to prompt. The action runtime is the enforcement point: it
re-derives/checks the classification rather than trusting a skill's
self-report blindly, and `BLOCKED` is never executable regardless of
confirmation. This is what makes the permission system deterministic and
not bypassable by an LLM (M2A acceptance criterion 9): the LLM's only
possible role is producing the `ActionRequest` itself; every request,
regardless of origin, passes through the same `classify_risk` →
runtime-enforce → `execute` path.

## ActionRequest

| Field | Type | Notes |
|---|---|---|
| `id` | UUID | Correlates to `ActionResult.request_id` |
| `skill` | str | Registered skill name (`windows_app`, `filesystem`, `browser`, `terminal`, `obsidian`, ...) |
| `action` | str | Skill-specific verb (`launch`, `focus`, `open_url`, `run_command`, `search`, ...) |
| `arguments` | dict | Skill-defined shape; the target skill's `validate()` is authoritative, not this contract |
| `source` | `ActionSource` | `hud \| voice_ptt \| voice_wake_word \| hotkey \| deterministic \| api` |
| `active_context` | `ActiveWindowContext \| null` | Foreground window snapshot at request time; null if unknown |
| `requested_at` | datetime | ISO 8601 |

`ActiveWindowContext`: `executable`, `process_id`, `window_title`,
`window_bounds` (`x`, `y`, `width`, `height`), `captured_at`. No
screenshot/content field exists on this type by design (M2A criterion 5) —
do not add one without a coordinator-approved schema/ADR change.

## ActionResult

| Field | Type | Notes |
|---|---|---|
| `request_id` | UUID | Correlates to the originating `ActionRequest.id` |
| `status` | `ActionStatus` | `PENDING \| CONFIRMATION_REQUIRED \| DENIED \| BLOCKED \| RUNNING \| SUCCEEDED \| FAILED` |
| `risk_level` | `RiskLevel \| null` | The classification this result was decided under; non-null once terminal |
| `summary` | str | Human-readable, shown in HUD + audit log; must describe the real outcome |
| `data` | dict | Structured, skill-specific payload |
| `error` | str \| null | Non-null when status is `FAILED`/`DENIED`/`BLOCKED` |
| `started_at` / `completed_at` | datetime / datetime\|null | `completed_at` is null until a terminal status |

`BaseSkill._result(...)` is a helper for concrete skills to build a
well-formed `ActionResult` (fills `completed_at` correctly based on
whether `status` is terminal) — prefer it over constructing `ActionResult`
by hand in skill code.

## Skill interface

```python
class Skill(Protocol):
    name: str
    capabilities: tuple[str, ...]
    def classify_risk(self, action: str, arguments: dict[str, Any]) -> RiskLevel: ...
    async def validate(self, action: str, arguments: dict[str, Any]) -> None: ...
    async def execute(self, request: ActionRequest) -> ActionResult: ...
```

`BaseSkill` (ABC) implements the same surface for concrete skills to
subclass. `validate()` raises `SkillValidationError` for structurally
invalid arguments (not a permission decision — that's the runtime's job).
`execute()` raises `SkillExecutionError` for a real execution-time failure;
the action runtime converts that into a `FAILED` `ActionResult` rather than
letting a skill fabricate a `SUCCEEDED` one.

## Expected surface between streams (not yet implemented — this is the
## boundary each stream builds to; concrete routers are each stream's own)

- **Skill registry**: Claude Code's `apps/backend/skills/` package is
  expected to expose a `SKILLS: dict[str, Skill]` (or an equivalent
  `register()`/`get(name)` registry) that Codex's action runtime imports to
  dispatch a validated `ActionRequest.skill` to the right implementation.
  Exact registry shape is Claude Code's implementation choice; document it
  in `docs/coordination/handoffs/claude.md` once built so Codex can wire
  against it without guessing.
- **HTTP/WebSocket surface**: Codex's action runtime is expected to expose
  request/result endpoints analogous to V1's event surface (e.g.
  `POST /api/v1/actions` accepting an `ActionRequest`-shaped body,
  `GET /api/v1/actions/{id}` for its `ActionResult`, and a WebSocket/stream
  for HUD-side live status) under `apps/backend/app/routers/` — exact
  route names are the action runtime's call; document them in
  `docs/coordination/handoffs/codex.md`.
- **HUD**: Antigravity's native shell consumes whatever the action runtime
  exposes plus the existing V1 WebSocket/REST surface; it does not talk to
  skills directly, and it does not re-implement permission logic
  client-side (the runtime's decision is authoritative; the HUD only
  renders it and collects the user's confirm/deny).

## Voice integration boundary

The existing voice pipeline (`apps/backend/voice/`, `apps/backend/assistant/`)
is unchanged in Wave 2A except: the global PTT/hotkey path's recognized
intent, once resolved to a concrete action, is expected to construct an
`ActionRequest` (`source=voice_ptt` or `voice_wake_word`) and go through the
same action-runtime path as any other source — not a separate, unaudited
execution path. `AssistantRouter`'s existing deterministic-vs-model routing
(`apps/backend/assistant/router.py`) is the reference pattern for
criterion 12's "deterministic commands bypass the LLM."
