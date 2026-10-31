"""Find what the bundled backend runtime needs from the machine it lands on.

Running the runtime here proves little: this machine has Visual Studio, so a DLL
missing from the bundle is found anyway. This reads the import table of every
native module instead, and reports each DLL that is neither inside the bundle nor
part of Windows itself. A non-empty list means the installer would fail on a clean
machine in a way no test on a development machine can show.

    python tools/verify_bundle_dependencies.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pefile

REPO = Path(__file__).resolve().parents[1]
BUNDLE = REPO / "apps" / "web" / "src-tauri" / "bundle-runtime" / "tars"

# Present on every supported Windows install. The Universal CRT has shipped
# with Windows itself since 10, so api-ms-win-* and ucrtbase are safe to assume.
# The Visual C++ runtime (msvcp140, vcruntime140, concrt140...) is NOT, which is
# why it is deliberately absent here.
WINDOWS_DLLS = {
    "advapi32", "bcrypt", "bcryptprimitives", "cfgmgr32", "comctl32", "comdlg32", "crypt32",
    "d2d1", "d3d11", "d3d12", "d3dcompiler_47", "dbghelp", "dnsapi", "dsound", "dwmapi", "dwrite",
    "dxcore", "dxgi", "gdi32", "gdiplus", "hid", "imm32", "iphlpapi", "kernel32", "kernelbase",
    "mf", "mfplat", "mfreadwrite", "mpr", "msi", "mswsock", "ncrypt", "netapi32", "normaliz",
    "ntdll", "ntdsapi", "odbc32", "ole32", "oleacc", "oleaut32", "opengl32", "pdh", "powrprof",
    "propsys", "psapi", "rpcrt4", "secur32", "setupapi", "shcore", "shell32", "shlwapi", "sspicli",
    "ucrtbase", "urlmon", "user32", "userenv", "uxtheme", "version", "wbemuuid", "webio", "winhttp",
    "wininet", "winmm", "winspool", "wintrust", "wldap32", "ws2_32", "wsock32", "wtsapi32",
    "mscoree", "msvcrt", "dcomp", "dxva2", "evr", "mfuuid", "credui", "authz", "cabinet",
    "rasapi32", "rstrtmgr", "combase", "imagehlp", "ktmw32", "loadperf", "query",
    "sfc", "webservices", "mmdevapi", "dhcpcsvc", "fwpuclnt", "profapi", "wevtapi", "wer",
    "aclui", "activeds", "avicap32", "lz32", "msimg32",
}

# Imported, but only by a code path nothing here uses. Named rather than ignored,
# so a new entry has to be argued for: numba's optional TBB thread pool.
ACCEPTED_ABSENT = {
    "tbb12.dll": "numba's optional TBB pool; numba falls back to its own",
    "axscript.pyd": "pywin32's ActiveScript debugger, which nothing here loads",
}


def is_windows_dll(name: str) -> bool:
    lowered = name.lower().removesuffix(".dll").removesuffix(".drv")
    return lowered in WINDOWS_DLLS or lowered.startswith(("api-ms-win-", "ext-ms-"))


def imports_of(path: Path) -> set[str]:
    try:
        pe = pefile.PE(str(path), fast_load=True)
        pe.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]]
        )
    except pefile.PEFormatError:
        return set()
    names = {
        entry.dll.decode("ascii", "replace")
        for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", [])
    }
    pe.close()
    return names


def main() -> int:
    if not BUNDLE.is_dir():
        print(f"no bundle at {BUNDLE}; run scripts/build-backend-runtime.py first")
        return 2
    native = [p for p in BUNDLE.rglob("*") if p.suffix.lower() in {".pyd", ".dll", ".exe"}]
    python_dir = {p.name.lower() for p in (BUNDLE / "python").iterdir()}
    bundled = {p.name.lower() for p in native}
    missing: dict[str, list[str]] = {}
    for module in native:
        beside = {p.name.lower() for p in module.parent.iterdir()}
        for dll in imports_of(module):
            name = dll.lower()
            # Windows looks beside the module and in the application directory.
            # A plain name elsewhere in the bundle is not found by that search;
            # name-mangled copies from wheels are, because the wheel preloads them.
            mangled = "-" in name and name in bundled
            if name in beside or name in python_dir or mangled or is_windows_dll(dll):
                continue
            if name not in ACCEPTED_ABSENT:
                missing.setdefault(dll, []).append(str(module.relative_to(BUNDLE)))

    print(f"{len(native)} native modules checked")
    if not missing:
        print("every import resolves inside the bundle or to Windows itself")
        return 0
    for dll, users in sorted(missing.items()):
        print(f"MISSING {dll}: needed by {len(users)}, e.g. {users[0]}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
