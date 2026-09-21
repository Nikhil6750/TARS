# Semantic memory

`MemoryService` remains the backend entry point. `semantic` operates on the
existing SQLite connection and `memory_notes` table; migration
`0010_semantic_memory.sql` adds revision links and an indexed canonical key.
There is no additional database, model, embedding service, or paid dependency.

## Interfaces

- `remember_fact(statement, conversation_id=..., channel="text")` interprets an
  explicit user statement. Returns `SAVED`, `UPDATED`, `UNCHANGED`,
  `NEEDS_CLARIFICATION`, `CONFLICT`, `EPHEMERAL`, `UNRECOGNIZED`, or `REJECTED`.
- `recall_memory(query, limit=5)` returns canonical facts, never source prose.
- `memory_response(text, conversation_id=...)` handles simple stores/recalls
  deterministically; `None` leaves unrelated conversation on its existing route.
- `semantic.resolve_symbol(alias)` resolves user aliases before market lookup.
- `forget(note_id)` removes the semantic revision lineage and invalidates the
  existing retrieval cache. Deleted corrections do not resurrect older facts.
- Explicit `scope="ephemeral"` with a `conversation_id` uses only the existing
  bounded session store. It is not visible to persistent or other-session reads.

A fact contains `subject`, `relation`, `object`, `memory_type`, `source`,
`created_at`, `updated_at`, `confidence`, `scope`, `aliases`,
`canonical_entities`, `id`, `supersedes`, and `superseded_by`. Types are `FACT`,
`PREFERENCE`, `ALIAS`, and `PROJECT_CONTEXT`. Confidence 1.0 means that the user
explicitly asserted the fact, **not** independent verification or trading
confidence. Memory is never validated trading evidence.

The original user statement is kept in the note body as local provenance.
Structured tool results include source identifiers and attribution but omit
the source sentence. FTS indexes the canonical triple. Existing text notes,
research notes, and trading observations retain their existing APIs.

## Normalization and corrections

The deterministic English grammar uses a closed relation vocabulary:
`developed_by`, `called`, `alias_of`, `default_timeframe`, `default_symbol`,
`prefers`, `uses`, `works_on`. Built/created/developed/made map to `developed_by`;
assistant pronouns map to `TARS`; first-person user pronouns map to `USER`.
`I built you` needs an explicitly known user name, including one stated in the
same utterance. Preferred chart timeframes map to `default_timeframe`; minutes
and hours normalize (`15 minutes` → `15m`, `60m` → `1h`).

An alias is directional: `gold alias_of XAUUSD`; `Call gold XAU` stores
`xau alias_of XAUUSD`. Alias metadata includes the alias and canonical entities.
Each entity/relation slot currently has one active value. Multiple developers,
generic preferences, tools, or projects in the same slot require clarification;
this implementation does not invent a plural interpretation.

An ordinary contradictory assertion leaves the current fact intact and asks
for clarification. `Actually, Arun developed you` explicitly supersedes the
old value. Internal callers may use `expected_id` for compare-and-swap updates.
A SQLite trigger validates the expected revision, retires the old fact, and
updates the canonical FTS entry within the same INSERT. Multi-fact utterances
are inserted as one atomic statement. All retrieval filters retired revisions.

At startup, existing user-authored explicit notes are examined in pages of 100.
Only fully recognized, unambiguous notes are promoted, preserving their original
note ID and creation time as provenance. Conflicting legacy notes remain local
evidence, marked `semantic_import_status=conflict` and excluded from model
grounding. Transcripts, agent statements, and trading observations are never
promoted. Unsupported old notes remain notes.

## Gemini Live and routing

Two tools are added; existing audio transport, voice selection, desktop guards,
and action permissions are unchanged:

- `remember_fact()` reads the actual latest human transcript, including an
  accumulated current transcription when the tool precedes audio finalization.
  It accepts no model-authored facts, confidence, overwrite flag, or provenance.
- `recall_memory(query)` retrieves at most five relevant structured records.
  It also serves alias/default lookup and bounded semantic search.

The system instruction tells Gemini to use these tools and answer from data
in natural language. Corrections use `remember_fact()` again. Simple memory
requests intercepted by `ask_claude` stay local. Normal text and local voice
turns also perform the deterministic memory gate before provider routing.
Default-chart requests retrieve settings; the deterministic response does not
claim it opened a chart. Gemini can then use the existing guarded desktop tools.

Unknown or ambiguous English asks for a clearer fact rather than storing the
raw sentence as semantic truth. This is a conservative grammar, not a claim of
universal language understanding. No live paid-model or microphone test is
required for the deterministic suite; real Gemini wording remains model-driven.

## Validation

`python -m pytest tests/test_semantic_memory.py tests/test_semantic_memory_integration.py -q`

The suite covers paraphrases, pronouns, preferences, aliases, atomic conflicts,
history, migration, restart, ephemeral scope, provenance, secrets, irrelevant
text, bounded results, latency, public assistant routing, and SDK/tool dispatch.
