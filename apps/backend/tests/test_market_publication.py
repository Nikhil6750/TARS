"""Publishing an imported candle-store generation.

An import that fails for one symbol must not cost that symbol the data it
already had, and two imports publishing at once must not lose either result.
The races run real imports in real processes: a lock that only worked inside
one process would pass any test run on threads.
"""
from __future__ import annotations

import errno
import json
import math
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.routers import market_xray
from market import ingest, universe
from market.store import CandleStore, active_dataset_revision

BACKEND = Path(ingest.__file__).resolve().parents[1]

# Two rendezvous. The first is after every racer has built its data, so all of
# them are about to publish. The second holds each racer at the rename until
# the others arrive, which is what makes unserialized publishers overwrite each
# other every time. It has to give up, because a correct lock keeps the others
# from ever arriving there.
RACING_IMPORT = """
import os, sys, time
from pathlib import Path
from market import ingest

symbol, store, csvs, meeting, racers = sys.argv[1:6]
meeting, racers = Path(meeting), int(racers)

def meet(stage, patience):
    (meeting / f"{stage}-{symbol}").touch()
    deadline = time.monotonic() + patience
    while len(list(meeting.glob(stage + "-*"))) < racers and time.monotonic() < deadline:
        time.sleep(0.01)

real_build, real_replace = ingest.ingest_symbol, os.replace

def build(*args, **kwargs):
    rows = real_build(*args, **kwargs)
    meet("built", 60)
    return rows

def replace(source, destination):
    if Path(destination).name == "manifest.json":
        meet("publishing", 1.5)
    return real_replace(source, destination)

ingest.ingest_symbol = build
os.replace = replace
ingest.ingest_all([symbol], store_root=Path(store), csv_root=Path(csvs))
"""

# Dies inside publication, the way a killed process does: no cleanup runs.
CRASHING_IMPORT = """
import os, sys
from pathlib import Path
from market import ingest

def die(source, destination):
    os._exit(9)

os.replace = die
ingest.ingest_all(["GBPUSD"], store_root=Path(sys.argv[1]), csv_root=Path(sys.argv[2]))
"""


def write_sources(csv_root: Path, symbols: list[str], *, price: float = 1.1) -> None:
    folder = csv_root / "Forex_UTC"
    folder.mkdir(parents=True, exist_ok=True)
    for symbol in symbols:
        for side, spread in (("Bid", 0), ("Ask", 0.0002)):
            rows = ["Time (UTC),Open,High,Low,Close,Volume"]
            for minute in range(180):
                quote = price + spread + minute * 0.00001
                at = datetime(2026, 1, 5, tzinfo=UTC) + timedelta(minutes=minute)
                rows.append(
                    f"{at:%Y.%m.%d %H:%M:%S},{quote},{quote + 0.001},{quote - 0.001},{quote},1"
                )
            name = f"{symbol}_1 Min_{side}_2026.01.01_2026.01.12.csv"
            (folder / name).write_text("\n".join(rows) + "\n", encoding="utf-8")


def remove_sources(csv_root: Path, symbol: str) -> None:
    for path in (csv_root / "Forex_UTC").glob(symbol + "*.csv"):
        path.unlink()


def fake_ingest(_con, symbol, *, store_root, start, csv_root):
    """One small file standing in for a symbol's data, marked with its origin."""
    folder = store_root / symbol
    folder.mkdir(parents=True)
    path = folder / "M5.parquet"
    path.write_bytes(symbol.encode())
    return [ingest.IngestResult(symbol, "M5", 1, None, None, None, str(path))]


def active(root: Path) -> dict:
    return json.loads((root / "manifest.json").read_text(encoding="utf-8"))


def generations(root: Path) -> set[str]:
    return {path.name for path in (root / "generations").iterdir()}


@pytest.fixture
def imported(tmp_path):
    """A store holding real EURUSD and GBPUSD data, and the CSVs behind it."""
    csvs, root = tmp_path / "csv", tmp_path / "store"
    write_sources(csvs, ["EURUSD", "GBPUSD", "AUDUSD"])
    ingest.ingest_all(["EURUSD", "GBPUSD"], store_root=root, csv_root=csvs)
    return root, csvs


# ---- a replacement that fails --------------------------------------------


def test_a_failed_replacement_keeps_the_data_it_was_replacing(imported):
    root, csvs = imported
    before = {
        tf: (root / active(root)["generation"] / "GBPUSD" / f"{tf}.parquet").read_bytes()
        for tf in universe.TIMEFRAMES
    }
    remove_sources(csvs, "GBPUSD")

    result = ingest.ingest_all(["EURUSD", "GBPUSD"], store_root=root, csv_root=csvs)

    store = CandleStore(root)
    try:
        assert "GBPUSD" in store.recorded_symbols()
        assert store.coverage("GBPUSD", "M5").bars > 0
        for tf, content in before.items():
            assert store.path_for("GBPUSD", tf).read_bytes() == content
    finally:
        store.close()
    listed = {(row["symbol"], row["timeframe"]): row["path"] for row in result["datasets"]}
    for tf in universe.TIMEFRAMES:
        assert Path(listed["GBPUSD", tf]).is_relative_to(root / result["generation"])


def test_the_report_says_a_failed_symbol_failed_and_kept_its_data(imported):
    root, csvs = imported
    remove_sources(csvs, "GBPUSD")
    result = ingest.ingest_all(["EURUSD", "GBPUSD"], store_root=root, csv_root=csvs)
    assert result["symbols_ok"] == ["EURUSD"]
    assert "FileNotFoundError" in result["symbols_failed"]["GBPUSD"]
    assert "previous data kept" in result["symbols_failed"]["GBPUSD"]


def test_a_replacement_failing_halfway_does_not_publish_its_partial_files(tmp_path, monkeypatch):
    root = tmp_path / "store"
    monkeypatch.setattr(ingest, "ingest_symbol", fake_ingest)
    ingest.ingest_all(["EURUSD", "GBPUSD"], store_root=root)

    def fails_after_writing(con, symbol, *, store_root, start, csv_root):
        rows = fake_ingest(con, symbol, store_root=store_root, start=start, csv_root=csv_root)
        if symbol == "GBPUSD":
            (store_root / symbol / "M5.parquet").write_bytes(b"half written")
            raise OSError("disk full")
        return rows

    monkeypatch.setattr(ingest, "ingest_symbol", fails_after_writing)
    result = ingest.ingest_all(["EURUSD", "GBPUSD"], store_root=root)
    assert (root / result["generation"] / "GBPUSD" / "M5.parquet").read_bytes() == b"GBPUSD"


def test_a_failed_symbol_with_no_earlier_data_leaves_nothing_behind(tmp_path, monkeypatch):
    root = tmp_path / "store"
    monkeypatch.setattr(ingest, "ingest_symbol", fake_ingest)
    ingest.ingest_all(["EURUSD"], store_root=root)

    def fails_after_writing(con, symbol, *, store_root, start, csv_root):
        rows = fake_ingest(con, symbol, store_root=store_root, start=start, csv_root=csv_root)
        if symbol == "GBPUSD":
            raise OSError("disk full")
        return rows

    monkeypatch.setattr(ingest, "ingest_symbol", fails_after_writing)
    result = ingest.ingest_all(["GBPUSD", "EURUSD"], store_root=root)
    assert not (root / result["generation"] / "GBPUSD").exists()
    assert result["symbols_failed"]["GBPUSD"] == "OSError: disk full"


def test_a_full_import_keeps_a_symbol_whose_replacement_failed(tmp_path, monkeypatch):
    root = tmp_path / "store"
    monkeypatch.setattr(ingest, "ingest_symbol", fake_ingest)
    ingest.ingest_all(store_root=root)

    def gbpusd_fails(con, symbol, *, store_root, start, csv_root):
        if symbol == "GBPUSD":
            raise FileNotFoundError("no source")
        return fake_ingest(con, symbol, store_root=store_root, start=start, csv_root=csv_root)

    monkeypatch.setattr(ingest, "ingest_symbol", gbpusd_fails)
    result = ingest.ingest_all(store_root=root)
    assert (root / result["generation"] / "GBPUSD" / "M5.parquet").read_bytes() == b"GBPUSD"


def test_a_failed_replacement_keeps_data_from_a_store_with_no_generations(tmp_path, monkeypatch):
    """The real store on this machine still has this older flat layout."""
    root = tmp_path / "store"
    (root / "GBPUSD").mkdir(parents=True)
    (root / "GBPUSD" / "M5.parquet").write_bytes(b"flat")
    (root / "manifest.json").write_text(json.dumps({
        "datasets": [{"symbol": "GBPUSD", "timeframe": "M5", "path": "old"}],
    }), encoding="utf-8")

    def gbpusd_fails(con, symbol, *, store_root, start, csv_root):
        if symbol == "GBPUSD":
            raise FileNotFoundError("no source")
        return fake_ingest(con, symbol, store_root=store_root, start=start, csv_root=csv_root)

    monkeypatch.setattr(ingest, "ingest_symbol", gbpusd_fails)
    result = ingest.ingest_all(["EURUSD", "GBPUSD"], store_root=root)
    assert (root / result["generation"] / "GBPUSD" / "M5.parquet").read_bytes() == b"flat"
    assert (root / "GBPUSD" / "M5.parquet").read_bytes() == b"flat"


def test_an_import_that_rebuilt_nothing_publishes_nothing(tmp_path, monkeypatch):
    """Publishing would only invalidate every cached frame for the same data."""
    root = tmp_path / "store"
    monkeypatch.setattr(ingest, "ingest_symbol", fake_ingest)
    ingest.ingest_all(["EURUSD", "GBPUSD"], store_root=root)
    revision, existing = active_dataset_revision(root), generations(root)

    def fails(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(ingest, "ingest_symbol", fails)
    with pytest.raises(ValueError, match="no usable candle"):
        ingest.ingest_all(["GBPUSD"], store_root=root)
    assert active_dataset_revision(root) == revision
    assert generations(root) == existing


def test_a_clean_full_import_can_replace_an_unreadable_manifest(tmp_path, monkeypatch):
    """It needs nothing from the old one, so this remains the way to recover."""
    root = tmp_path / "store"
    root.mkdir()
    (root / "manifest.json").write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(ingest, "ingest_symbol", fake_ingest)
    result = ingest.ingest_all(store_root=root)
    assert active(root)["dataset_revision"] == result["dataset_revision"]


# ---- publishers at the same time -----------------------------------------


def test_racing_processes_all_keep_what_they_published(tmp_path):
    csvs, root, meeting = tmp_path / "csv", tmp_path / "store", tmp_path / "meeting"
    racers = ["GBPUSD", "AUDUSD", "USDJPY"]
    write_sources(csvs, ["EURUSD", *racers])
    meeting.mkdir()
    ingest.ingest_all(["EURUSD"], store_root=root, csv_root=csvs)

    running = [
        subprocess.Popen(
            [sys.executable, "-c", RACING_IMPORT, symbol, str(root), str(csvs),
             str(meeting), str(len(racers))],
            cwd=BACKEND, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        for symbol in racers
    ]
    outcomes = [(process.communicate(timeout=120)[0], process.returncode) for process in running]
    assert [code for _output, code in outcomes] == [0] * len(racers), outcomes
    published = active(root)
    assert set(published["symbols_ok"]) == {"EURUSD", *racers}
    for symbol in ("EURUSD", *racers):
        for tf in universe.TIMEFRAMES:
            assert (root / published["generation"] / symbol / f"{tf}.parquet").is_file()


def test_a_publisher_that_dies_does_not_block_the_next_one(tmp_path):
    csvs, root = tmp_path / "csv", tmp_path / "store"
    write_sources(csvs, ["EURUSD", "GBPUSD", "AUDUSD"])
    first = ingest.ingest_all(["EURUSD"], store_root=root, csv_root=csvs)

    crashed = subprocess.run(
        [sys.executable, "-c", CRASHING_IMPORT, str(root), str(csvs)],
        cwd=BACKEND, capture_output=True, text=True, timeout=120,
    )
    assert crashed.returncode == 9, crashed.stdout + crashed.stderr
    assert active_dataset_revision(root) == first["dataset_revision"]

    after = ingest.ingest_all(["AUDUSD"], store_root=root, csv_root=csvs)
    assert set(after["symbols_ok"]) == {"EURUSD", "AUDUSD"}
    assert active_dataset_revision(root) == after["dataset_revision"]


def test_a_publication_that_fails_leaves_the_active_data_alone(tmp_path, monkeypatch):
    root = tmp_path / "store"
    monkeypatch.setattr(ingest, "ingest_symbol", fake_ingest)
    ingest.ingest_all(["EURUSD"], store_root=root)
    revision, existing = active_dataset_revision(root), generations(root)

    def refused(_source, _destination):
        raise PermissionError("in use")

    monkeypatch.setattr(ingest.os, "replace", refused)
    with pytest.raises(PermissionError):
        ingest.ingest_all(["GBPUSD"], store_root=root)
    assert active_dataset_revision(root) == revision
    assert generations(root) == existing


def test_waiting_too_long_to_publish_fails_without_touching_the_active_data(tmp_path, monkeypatch):
    root = tmp_path / "store"
    monkeypatch.setattr(ingest, "ingest_symbol", fake_ingest)
    ingest.ingest_all(["EURUSD"], store_root=root)
    revision, existing = active_dataset_revision(root), generations(root)
    monkeypatch.setattr(ingest, "PUBLICATION_LOCK_SECONDS", 0.05)

    with ingest._publication_lock(root):
        with pytest.raises(TimeoutError, match="another import"):
            ingest.ingest_all(["GBPUSD"], store_root=root)
    assert active_dataset_revision(root) == revision
    assert generations(root) == existing


# ---- windows sharing violations ------------------------------------------

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows file sharing")
ACCESS_DENIED, SHARING_VIOLATION, LOCK_VIOLATION = 5, 32, 33


def refusal(code: int) -> PermissionError:
    """How `os.replace` reports a file in use."""
    return PermissionError(13, "in use", "manifest.json", code)


def opening_refused() -> PermissionError:
    """How `open` reports the same thing: the C runtime's EACCES, no Windows code."""
    return PermissionError(errno.EACCES, "Permission denied", "manifest.json")


def replace_refusing(monkeypatch, times: float, error: OSError) -> list:
    attempts = []
    real = os.replace

    def replace(source, destination):
        if Path(destination).name == "manifest.json":
            attempts.append(1)
            if len(attempts) <= times:
                raise error
        return real(source, destination)

    monkeypatch.setattr(ingest.os, "replace", replace)
    return attempts


def manifest_reads_refused(monkeypatch, times: float, error: OSError) -> list:
    attempts = []
    real = Path.read_text

    def read_text(self, *args, **kwargs):
        if self.name == "manifest.json":
            attempts.append(1)
            if len(attempts) <= times:
                raise error
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    return attempts


@windows_only
@pytest.mark.parametrize("code", [ACCESS_DENIED, SHARING_VIOLATION, LOCK_VIOLATION])
def test_publishing_waits_out_a_reader_holding_the_manifest(tmp_path, monkeypatch, code):
    root = tmp_path / "store"
    monkeypatch.setattr(ingest, "ingest_symbol", fake_ingest)
    ingest.ingest_all(["EURUSD"], store_root=root)
    attempts = replace_refusing(monkeypatch, 3, refusal(code))
    result = ingest.ingest_all(["GBPUSD"], store_root=root)
    assert len(attempts) == 4
    assert active_dataset_revision(root) == result["dataset_revision"]
    assert set(active(root)["symbols_ok"]) == {"EURUSD", "GBPUSD"}


@windows_only
def test_a_publication_that_stays_refused_gives_up_and_keeps_the_active_data(tmp_path, monkeypatch):
    root = tmp_path / "store"
    monkeypatch.setattr(ingest, "ingest_symbol", fake_ingest)
    ingest.ingest_all(["EURUSD"], store_root=root)
    revision, existing = active_dataset_revision(root), generations(root)
    monkeypatch.setattr(ingest, "MANIFEST_PUBLISH_RETRY_SECONDS", 0.05)
    attempts = replace_refusing(monkeypatch, math.inf, refusal(ACCESS_DENIED))
    began = time.monotonic()
    with pytest.raises(PermissionError):
        ingest.ingest_all(["GBPUSD"], store_root=root)
    assert time.monotonic() - began < 1
    assert len(attempts) > 1
    assert active_dataset_revision(root) == revision
    assert generations(root) == existing
    assert not list(root.glob(".manifest-*"))


@windows_only
@pytest.mark.parametrize("error", [
    PermissionError(1, "not held", "manifest.json", 1314),
    PermissionError("in use"),
    OSError(28, "disk full", "manifest.json", 112),
], ids=["other-permission", "no-code", "disk-full"])
def test_only_sharing_violations_are_retried(tmp_path, monkeypatch, error):
    root = tmp_path / "store"
    monkeypatch.setattr(ingest, "ingest_symbol", fake_ingest)
    ingest.ingest_all(["EURUSD"], store_root=root)
    attempts = replace_refusing(monkeypatch, math.inf, error)
    with pytest.raises(OSError):
        ingest.ingest_all(["GBPUSD"], store_root=root)
    assert len(attempts) == 1


def test_a_sharing_violation_is_never_retried_off_windows(monkeypatch):
    monkeypatch.setattr(ingest.os, "name", "posix")
    assert not ingest.is_sharing_violation(opening_refused())


@windows_only
@pytest.mark.parametrize("error", [opening_refused(), refusal(SHARING_VIOLATION)],
                         ids=["open", "win32"])
def test_reading_the_revision_waits_out_a_publication(imported, monkeypatch, error):
    root, _csvs = imported
    revision = active_dataset_revision(root)
    attempts = manifest_reads_refused(monkeypatch, 2, error)
    assert active_dataset_revision(root) == revision
    assert len(attempts) == 3


@windows_only
def test_a_new_store_waits_out_a_publication_instead_of_going_legacy(imported, monkeypatch):
    root, _csvs = imported
    revision = active_dataset_revision(root)
    manifest_reads_refused(monkeypatch, 2, opening_refused())
    store = CandleStore(root)
    try:
        assert store.recorded_revision == revision
        assert store.path_for("EURUSD", "M5").is_relative_to(root / "generations")
    finally:
        store.close()


@windows_only
def test_a_manifest_that_stays_refused_is_an_error_and_never_legacy(imported, monkeypatch):
    root, _csvs = imported
    monkeypatch.setattr(ingest, "MANIFEST_READ_RETRY_SECONDS", 0.05)
    attempts = manifest_reads_refused(monkeypatch, math.inf, opening_refused())
    began = time.monotonic()
    with pytest.raises(PermissionError):
        active_dataset_revision(root)
    with pytest.raises(PermissionError):
        CandleStore(root)
    assert time.monotonic() - began < 1
    assert len(attempts) > 2


@pytest.mark.parametrize("content", [
    "{ not json", "", "[]", '"text"', '{"generation": "generations/x"}',
    '{"dataset_revision": "x"}',
], ids=["truncated", "empty", "list", "string", "no-revision", "no-generation"])
def test_a_corrupt_manifest_is_never_taken_for_a_legacy_store(tmp_path, content):
    root = tmp_path / "store"
    root.mkdir()
    (root / "manifest.json").write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        active_dataset_revision(root)
    with pytest.raises(ValueError):
        CandleStore(root)


def test_a_store_with_no_manifest_or_an_old_one_is_still_legacy(tmp_path):
    empty, flat = tmp_path / "empty", tmp_path / "flat"
    flat.mkdir()
    (flat / "manifest.json").write_text('{"datasets": []}', encoding="utf-8")
    for root in (empty, flat):
        assert active_dataset_revision(root) == "legacy"
        store = CandleStore(root)
        try:
            assert store.recorded_revision == "legacy"
        finally:
            store.close()


def test_a_corrupt_manifest_does_not_reach_a_running_dashboard_as_legacy(imported):
    root, _csvs = imported
    service = SimpleNamespace(store=CandleStore(root))
    app = SimpleNamespace(state=SimpleNamespace(market_xray_service=service))
    try:
        assert market_xray._app_service(app) is service
        (root / "manifest.json").write_text("{ not json", encoding="utf-8")
        with pytest.raises(ValueError):
            market_xray._app_service(app)
        assert app.state.market_xray_service is service
    finally:
        service.store.close()


# Reads the way every backend request does, as fast as it can, and reports anything
# other than a revision that really was published.
READING_THE_MANIFEST = """
import json, sys
from pathlib import Path
from market.store import CandleStore, active_dataset_revision

root, stop = Path(sys.argv[1]), Path(sys.argv[2])
seen, errors, reads = set(), [], 0
while not stop.exists():
    reads += 1
    try:
        seen.add(active_dataset_revision(root))
        if reads % 25 == 0:
            store = CandleStore(root)
            seen.add(store.recorded_revision)
            store.close()
    except Exception as exc:
        errors.append(repr(exc)[:200])
print(json.dumps({"reads": reads, "seen": sorted(seen), "errors": errors[:5]}))
"""

PUBLISHING_THE_MANIFEST = """
import json, sys
from pathlib import Path
from market import ingest

store, csvs, symbol, rounds = sys.argv[1:5]
published = [
    ingest.ingest_all([symbol], store_root=Path(store), csv_root=Path(csvs))["dataset_revision"]
    for _ in range(int(rounds))
]
print(json.dumps(published))
"""


@windows_only
def test_readers_and_publishers_in_separate_processes_never_disturb_each_other(tmp_path):
    csvs, root, stop = tmp_path / "csv", tmp_path / "store", tmp_path / "stop"
    write_sources(csvs, ["EURUSD", "GBPUSD", "AUDUSD"])
    first = ingest.ingest_all(["EURUSD"], store_root=root, csv_root=csvs)
    # As large as a real 18-instrument manifest, which is what keeps readers
    # inside the file long enough to collide with a rename.
    manifest = active(root)
    manifest["datasets"][0]["padding"] = "x" * 380_000
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def launch(script, *arguments):
        return subprocess.Popen(
            [sys.executable, "-c", script, *map(str, arguments)],
            cwd=BACKEND, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    readers = [launch(READING_THE_MANIFEST, root, stop) for _ in range(3)]
    publishers = [launch(PUBLISHING_THE_MANIFEST, root, csvs, symbol, 8)
                  for symbol in ("GBPUSD", "AUDUSD")]
    published, problems = {first["dataset_revision"]}, []
    for process in publishers:
        output, errors = process.communicate(timeout=120)
        if process.returncode:
            problems.append(errors[-300:])
        else:
            published.update(json.loads(output))
    stop.touch()
    reports = []
    for process in readers:
        output, errors = process.communicate(timeout=60)
        assert process.returncode == 0, errors[-300:]
        reports.append(json.loads(output))

    assert problems == []
    assert len(published) == 17
    assert all(report["reads"] > 0 and report["errors"] == [] for report in reports), reports
    assert set().union(*(report["seen"] for report in reports)) <= published
    assert set(active(root)["symbols_ok"]) == {"EURUSD", "GBPUSD", "AUDUSD"}
