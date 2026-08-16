# TARS verification tools

The JSON Schemas in `contracts/` are the only manually maintained contract
definitions. Generated Python/Pydantic and TypeScript types live under
`tools/generated/` and must never be edited by hand.

## Contract generation

The generators are free/open source, actively published, and directly consume
JSON Schema Draft 2020-12:

- `datamodel-code-generator==0.73.0` generates Pydantic v2 models. Its complete
  Python dependency set is exact-pinned in `tools/codegen/requirements.lock`.
- `json-schema-to-typescript==15.0.4` generates TypeScript declarations. npm's
  lockfile pins the complete dependency graph and package integrity hashes.

Bootstrap and generate from the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r tools\codegen\requirements.lock
npm ci --prefix tools\codegen
.\.venv\Scripts\python tools\generate_contracts.py
```

CI/drift verification uses the same environment and runs:

```powershell
.\.venv\Scripts\python tools\generate_contracts.py --check
```

The generator canonicalizes every Python and TypeScript artifact to LF, and
the repository pins LF checkout behavior for only `tools/generated/`. The
check regenerates into a temporary directory and compares the complete file
set and content, tolerating only equivalent CRLF/LF encodings in an existing
checkout. A schema change, generator change, whitespace change, or other hand
edit therefore still fails deterministically.

## External test client

`tools/tars_test_client.py` imports no backend or frontend implementation. It
validates all public trading-event and assistant-message data against the
canonical schemas and supports `health`, `send-event`, `active`, `history`,
`listen`, `invalidate`, `ask`, and `verify-grounded` commands.

```powershell
.\.venv\Scripts\python tools\tars_test_client.py health
.\.venv\Scripts\python tools\tars_test_client.py send-event tests\fixtures\valid\setup_valid.json
.\.venv\Scripts\python tools\tars_test_client.py listen --count 2
.\.venv\Scripts\python tools\tars_test_client.py verify-grounded "What is EURUSD entry?" --symbol EURUSD
```

`TARS_BASE_URL` and `TARS_TEST_TIMEOUT` configure the target and bounded wait.
Because shared contracts do not freeze URL paths, each route can be overridden
without changing the client: `TARS_HEALTH_PATH`, `TARS_EVENTS_PATH`,
`TARS_ACTIVE_PATH`, `TARS_HISTORY_PATH`, `TARS_INVALIDATE_PATH`,
`TARS_ASSISTANT_PATH`, `TARS_MEMORY_SEARCH_PATH`, and `TARS_WEBSOCKET_PATH`.
The invalidation path may use an `{event_id}` placeholder.

## Full acceptance harness

Install the pinned test dependencies and Chromium once, then supply process
commands for the integrated backend and frontend:

```powershell
.\.venv\Scripts\python -m pip install -r tests\requirements.lock
.\.venv\Scripts\playwright install chromium
.\.venv\Scripts\python tools\run_acceptance.py `
  --backend-command "python -m uvicorn app.main:app --port 8000" `
  --backend-cwd apps\backend `
  --frontend-command "npm run dev -- --host 127.0.0.1" `
  --frontend-cwd apps\web
```

The runner removes all paid-provider keys, uses an isolated SQLite database,
creates a deterministic Obsidian provenance fixture, starts both process trees,
polls readiness with deadlines, executes `tests/acceptance/`, scans captured
logs for a secret sentinel, and always tears down descendants.
`--use-running-services` is available for partial diagnostics, but
intentionally fails the explicit "processes started by this harness"
acceptance check.

## V1 certification

The certification entry point runs every mandatory gate and returns nonzero if
any gate fails. It covers contract/codegen checks, backend pytest/Ruff/MyPy,
frontend tests/typecheck/lint/production build, Tauri compatibility (plus a
native build when Rust tooling exists), and process-owning external acceptance:

```powershell
python tools\run_certification.py
```

`tools/tauri_checks.py --cargo-check` can be run separately for deterministic
npm/Cargo version, configuration, and Rust build-metadata diagnostics.
