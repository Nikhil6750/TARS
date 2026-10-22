"""Dukascopy historical candle feed (public datafeed): per-UTC-day LZMA files of M1 candles, separate BID and ASK.

Record layout (24 bytes, big endian): uint32 seconds-from-day-start, uint32 open, uint32 close, uint32 low, uint32 high,
float32 volume. Prices are integers scaled by 10**price_precision. URL months are ZERO-indexed (00 = January)."""
from __future__ import annotations

import hashlib
import http.client
import lzma
import random
import threading
import struct
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from ..instruments import get_meta
from ..schema import make_bars
from ..sessions import is_daily_break, is_market_closed
from .base import MarketDataProvider, RawCache, RawFile
from .download import HttpFailure, IntegrityError, Manifest, RateGovernor, backoff_delay, verify_bi5

NORMALIZATION_VERSION = "dukascopy-m1-v3"  # v3: also drops long (>=30 min) flat zero-volume runs (holidays / dead periods)
BASE = "https://datafeed.dukascopy.com/datafeed"
RECORD = struct.Struct(">IIIIIf")
LONG_DEAD_RUN_MIN = 30
AVG_FILE_BYTES = 20_000  # observed ~15-17 KB/day/side for XAUUSD M1; padded


def drop_dead_padding(df: pd.DataFrame, meta) -> tuple[pd.DataFrame, dict]:
    """Dukascopy publishes a full 1440-row file for every UTC day and pads closed periods with FLAT, ZERO-VOLUME candles.
    Those rows are not market data. Rule (deterministic, counted, reported in provenance, raw files untouched): drop a row only if
    it is inside the instrument's weekly closure or daily break AND all eight prices are identical AND volume == 0.
    Any NON-flat row inside a closure is kept and surfaces as a quality warning."""
    ts = df["timestamp_utc"]
    closed = (is_market_closed(ts, meta) | is_daily_break(ts, meta)).to_numpy()
    flat = (df["bid_open"] == df["bid_close"]).to_numpy() & \
           (df["bid_high"] == df["bid_low"]).to_numpy() & (df["ask_high"] == df["ask_low"]).to_numpy() & \
           (df["ask_open"] == df["ask_close"]).to_numpy() & (df["bid_open"] == df["bid_high"]).to_numpy() & \
           (df["ask_open"] == df["ask_high"]).to_numpy()
    zero = (df["volume"].fillna(0) == 0).to_numpy()
    dead = flat & zero
    drop_closed = closed & dead
    # Holidays and other dead periods are padded too, but sit outside any weekly/daily closure window. A run of >= 30
    # consecutive flat, zero-volume minutes is not a market (live gold quotes never freeze that long): drop it, count it.
    run_id = np.cumsum(np.concatenate([[True], dead[1:] != dead[:-1]]))
    run_len = pd.Series(run_id).map(pd.Series(run_id).value_counts()).to_numpy()
    drop_long = dead & (run_len >= LONG_DEAD_RUN_MIN) & ~drop_closed
    drop = drop_closed | drop_long
    stats = {
        "rule": "drop closed-market padding (in closure/break AND flat OHLC both sides AND volume==0)",
        "rows_in": int(len(df)), "rows_dropped": int(drop.sum()), "rows_out": int((~drop).sum()),
        "closed_nonflat_rows_kept": int((closed & ~(flat & zero)).sum()),
        "dropped_weekly_closure": int((drop & is_market_closed(ts, meta).to_numpy()).sum()),
        "dropped_daily_break": int((drop_closed & ~is_market_closed(ts, meta).to_numpy()).sum()),
        "dropped_long_dead_runs": int(drop_long.sum()), "long_dead_run_min_minutes": LONG_DEAD_RUN_MIN,
        "open_hours_flat_rows_kept": int((dead & ~drop).sum())}
    return df.loc[~drop].reset_index(drop=True), stats


class PartialFetchError(RuntimeError):
    pass


class DukascopyProvider(MarketDataProvider):
    name = "dukascopy"

    def __init__(self, workers: int = 8, timeout: float = 45.0, retries: int = 14, opener=None, min_interval: float = 0.12,
                 skip_closed_days: bool = True, sleep=time.sleep) -> None:
        self.workers, self.timeout, self.retries = workers, timeout, retries
        self._open = opener or self._http_get_once
        self._sleep = sleep
        self.skip_closed_days = skip_closed_days   # Saturday (UTC) lies wholly inside the weekly closure of every supported instrument
        self.governor = RateGovernor(workers, min_interval)
        self._tls = threading.local()
        self.last_normalization_stats: dict = {}
        self.last_fetch_stats: dict = {}

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

    def _conn(self) -> http.client.HTTPSConnection:
        c = getattr(self._tls, "conn", None)
        if c is None:
            c = self._tls.conn = http.client.HTTPSConnection("datafeed.dukascopy.com", timeout=self.timeout)
        return c

    def _http_get_once(self, url: str) -> bytes:
        """One HTTP attempt over a per-thread keep-alive connection. 404 = day without data. 429/503 raise HttpFailure."""
        path = url.split("datafeed.dukascopy.com", 1)[1]
        try:
            c = self._conn()
            c.request("GET", path, headers={"User-Agent": "Mozilla/5.0", "Connection": "keep-alive"})
            r = c.getresponse()
            body = r.read()
        except Exception:
            try:
                self._tls.conn.close()
            except Exception:
                pass
            self._tls.conn = None
            raise
        if r.status == 200:
            return body
        if r.status == 404:
            return b""
        ra = r.getheader("Retry-After")
        raise HttpFailure(r.status, float(ra) if ra and ra.replace(".", "", 1).isdigit() else None)

    # ---- contract -------------------------------------------------------------------------------
    def estimate_download_bytes(self, instrument: str, start: datetime, end: datetime) -> int:
        n = len(self.days(start, end))
        if self.skip_closed_days:
            n = sum(1 for d in self.days(start, end) if d.weekday() != 5)
        return n * 2 * AVG_FILE_BYTES

    def _download(self, url: str):
        """Fetch + verify one file with governed retries. Returns (payload, rows, attempts). Raises after exhausting attempts."""
        last: Exception | None = None
        for attempt in range(self.retries):
            self.governor.acquire()
            outcome, retry_after, miss_wait = "error", None, None
            try:
                payload = self._open(url)
                rows = verify_bi5(payload)
                outcome = "ok"
                return payload, rows, attempt + 1
            except HttpFailure as exc:
                last = exc
                if exc.status == 503 and not exc.retry_after:
                    outcome = "miss"
                    miss_wait = 4.0 + random.random() * 4.0
                else:
                    outcome = "throttled" if exc.status in (429, 503) else "error"
                    retry_after = exc.retry_after
            except IntegrityError as exc:
                last = exc            # truncated/corrupt body: refetch, never cache
            except Exception as exc:  # timeouts, resets
                last = exc
                kinds = self.governor.stats.setdefault("error_kinds", {})
                kinds[type(exc).__name__] = kinds.get(type(exc).__name__, 0) + 1
            finally:
                self.governor.release(outcome, retry_after)
            self._sleep(retry_after if retry_after else (miss_wait if miss_wait else backoff_delay(attempt)))
        raise RuntimeError(f"download failed after {self.retries} attempts: {url}: {last}")

    def fetch(self, instrument: str, start: datetime, end: datetime, cache: RawCache, progress=None) -> list[RawFile]:
        code = self.code(instrument)
        manifest = Manifest(cache.root / "manifest.sqlite")
        days = [d for d in self.days(start, end) if not (self.skip_closed_days and d.weekday() == 5)]
        jobs = [(d, side) for d in days for side in ("BID", "ASK")]
        done = {"n": 0}

        def one(job):
            d, side = job
            key = f"{self.name}/{code}/{d:%Y-%m-%d}/{side}"
            rel = (self.name, code, f"{d:%Y}", f"{d:%m}", f"{d:%d}_{side}.bi5")
            path = cache.path_for(*rel)
            url = self.url(code, d, side)
            rec = manifest.get(key)
            if rec and path.exists() and path.stat().st_size == rec["size"]:   # verified earlier: no network, no re-hash needed
                rf = RawFile(path, f"{d:%Y-%m-%d}/{side}", url, rec["sha256"], rec["size"], rec["retrieved_at"])
            else:
                if path.exists() and not rec:  # file from before the manifest existed: adopt it after verifying
                    b = path.read_bytes()
                    rows = verify_bi5(b)
                    digest = hashlib.sha256(b).hexdigest()
                    manifest.put(key, url, "/".join(rel), digest, len(b), rows, "OK" if b else "EMPTY", 0, "adopted")
                    rf = RawFile(path, f"{d:%Y-%m-%d}/{side}", url, digest, len(b), "adopted")
                else:
                    payload, rows, attempts = self._download(url)
                    p, digest = cache.put(rel, payload)
                    now = datetime.now(timezone.utc).isoformat()
                    manifest.put(key, url, "/".join(rel), digest, len(payload), rows, "OK" if payload else "EMPTY", attempts, now)
                    rf = RawFile(p, f"{d:%Y-%m-%d}/{side}", url, digest, len(payload), now)
            done["n"] += 1
            if progress:
                progress(done["n"], len(jobs), key)
            return rf

        def safe(job):
            try:
                return one(job)
            except Exception as exc:  # keep going; completed files stay cached/manifested for the next pass
                return exc

        with ThreadPoolExecutor(self.workers) as ex:
            results = list(ex.map(safe, jobs))
        self.last_fetch_stats = {**self.governor.stats, "files": len(jobs)}
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
        out, self.last_normalization_stats = drop_dead_padding(df, meta)
        return out

    def describe_source(self, instrument: str) -> dict:
        meta = get_meta(instrument)
        return {"provider": self.name, "vendor_url": BASE, "source_format": "dukascopy .bi5 (LZMA, 24-byte big-endian candle records)",
                "price_scale": 10 ** meta.price_precision, "timeframe": "M1", "sides": ["BID", "ASK"],
                "timestamps": "UTC (file day + seconds offset)", "volume": "bid-side candle volume as published (not exchange volume)",
                "normalization_version": NORMALIZATION_VERSION,
                "caveats": ["retail/indicative quotes, not exchange-matched prices", "bar timestamps are minute OPEN times",
                            "volume is vendor tick-volume-like and not comparable across vendors"]}
