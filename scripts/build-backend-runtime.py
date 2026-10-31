"""Build the self-contained backend runtime an installed TARS ships with.

An installed app has no checkout and may have no Python, so it carries its own:
the official embeddable CPython, the backend's dependencies, and the backend
source. The output mirrors the repository layout (`apps/backend`, `contracts`)
because the backend locates its files relative to that layout, so no code path
differs between a checkout and an install except where writable state goes.

Only runtime dependencies are bundled. Test tools and caches are not, and
neither is anything from `.env` or the user's data: this tree ends up in a
distributable installer.

    python scripts/build-backend-runtime.py          # uses a cached build
    python scripts/build-backend-runtime.py --clean  # from nothing

The output is apps/web/src-tauri/bundle-runtime/tars (gitignored). Build the
installer afterwards with scripts/build-installer.cmd.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUTPUT = REPO / "apps" / "web" / "src-tauri" / "bundle-runtime" / "tars"
CACHE = REPO / "apps" / "web" / "src-tauri" / "bundle-runtime" / ".cache"

PYTHON_VERSION = "3.13.3"
EMBED_URL = (
    f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"
)

BACKEND = REPO / "apps" / "backend"
REQUIREMENTS = ("requirements.txt", "requirements-research.txt", "requirements-voice.txt")
# Imported by the backend but not pinned in a requirements file.
EXTRA_PACKAGES = ("MetaTrader5", "psutil")
# Development tools listed in requirements.txt; never needed at runtime.
DEV_ONLY = ("pytest", "pytest-asyncio", "ruff", "mypy")

# Backend paths that are not part of the running application.
EXCLUDED_DIRS = {
    "tests", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv", "venv",
    "benchmarks", "scripts", "tmp",
}
# The stray nested directory an old relative path created; user data, not code.
EXCLUDED_TOP = {"apps"}
# Note `storage/` itself is a code package: only the data inside it is excluded.
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".db", ".db-wal", ".db-shm", ".log", ".wav"}

# pywin32's GUI toolkit: never imported, and it needs MFC, which nothing ships.
EXCLUDED_PACKAGES = ("pythonwin",)

# Wheels import these by their plain names, and a clean Windows does not have
# them. Copied beside python.exe, from Microsoft's redistributable folder, which
# is where Windows looks for a process's DLLs first.
VC_RUNTIME = (
    "msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "msvcp140_atomic_wait.dll",
    "msvcp140_codecvt_ids.dll", "concrt140.dll", "vcruntime140.dll", "vcruntime140_1.dll",
    "vcomp140.dll",
)


def step(message: str) -> None:
    print(f">> {message}", flush=True)


def download_python() -> Path:
    target = CACHE / "download" / f"python-{PYTHON_VERSION}-embed-amd64.zip"
    if target.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    step(f"Downloading {EMBED_URL}")
    partial = target.with_suffix(".part")
    with urllib.request.urlopen(EMBED_URL, timeout=120) as response, partial.open("wb") as out:
        shutil.copyfileobj(response, out)
    partial.replace(target)
    return target


def unpack_python(archive: Path, destination: Path) -> None:
    step(f"Unpacking CPython {PYTHON_VERSION}")
    destination.mkdir(parents=True)
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(destination)
    # The embeddable build's path file decides everything it can import. With
    # this, it sees only its own files, whatever is installed on the machine.
    # `import site` is what processes pywin32's .pth file from those.
    pth = next(destination.glob("python*._pth"))
    pth.write_text(
        f"python{PYTHON_VERSION.replace('.', '')[:3]}.zip\n.\nLib\\site-packages\nimport site\n",
        encoding="utf-8",
    )


def merged_requirements() -> Path:
    lines: list[str] = []
    for name in REQUIREMENTS:
        for raw in (BACKEND / name).read_text(encoding="utf-8").splitlines():
            line = raw.split("#")[0].strip()
            if not line or line.startswith("-"):
                continue
            package = line.split("[")[0].split("=")[0].split(">")[0].split("<")[0].strip()
            if package.lower() not in DEV_ONLY:
                lines.append(line)
    lines.extend(EXTRA_PACKAGES)
    path = CACHE / "requirements.lock.in"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def rename_when_released(source: Path, target: Path) -> None:
    """Antivirus holds thousands of just-written files open for a while, and
    Windows will not rename a directory while it does."""
    for _ in range(40):
        try:
            source.rename(target)
            return
        except PermissionError:
            time.sleep(15)
    source.rename(target)


def install_dependencies(site_packages: Path) -> None:
    if sys.version_info[:2] != tuple(int(p) for p in PYTHON_VERSION.split(".")[:2]):
        raise SystemExit(f"run this with Python {PYTHON_VERSION[:4]}: wheels must match the runtime")
    step("Installing dependencies")
    requirements = merged_requirements()
    digest = hashlib.sha256(requirements.read_bytes()).hexdigest()[:16]
    cached = CACHE / f"site-{digest}"
    if not cached.exists():
        partial = cached.with_name(cached.name + ".part")
        shutil.rmtree(partial, ignore_errors=True)
        subprocess.run(
            [
                sys.executable, "-m", "pip", "install", "--no-compile", "--disable-pip-version-check",
                "--target", str(partial), "-r", str(requirements),
            ],
            check=True,
        )
        rename_when_released(partial, cached)
    site_packages.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        cached, site_packages,
        ignore=shutil.ignore_patterns("__pycache__", "tests", *EXCLUDED_PACKAGES),
    )


def copy_vc_runtime(python_dir: Path) -> None:
    step("Copying the Visual C++ runtime")
    roots = [
        Path(r"C:\Program Files (x86)\Microsoft Visual Studio"),
        Path(r"C:\Program Files\Microsoft Visual Studio"),
    ]
    found = sorted(
        (d for root in roots if root.exists()
         for d in root.glob("*/*/VC/Redist/MSVC/*/x64/Microsoft.VC*.CRT")),
        key=lambda p: p.parts,
    )
    openmp = sorted(
        d for root in roots if root.exists()
        for d in root.glob("*/*/VC/Redist/MSVC/*/x64/Microsoft.VC*.OpenMP")
    )
    if not found:
        raise SystemExit("no Visual C++ redistributable found; install the MSVC build tools")
    sources = [found[-1], *openmp[-1:]]
    for name in VC_RUNTIME:
        source = next((d / name for d in sources if (d / name).exists()), None)
        if source is None:
            raise SystemExit(f"{name} is missing from {sources}")
        shutil.copy2(source, python_dir / name)


def copy_backend(destination: Path) -> None:
    step("Copying the backend source")

    def ignore(directory: str, names: list[str]) -> set[str]:
        at_root = Path(directory) == BACKEND
        skipped = {n for n in names if n in EXCLUDED_DIRS or Path(n).suffix in EXCLUDED_SUFFIXES}
        if at_root:
            skipped |= {n for n in names if n in EXCLUDED_TOP or n.startswith(".env")}
            skipped |= {n for n in names if n.startswith("requirements")}
        return skipped

    shutil.copytree(BACKEND, destination, ignore=ignore)


def write_manifest(output: Path) -> None:
    files = [p for p in output.rglob("*") if p.is_file()]
    manifest = {
        "python": PYTHON_VERSION,
        "files": len(files),
        "bytes": sum(p.stat().st_size for p in files),
    }
    (output / "runtime-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"   {manifest['files']} files, {manifest['bytes'] / 1e6:.0f} MB")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--clean", action="store_true", help="discard the dependency cache too")
    args = parser.parse_args()

    if args.clean:
        shutil.rmtree(CACHE, ignore_errors=True)
    shutil.rmtree(OUTPUT, ignore_errors=True)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    python_dir = OUTPUT / "python"
    unpack_python(download_python(), python_dir)
    install_dependencies(python_dir / "Lib" / "site-packages")
    copy_vc_runtime(python_dir)
    # Normally found through a bootstrap hook; beside python.exe they just work.
    for dll in (python_dir / "Lib" / "site-packages" / "pywin32_system32").glob("*.dll"):
        shutil.copy2(dll, python_dir / dll.name)
    copy_backend(OUTPUT / "apps" / "backend")
    shutil.copytree(REPO / "contracts", OUTPUT / "contracts")
    write_manifest(OUTPUT)
    step(f"Runtime ready at {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
