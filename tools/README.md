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

The check regenerates into a temporary directory and compares the complete
file set and file bytes against committed output. A schema change, generator
change, or hand edit therefore fails deterministically.

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
`TARS_ASSISTANT_PATH`, and `TARS_WEBSOCKET_PATH`. The invalidation path may use
an `{event_id}` placeholder.
