"""Generate Python and TypeScript models from the frozen JSON Schemas."""

from __future__ import annotations

import argparse
import filecmp
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "contracts"
COMMITTED_DIR = ROOT / "tools" / "generated"
SCHEMAS = (
    ("assistant-message.schema.json", "assistant_message.py", "assistant-message.d.ts"),
    ("trading-event.schema.json", "trading_event.py", "trading-event.d.ts"),
)


def _run(command: list[str]) -> None:
    try:
        subprocess.run(command, cwd=ROOT, check=True)
    except FileNotFoundError as exc:
        raise SystemExit(
            f"Required generator is not installed: {command[0]}. "
            "See tools/README.md for bootstrap instructions."
        ) from exc


def _json2ts_binary() -> Path:
    base = ROOT / "tools" / "codegen" / "node_modules" / ".bin" / "json2ts"
    candidate = base.with_suffix(".cmd") if sys.platform == "win32" else base
    if not candidate.exists():
        raise SystemExit(
            "json-schema-to-typescript is not installed. Run "
            "`npm ci --prefix tools/codegen` first."
        )
    return candidate


def generate(destination: Path) -> None:
    python_dir = destination / "python"
    typescript_dir = destination / "typescript"
    python_dir.mkdir(parents=True, exist_ok=True)
    typescript_dir.mkdir(parents=True, exist_ok=True)
    (python_dir / "__init__.py").write_text(
        '"""Generated from contracts/*.schema.json; do not edit manually."""\n',
        encoding="utf-8",
        newline="\n",
    )

    for schema_name, python_name, typescript_name in SCHEMAS:
        schema = SCHEMA_DIR / schema_name
        _run(
            [
                sys.executable,
                "-m",
                "datamodel_code_generator",
                "--input",
                str(schema),
                "--input-file-type",
                "jsonschema",
                "--output",
                str(python_dir / python_name),
                "--output-model-type",
                "pydantic_v2.BaseModel",
                "--target-python-version",
                "3.12",
                "--use-standard-collections",
                "--use-union-operator",
                "--use-default",
                "--disable-timestamp",
                "--formatters",
                "black",
                "isort",
            ]
        )
        _run(
            [
                str(_json2ts_binary()),
                "--input",
                str(schema),
                "--output",
                str(typescript_dir / typescript_name),
                "--cwd",
                str(SCHEMA_DIR),
                "--no-enableConstEnums",
                "--unknownAny",
            ]
        )


def _relative_files(directory: Path) -> list[Path]:
    return sorted(
        path.relative_to(directory)
        for path in directory.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix not in {".pyc", ".pyo"}
    )


def check_drift(generated: Path) -> None:
    expected = _relative_files(COMMITTED_DIR) if COMMITTED_DIR.exists() else []
    actual = _relative_files(generated)
    problems: list[str] = []

    if expected != actual:
        problems.append(f"file set differs: committed={expected!r}, generated={actual!r}")
    for relative_path in sorted(set(expected) & set(actual)):
        if not filecmp.cmp(
            COMMITTED_DIR / relative_path,
            generated / relative_path,
            shallow=False,
        ):
            problems.append(f"content differs: {relative_path.as_posix()}")

    if problems:
        joined = "\n  - ".join(problems)
        raise SystemExit(
            "Generated contract artifacts are stale. Run "
            "`python tools/generate_contracts.py` and commit the result:\n  - "
            + joined
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="regenerate in a temporary directory and fail on committed drift",
    )
    args = parser.parse_args()

    if args.check:
        with tempfile.TemporaryDirectory(prefix="tars-contracts-") as temp_dir:
            destination = Path(temp_dir)
            generate(destination)
            check_drift(destination)
        print("Contract artifacts match canonical schemas.")
        return

    staging = COMMITTED_DIR.with_name(".generated-staging")
    if staging.exists():
        shutil.rmtree(staging)
    try:
        generate(staging)
        if COMMITTED_DIR.exists():
            shutil.rmtree(COMMITTED_DIR)
        staging.replace(COMMITTED_DIR)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    print(f"Generated contract artifacts in {COMMITTED_DIR.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
