"""Dukascopy historical candle feed (public datafeed): per-UTC-day LZMA files of M1 candles, separate BID and ASK.

Record layout (24 bytes, big endian): uint32 seconds-from-day-start, uint32 open, uint32 close, uint32 low, uint32 high,
float32 volume. Prices are integers scaled by 10**price_precision. URL months are ZERO-indexed (00 = January)."""
from __future__ import annotations

import lzma
import struct
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pandas as pd

from ..instruments import get_meta
from ..schema import make_bars
from ..sessions import is_daily_break, is_market_closed
from .base import MarketDataProvider, RawCache, RawFile

NORMALIZATION_VERSION = "dukascopy-m1-v2"  # v2: explicit, counted removal of provider closed-market padding
BASE = "https://datafeed.dukascopy.com/datafeed"
RECORD = struct.Struct(">IIIIIf")
AVG_FILE_BYTES = 20_000  # observed ~15-17 KB/day/side for XAUUSD M1; padded


class PartialFetchError(RuntimeError):
    pass


class DukascopyProvider(MarketDataProvider):
    name = "dukascopy"

    def __init__(self, workers: int = 6, timeout: float = 90.0, retries: int = 3, opener=None) -> None:
        self.workers, self.timeout, self.retries = workers, timeout, retries
        self._open = opener or self._http_get
        self.last_normalization_stats: dict = {}

    # ---- helpers -----------------------------------------------------------------------------
    @staticmethod
    def code(instrument: str) -> str:
        return get_meta(instrument).instrument

    @staticmethod
    def url(instrument: str, day: datetime, side: str) -> str:
        return f"{BASE}/{instrument}/{day.year:04d}/{day.month - 1:02d}/{day.day:02d}/{side}_candles_min_1.bi5"

    @staticmethod
    def days(start: datetime, end: datetime) -> list[datetime]:
        d = start.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        out = []
        while d < end:
            out.append(d)
            d += timedelta(days=1)
        return out

    def _http_get(self, url: str) -> bytes:
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return r.read()
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return b""
                last = exc
            except Exception as exc:  # network hiccup -> retry
                last = exc
            time.sleep(min(1.0 + 0.5 * attempt, 6))
        raise RuntimeError(f"download failed after {self.retries} attempts: {url}: {last}")

    # ---- contract -------------------------------------------------------------------------------
    def estimate_download_bytes(self, instrument: str, start: datetime, end: datetime) -> int:
        return len(self.days(start, end)) * 2 * AVG_FILE_BYTES

    def fetch(self, instrument: str, start: datetime, end: datetime, cache: RawCache) -> list[RawFile]:
        code = self.code(instrument)
        jobs = [(d, side) for d in self.days(start, end) for side in ("BID", "ASK")]

        def one(job):
            d, side = job
            rel = (self.name, code, f"{d:%Y}", f"{d:%m}", f"{d:%d}_{side}.bi5")
            path = cache.path_for(*rel)
            url = self.url(code, d, side)
            if path.exists():  # immutable cache hit: no network, no overwrite
                import hashlib
                b = path.read_bytes()
                return RawFile(path, f"{d:%Y-%m-%d}/{side}", url, hashlib.sha256(b).hexdigest(), len(b), "cached")
            payload = self._open(url)
            p, digest = cache.put(rel, payload)
            return RawFile(p, f"{d:%Y-%m-%d}/{side}", url, digest, len(payload), datetime.now(timezone.utc).isoformat())

        def safe(job):
            try:
                return one(job)
            except Exception as exc:  # keep going; completed files stay in the immutable cache for the next pass
                return exc

        with ThreadPoolExecutor(self.workers) as ex:
            results = list(ex.map(safe, jobs))
        failed = [r for r in results if isinstance(r, Exception)]
        if failed:
            raise PartialFetchError(f"{len(failed)}/{len(jobs)} files failed (cache keeps the rest; rerun to resume): {failed[0]}")
        return results

    def normalize(self, instrument: str, raw: list[RawFile]) -> pd.DataFrame:
        meta = get_meta(instrument)
        scale = 10 ** meta.price_precision
        sides: dict[str, list[pd.DataFrame]] = {"BID": [], "ASK": []}
        for rf in raw:
            day_s, side = rf.key.split("/")
            data = rf.path.read_bytes()
            if not data:
                continue
            body = lzma.decompress(data)
            if len(body) % RECORD.size:
                raise ValueError(f"{rf.path}: size {len(body)} is not a multiple of {RECORD.size}")
            rows = [RECORD.unpack_from(body, i) for i in range(0, len(body), RECORD.size)]
            df = pd.DataFrame(rows, columns=["sec", "open", "close", "low", "high", "volume"])
            day0 = pd.Timestamp(day_s, tz="UTC")
            df["timestamp_utc"] = day0 + pd.to_timedelta(df["sec"], unit="s")
            for c in ("open", "close", "low", "high"):
                df[c] = df[c] / scale
            sides[side].append(df.drop(columns=["sec"]))
        if not sides["BID"] and not sides["ASK"]:
            return make_bars(pd.DataFrame(columns=["timestamp_utc", "bid_open", "bid_high", "bid_low", "bid_close",
                                                   "ask_open", "ask_high", "ask_low", "ask_close"]), instrument=instrument,
                             provider=self.name)
        def cat(side, prefix):
            if not sides[side]:
                return pd.DataFrame(columns=["timestamp_utc"])
            d = pd.concat(sides[side], ignore_index=True)
            d = d.rename(columns={c: f"{prefix}_{c}" for c in ("open", "high", "low", "close")})
            return d.rename(columns={"volume": f"{prefix}_vol"})
        bid, ask = cat("BID", "bid"), cat("ASK", "ask")
        merged = bid.merge(ask, on="timestamp_utc", how="outer", sort=True)  # one-sided rows stay visible as NaN -> quality BLOCK
        merged["volume"] = merged.get("bid_vol")
        merged["spread"] = merged["ask_close"] - merged["bid_close"]
        cols = ["timestamp_utc", "bid_open", "bid_high", "bid_low", "bid_close", "ask_open", "ask_high", "ask_low", "ask_close", "spread", "volume"]
        out = make_bars(merged[cols], instrument=meta.instrument, provider=self.name)
        return self._drop_closed_market_padding(out, meta)

    def _drop_closed_market_padding(self, df: pd.DataFrame, meta) -> pd.DataFrame:
        """Dukascopy publishes a full 1440-row file for every UTC day and pads closed periods with FLAT, ZERO-VOLUME candles.
        Those rows are not market data. Rule (deterministic, counted, reported in provenance, raw files untouched): drop a row only if
        it is inside the instrument's weekly closure or daily break AND all eight prices are identical AND volume == 0.
        Any NON-flat row inside a closure is kept and surfaces as a quality warning."""
        ts = df["timestamp_utc"]
        closed = (is_market_closed(ts, meta) | is_daily_break(ts, meta)).to_numpy()
        p = df[["bid_open", "bid_high", "bid_low", "bid_close", "ask_open", "ask_high", "ask_low", "ask_close"]]
        flat = (p.nunique(axis=1, dropna=False) <= 2).to_numpy() & (df["bid_open"] == df["bid_close"]).to_numpy() & \
               (df["bid_high"] == df["bid_low"]).to_numpy() & (df["ask_high"] == df["ask_low"]).to_numpy() & \
               (df["ask_open"] == df["ask_close"]).to_numpy() & (df["bid_open"] == df["bid_high"]).to_numpy() & \
               (df["ask_open"] == df["ask_high"]).to_numpy()
        zero = (df["volume"].fillna(0) == 0).to_numpy()
        drop = closed & flat & zero
        self.last_normalization_stats = {
            "rule": "drop closed-market padding (in closure/break AND flat OHLC both sides AND volume==0)",
            "rows_in": int(len(df)), "rows_dropped": int(drop.sum()), "rows_out": int((~drop).sum()),
            "closed_nonflat_rows_kept": int((closed & ~(flat & zero)).sum()),
            "dropped_weekly_closure": int((drop & is_market_closed(ts, meta).to_numpy()).sum()),
            "dropped_daily_break": int((drop & ~is_market_closed(ts, meta).to_numpy()).sum()),
            "open_hours_flat_rows_kept": int((flat & ~closed).sum())}
        return df.loc[~drop].reset_index(drop=True)

    def describe_source(self, instrument: str) -> dict:
        meta = get_meta(instrument)
        return {"provider": self.name, "vendor_url": BASE, "source_format": "dukascopy .bi5 (LZMA, 24-byte big-endian candle records)",
                "price_scale": 10 ** meta.price_precision, "timeframe": "M1", "sides": ["BID", "ASK"],
                "timestamps": "UTC (file day + seconds offset)", "volume": "bid-side candle volume as published (not exchange volume)",
                "normalization_version": NORMALIZATION_VERSION,
                "caveats": ["retail/indicative quotes, not exchange-matched prices", "bar timestamps are minute OPEN times",
                            "volume is vendor tick-volume-like and not comparable across vendors"]}
