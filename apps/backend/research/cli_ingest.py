"""Resumable multi-year ingestion driver.  python -m research.cli_ingest XAUUSD 2020-01-01 2026-10-01 [--workers 4]

Safe to interrupt and rerun: verified raw files are skipped via the manifest, finished months are skipped via provenance."""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone

from .config import resolve_data_root
from .ingest import ingest_months
from .providers.dukascopy import DukascopyProvider, PartialFetchError


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("instrument")
    ap.add_argument("start")
    ap.add_argument("end")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--timeout", type=float, default=45.0)
    ap.add_argument("--passes", type=int, default=50, help="max resume passes over transient failures")
    a = ap.parse_args(argv)
    s = datetime.fromisoformat(a.start).replace(tzinfo=timezone.utc)
    e = datetime.fromisoformat(a.end).replace(tzinfo=timezone.utc)
    root = resolve_data_root()
    prov = DukascopyProvider(workers=a.workers, timeout=a.timeout)
    t0 = time.time()
    for n in range(a.passes):
        try:
            totals = ingest_months(prov, a.instrument, s, e, root, log=lambda m: print(m, flush=True))
            totals["wall_seconds"] = round(time.time() - t0, 1)
            totals["governor"] = prov.governor.stats
            print("DONE", json.dumps(totals), flush=True)
            return 0
        except PartialFetchError as ex:
            print(f"pass {n}: {ex}", flush=True)
            time.sleep(20)
    print("INCOMPLETE", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
