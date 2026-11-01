"""Build a separate candle store for a historical replay from Dukascopy's public feed.

The store the desktop normally reads ends where its source CSVs end, so a later
date has nothing behind it. This downloads real M1 bid and ask candles, writes
them in the layout `market.ingest` already reads, and publishes through the normal
import. Everything lands under `--data-root`, never under the default data root.

The result is Dukascopy data and is labelled as such. It is not OANDA data, and
nothing here treats one broker's prices as another's.

    python tools/build_replay_store.py --data-root D:/TARS_REPLAY_20261027
"""
from __future__ import annotations

import argparse
import json
import lzma
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "backend"))

from market import universe  # noqa: E402
from market.ingest import ingest_all  # noqa: E402
from research.providers.base import RawCache  # noqa: E402
from research.providers.dukascopy import BASE, RECORD, DukascopyProvider  # noqa: E402

SOURCE = {
    "provider": "dukascopy",
    "label": "Dukascopy",
    "vendor_url": BASE,
    "prices": "bid/ask M1 candles, stored as mid; volume is bid-side vendor tick volume",
    "caveat": "Dukascopy quotes, not OANDA quotes. Prices differ between brokers.",
}
# The replay this was built for: 15:30 to 19:30 IST on 27 October 2026.
WINDOW_START = pd.Timestamp("2026-10-27T10:00:00", tz="UTC")
WINDOW_END = pd.Timestamp("2026-10-27T14:00:00", tz="UTC")
CSV_HEADER = ["Time (UTC)", "Open", "High", "Low", "Close", "Volume"]


class UniverseFeed(DukascopyProvider):
    """The provider's verified download and cache, for every Market instrument.
    The feed's instrument code is the symbol, and `research.instruments` only
    describes the two it studies."""

    @staticmethod
    def code(instrument: str) -> str:
        return universe.get(instrument).symbol


def decode(symbol: str, raw: list) -> tuple[pd.DataFrame, pd.DataFrame]:
    scale = 10 ** universe.get(symbol).price_precision
    sides: dict[str, list[pd.DataFrame]] = {"BID": [], "ASK": []}
    for item in raw:
        day, side = item.key.split("/")
        data = item.path.read_bytes()
        if not data:
            continue
        body = lzma.decompress(data)
        rows = [RECORD.unpack_from(body, i) for i in range(0, len(body), RECORD.size)]
        frame = pd.DataFrame(rows, columns=["sec", "Open", "Close", "Low", "High", "Volume"])
        frame["ts"] = pd.Timestamp(day, tz="UTC") + pd.to_timedelta(frame["sec"], unit="s")
        for column in ("Open", "High", "Low", "Close"):
            frame[column] = frame[column] / scale
        sides[side].append(frame.drop(columns=["sec"]))
    return pd.concat(sides["BID"], ignore_index=True), pd.concat(sides["ASK"], ignore_index=True)


def is_padding(side: pd.DataFrame) -> pd.Series:
    """Dukascopy pads closed periods with flat, zero-volume minutes. They are not
    observations, and would become bars on a weekend."""
    flat = (side["Open"] == side["High"]) & (side["High"] == side["Low"]) & (side["Low"] == side["Close"])
    return flat & (side["Volume"] == 0)


def to_minutes(bid: pd.DataFrame, ask: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, int]:
    """Minutes quoted on both sides, without the padding."""
    joined = bid.merge(ask, on="ts", suffixes=("_bid", "_ask"), how="inner")
    bid_side = joined[["ts"] + [c + "_bid" for c in CSV_HEADER[1:]]].copy()
    ask_side = joined[["ts"] + [c + "_ask" for c in CSV_HEADER[1:]]].copy()
    bid_side.columns = ask_side.columns = ["ts"] + CSV_HEADER[1:]
    keep = ~(is_padding(bid_side) & is_padding(ask_side))
    # The ingester adds the two volumes, and the feed publishes them separately
    # as the same figure, so the ask side carries none.
    ask_side["Volume"] = 0.0
    return bid_side[keep].sort_values("ts"), ask_side[keep].sort_values("ts"), int((~keep).sum())


def write_csv(frame: pd.DataFrame, path: Path) -> None:
    out = frame.copy()
    out.insert(0, "Time (UTC)", out.pop("ts").dt.strftime("%Y.%m.%d %H:%M:%S"))
    out = out[CSV_HEADER]
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_suffix(".tmp")
    out.to_csv(staged, index=False)
    os.replace(staged, path)


def check_window(symbol: str, bid: pd.DataFrame, ask: pd.DataFrame) -> dict:
    """What the replay window actually holds. Reports gaps; never fills one."""
    expected = pd.date_range(WINDOW_START, WINDOW_END, freq="1min", inclusive="left")
    inside = bid[(bid["ts"] >= WINDOW_START) & (bid["ts"] < WINDOW_END)]
    other = ask[(ask["ts"] >= WINDOW_START) & (ask["ts"] < WINDOW_END)]
    return {
        "symbol": symbol,
        "minutes_expected": len(expected),
        "minutes_present": len(inside),
        "missing": [t.isoformat() for t in expected.difference(pd.DatetimeIndex(inside["ts"]))][:20],
        "ohlc_invalid": int(((inside["High"] < inside[["Open", "Close"]].max(axis=1))
                             | (inside["Low"] > inside[["Open", "Close"]].min(axis=1))).sum()),
        "crossed_quotes": int((other["Close"].to_numpy() < inside["Close"].to_numpy()).sum()),
        "last_minute_overall": bid["ts"].max().isoformat(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--start", default="2026-01-01")
    parser.add_argument("--end", default="2026-10-29", help="exclusive")
    parser.add_argument("--symbols", nargs="*", default=list(universe.SYMBOLS))
    parser.add_argument("--fetch-only", action="store_true")
    args = parser.parse_args()

    store_root = args.data_root / "market" / "xray_store"
    csv_root = args.data_root / "source_csv"
    start = datetime.fromisoformat(args.start).replace(tzinfo=UTC)
    end = datetime.fromisoformat(args.end).replace(tzinfo=UTC)
    # Friday-to-Sunday days are still fetched: only Saturday is skipped, as closed.
    feed = UniverseFeed(workers=6, min_interval=0.15)
    cache = RawCache(args.data_root / "raw")
    cache.root.mkdir(parents=True, exist_ok=True)

    report: dict = {
        "generated_at": datetime.now(UTC).isoformat(),
        "source": SOURCE,
        "range": [args.start, args.end],
        "window_utc": [WINDOW_START.isoformat(), WINDOW_END.isoformat()],
        "symbols": [],
    }
    for symbol in args.symbols:
        raw = feed.fetch(symbol, start, end, cache)
        print(f"{symbol}: {len(raw)} files verified", flush=True)
        if args.fetch_only:
            continue
        bid, ask, padding = to_minutes(*decode(symbol, raw))
        meta = universe.get(symbol)
        stem = f"{args.start.replace('-', '.')}_{args.end.replace('-', '.')}.csv"
        for side, frame in (("Bid", bid), ("Ask", ask)):
            write_csv(frame, csv_root / meta.source_dir / f"{meta.source_stem}_{side}_{stem}")
        report["symbols"].append({**check_window(symbol, bid, ask), "minutes_written": len(bid),
                                  "padding_dropped": padding})
    if args.fetch_only:
        return 0

    manifest = ingest_all(
        list(args.symbols), store_root=store_root, start=args.start, csv_root=csv_root, source=SOURCE
    )
    report["dataset_revision"] = manifest["dataset_revision"]
    report["symbols_ok"] = manifest["symbols_ok"]
    report["symbols_failed"] = manifest["symbols_failed"]
    (args.data_root / "replay_validation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"published {manifest['dataset_revision']}: {len(manifest['symbols_ok'])} ok, "
          f"{len(manifest['symbols_failed'])} failed")
    return 1 if manifest["symbols_failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
