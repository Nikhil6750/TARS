"""Compare the running replay API with independently aggregated vendor M1 CSVs.

Uses genuine data supplied by --csv-root. Does not import, download or change
the store; it does move the shared replay cursor, and restores 10:00 UTC.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import lzma
import math
import struct
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd
import websockets

SPANS = {"M5": 5, "M15": 15, "H1": 60, "H4": 240, "D1": 1440}
START = "2026-10-27T10:00:00+00:00"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", required=True)
    parser.add_argument("--csv-root", required=True, type=Path)
    parser.add_argument("--raw-root", type=Path, help="Optional vendor BI5 cache for independent binary decoding")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    checks = []

    def check(name, ok, detail):
        checks.append({"name": name, "pass": bool(ok), "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)
        assert ok, name

    def api(path, post=False, **params):
        url = args.api + "/api/v1/market/" + path
        if params:
            url += "?" + urlencode(params)
        with urlopen(Request(url, method="POST" if post else "GET"), timeout=60) as response:
            return json.load(response)

    def seek(moment):
        return api("replay/seek", True, to=moment)

    async def socket(tf):
        async with websockets.connect(args.api.replace("http", "ws", 1) + "/api/v1/market/ws",
                                      max_size=None) as ws:
            await ws.send(json.dumps({"type": "subscribe", "timeframe": tf}))
            return json.loads(await asyncio.wait_for(ws.recv(), 30))

    try:
        sides = []
        for side in ("Bid", "Ask"):
            paths = list(args.csv_root.rglob(f"XAUUSD_1 Min_{side}_*.csv"))
            assert len(paths) == 1, paths
            frame = pd.read_csv(paths[0])
            frame.index = pd.to_datetime(frame.pop("Time (UTC)"), format="%Y.%m.%d %H:%M:%S", utc=True)
            sides.append(frame)
        bid, ask = sides
        assert bid.index.equals(ask.index)
        mid = (bid[["Open", "High", "Low", "Close"]] + ask[["Open", "High", "Low", "Close"]]) / 2
        window = mid.loc[(mid.index >= START) & (mid.index < "2026-10-27T14:00:00Z")]
        check("Real replay window is complete", len(window) == 240 and window.index.nunique() == 240,
              f"{len(window)} distinct vendor minutes")
        for side in sides:
            quotes = side.loc[window.index]
            assert (quotes.High >= quotes[["Open", "Close"]].max(axis=1)).all()
            assert (quotes.Low <= quotes[["Open", "Close"]].min(axis=1)).all()
        assert (ask.loc[window.index, ["Open", "High", "Low", "Close"]] >= bid.loc[window.index, ["Open", "High", "Low", "Close"]]).all().all()
        check("Window quote integrity", True, "No invalid OHLC envelopes or crossed bid/ask prices")
        if args.raw_root:
            record = struct.Struct(">5if")
            for side_name, side in zip(("BID", "ASK"), sides):
                path = args.raw_root / "dukascopy" / "XAUUSD" / "2026" / "10" / f"27_{side_name}.bi5"
                raw = lzma.decompress(path.read_bytes())
                assert len(raw) % record.size == 0
                rows = [record.unpack_from(raw, offset) for offset in range(0, len(raw), record.size)]
                vendor = {pd.Timestamp("2026-10-27T00:00:00Z") + pd.Timedelta(seconds=r[0]): r for r in rows}
                for at in window.index:
                    row = vendor[at]
                    for field, index in (("Open", 1), ("Close", 2), ("Low", 3), ("High", 4)):
                        assert math.isclose(side.loc[at, field], row[index] / 1000, abs_tol=1e-9)
            check("Original vendor binary matches replay CSV", True, "480 BID/ASK minutes decoded independently from the cached BI5 files")
        candles_checked = 0
        for tf, minutes in SPANS.items():
            expected = mid.resample(f"{minutes}min").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
            for moment in (START, "2026-10-27T10:03:00+00:00", "2026-10-27T14:00:00+00:00"):
                seek(moment)
                chart = api("candles/XAUUSD", timeframe=tf)
                bars = chart["candles"]
                assert bars
                for candle in bars:
                    at = pd.Timestamp(candle["ts"])
                    assert at + pd.Timedelta(minutes=minutes) <= pd.Timestamp(moment), candle
                    row = expected.loc[at]
                    for field in ("open", "high", "low", "close"):
                        assert math.isclose(candle[field], row[field.capitalize()], abs_tol=1e-6), (tf, candle, row.to_dict())
                candles_checked += len(bars)
                check(f"{tf} closed candles at {moment[11:16]}", True, f"{len(bars)} OHLC rows match raw bid/ask aggregation")
        # First visit later data to populate persistent events, then rewind.
        seek("2026-10-28T20:00:00+00:00")
        api("dashboard", timeframe="H1", symbol="XAUUSD")
        seek(START)
        dashboard = api("dashboard", timeframe="H1", symbol="XAUUSD")
        for name in (None, "xray", "strength", "leaders", "chart", "order_blocks", "events"):
            panel = dashboard if name is None else dashboard[name]
            provenance = panel["provenance"]
            assert provenance["source"] == "RECORDED_STORE" and not provenance["is_live"]
            assert provenance["provider"] == "dukascopy"
            observed = provenance.get("observed_at")
            assert observed is None or pd.Timestamp(observed) <= pd.Timestamp(START)
        check("Every panel names historical Dukascopy", True, "7 payloads; is_live=false; observation <= cursor")
        events = dashboard["events"]["events"]
        assert all(pd.Timestamp(e["ts"]) + pd.Timedelta(minutes=SPANS[e["timeframe"]]) <= pd.Timestamp(START) for e in events)
        check("Rewind hides future events", True, f"{len(events)} visible events checked by their own candle duration")
        check("Single-instrument currency strength is insufficient", not dashboard["strength"]["pairs_evaluated"], "No FX pairs evaluated")
        health = api("health")
        check("Execution remains disabled", health["trading_execution"] == "DISABLED", health["trading_execution"])
        for tf in ("M5", "H1", "H4"):
            value = asyncio.run(socket(tf))
            check(f"{tf} WebSocket first frame", value["timeframe"] == tf and not value["provenance"]["is_live"],
                  {"timeframe": value["timeframe"], "provenance": value["provenance"]})
        check("Total independent OHLC comparisons", candles_checked > 4000, candles_checked)
        return 0
    finally:
        try:
            seek(START)
        finally:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(checks, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
