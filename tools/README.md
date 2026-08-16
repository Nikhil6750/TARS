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
