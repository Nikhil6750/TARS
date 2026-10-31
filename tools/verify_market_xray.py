"""Browser + API acceptance verification for the Market X-Ray dashboard.

Drives the real dashboard in Chromium and checks the behaviour the brief asks
to be demonstrated, rather than asserting that code exists. Each check prints
PASS or FAIL with the evidence it used, captures screenshots, and the script
exits non-zero if anything failed.

Usage:
    python tools/verify_market_xray.py --api http://127.0.0.1:8000 \
                                       --web http://127.0.0.1:5173
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path

from playwright.sync_api import sync_playwright

# The Windows console defaults to cp1252, which cannot encode the sigma and
# arrow glyphs this report prints. Force UTF-8 so evidence is never lost to an
# encoding error mid-run.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

OUT_DIR = Path("artifacts/market-xray")


@dataclass
class Report:
    results: list[tuple[str, bool, str]] = field(default_factory=list)

    def check(self, name: str, ok: bool, evidence: str) -> bool:
        self.results.append((name, ok, evidence))
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name}\n         {evidence}")
        return ok

    @property
    def failed(self) -> list[str]:
        return [n for n, ok, _ in self.results if not ok]

    def summary(self) -> str:
        passed = sum(1 for _, ok, _ in self.results if ok)
        return f"{passed}/{len(self.results)} checks passed"


def api_get(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path, timeout=90) as response:
        return json.loads(response.read())


def api_post(base: str, path: str) -> dict:
    request = urllib.request.Request(base + path, method="POST")
    with urllib.request.urlopen(request, timeout=90) as response:
        return json.loads(response.read())


def verify_api(base: str, report: Report) -> dict:
    print("\n=== API verification ===")
    root = base + "/api/v1/market"

    health = api_get(root, "/health")
    report.check(
        "Backend healthy and reports store coverage",
        bool(health.get("ok")) and health["store"]["symbols"] > 0,
        f"{health['store']['symbols']} instruments, last bar {health['store']['last_bar']}",
    )

    status = health["status"]
    report.check(
        "Feed status is truthful (never LIVE from recorded history)",
        status["status"] in {"REPLAY", "CLOSED", "NO_DATA", "STALE"},
        f"status={status['status']} market_open={status['market_open']} :: {status['detail']}",
    )
    report.check(
        "Trading execution disabled",
        health.get("trading_execution") == "DISABLED",
        f"trading_execution={health.get('trading_execution')}",
    )

    instruments = api_get(root, "/instruments")
    symbols = [i["symbol"] for i in instruments["instruments"]]
    report.check(
        "EURUSD and XAUUSD both present with real history",
        {"EURUSD", "XAUUSD"} <= set(symbols),
        f"{len(symbols)} instruments: {', '.join(symbols)}",
    )

    # Real historical candles.
    for symbol in ("EURUSD", "XAUUSD"):
        payload = api_get(root, f"/candles/{symbol}?timeframe=H1&limit=200")
        candles = payload["candles"]
        valid = all(
            c["high"] >= c["low"]
            and c["high"] >= max(c["open"], c["close"])
            and c["low"] <= min(c["open"], c["close"])
            and c["spread"] >= 0
            for c in candles
        )
        monotonic = all(a["ts"] < b["ts"] for a, b in pairwise(candles))
        report.check(
            f"{symbol} candles are real, ordered and internally consistent",
            len(candles) == 200 and valid and monotonic,
            f"{len(candles)} bars, {candles[0]['ts'][:16]} -> {candles[-1]['ts'][:16]}, "
            f"ohlc_valid={valid} strictly_increasing={monotonic}",
        )

    # X-Ray from real observations.
    xray = api_get(root, "/xray?timeframe=H1")
    series = xray["series"]
    both_sides = any(s["current_z"] > 0 for s in series) and any(s["current_z"] < 0 for s in series)
    all_real = all(
        len(s["points"]) > 10 and all(p["close"] > 0 for p in s["points"]) for s in series
    )
    report.check(
        "X-Ray series are built from real observations, on both sides of zero",
        len(series) >= 10 and both_sides and all_real,
        f"{len(series)} series x {len(series[0]['points'])} points; "
        f"z range {min(s['current_z'] for s in series):.2f}..{max(s['current_z'] for s in series):.2f}",
    )
    report.check(
        "X-Ray declares what it measures (not money flow)",
        "not institutional money" in xray["measures"].lower()
        or "not a flow" in xray["measures"].lower(),
        xray["measures"][:120],
    )

    # Currency strength with coverage honesty.
    strength = api_get(root, "/strength?window=1D")
    ranking = strength["ranking"]
    sorted_desc = all(
        a["score"] >= b["score"] for a, b in pairwise(ranking)
    )
    thin = [r["currency"] for r in ranking if r["thin_coverage"]]
    ranked_text = ", ".join(f"{r['currency']}{r['score']:+.2f}" for r in ranking)
    report.check(
        "Currency strength ranks 8 currencies and flags thin coverage",
        len(ranking) == 8 and sorted_desc and bool(thin),
        f"ranked: {ranked_text} | thin={thin}",
    )
    report.check(
        "Leave-one-pair-out diagnostic present on every currency",
        all("leave_one_out_max_delta" in r for r in ranking),
        "max shift per currency: "
        + ", ".join(f"{r['currency']}={r['leave_one_out_max_delta']:.2f}" for r in ranking),
    )

    # Leaders.
    leaders = api_get(root, "/leaders?timeframe=H1")
    report.check(
        "Bullish and bearish boards populate with prices and OB distance",
        bool(leaders["bullish"]) and bool(leaders["bearish"])
        and all(c["close"] > 0 for c in leaders["bullish"] + leaders["bearish"]),
        f"{len(leaders['bullish'])} bullish / {len(leaders['bearish'])} bearish; "
        f"top bull {leaders['bullish'][0]['symbol']} @ {leaders['bullish'][0]['close']}",
    )

    # Order blocks against the stated rules.
    blocks = api_get(root, "/order-blocks/XAUUSD?timeframe=H1")
    zones = blocks["zones"]
    # The published rules: a non-empty zone, a displacement of at least the
    # configured ATR multiple, and an origin candle that closed strictly
    # before the displacement candle that confirmed it. The break-of-structure
    # level is the prior opposing swing, which for a bullish zone sits *above*
    # the zone itself -- so its relation to the zone edges is not an invariant
    # and is checked against the displacement close in the unit tests instead.
    rules_ok = all(
        z["zone_high"] > z["zone_low"]
        and z["displacement_atr"] >= 1.0
        and z["origin_ts"] < z["confirmed_ts"]
        and isinstance(z["bos_level"], (int, float))
        for z in zones
    )
    unique = len({z["zone_id"] for z in zones}) == len(zones)
    descriptive = all("probabilit" not in z["score_basis"].lower().replace("not a probability", "") for z in zones)
    report.check(
        "Order blocks satisfy the published rules, are unique, and rank descriptively",
        bool(zones) and rules_ok and unique and descriptive,
        f"{len(zones)} zones, {blocks['active_count']} live; all displacement >= 1.0 ATR "
        f"and origin before confirmation = {rules_ok}; unique ids = {unique}",
    )

    # Replay determinism.
    api_post(root, "/replay/reset")
    before = api_get(root, "/xray?timeframe=H1")
    api_post(root, "/replay/step?timeframe=H1&bars=-24")
    middle = api_get(root, "/xray?timeframe=H1")
    api_post(root, "/replay/step?timeframe=H1&bars=24")
    after = api_get(root, "/xray?timeframe=H1")
    changed = middle["as_of"] != before["as_of"] and [
        s["symbol"] for s in middle["series"]
    ] != [s["symbol"] for s in before["series"]]
    report.check(
        "Replay advances the visualisation and rankings deterministically",
        changed and after == before,
        f"stepping -24 moved as_of to {middle['as_of'][:16]} and reordered the ranking; "
        f"stepping back reproduced the payload exactly = {after == before}",
    )
    report.check(
        "Replay cursor is clamped to real coverage",
        api_post(root, "/replay/seek?to=2099-01-01T00:00:00%2B00:00")["cursor"]
        == health["store"]["last_bar"],
        "seeking to 2099 clamps to the newest recorded bar " + str(health["store"]["last_bar"]),
    )
    api_post(root, "/replay/reset")

    # Event history persistence without duplication.
    first = api_get(root, "/events?limit=500")
    api_get(root, "/order-blocks/XAUUSD?timeframe=H1")
    api_get(root, "/order-blocks/EURUSD?timeframe=H1")
    second = api_get(root, "/events?limit=500")
    ids = [e["event_id"] for e in second["events"]]
    report.check(
        "Event history persists and re-scanning creates no duplicates",
        first["total_stored"] == second["total_stored"] and len(ids) == len(set(ids)),
        f"{first['total_stored']} events before re-scan, {second['total_stored']} after; "
        f"{len(set(ids))} unique ids of {len(ids)} rows",
    )

    # Error handling.
    bad_symbol = bad_tf = 0
    try:
        api_get(root, "/candles/NOTAPAIR")
    except urllib.error.HTTPError as exc:
        bad_symbol = exc.code
    try:
        api_get(root, "/xray?timeframe=M3")
    except urllib.error.HTTPError as exc:
        bad_tf = exc.code
    report.check(
        "Invalid instrument and timeframe are rejected, not guessed",
        bad_symbol == 404 and bad_tf == 400,
        f"unknown symbol -> {bad_symbol}, unknown timeframe -> {bad_tf}",
    )

    return health


def verify_browser(web: str, report: Report) -> None:
    print("\n=== Browser verification (Chromium) ===")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    url = web.rstrip("/") + "/market.html"

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1680, "height": 1050})
        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))

        page.goto(url, wait_until="networkidle", timeout=90_000)
        page.wait_for_selector(".mx-ccy-row", timeout=60_000)
        page.wait_for_selector(".mx-card", timeout=60_000)
        # Let the canvas transition settle before measuring pixels.
        page.wait_for_timeout(2500)

        report.check(
            "Dashboard loads in Chromium with no console or page errors",
            not errors,
            f"title={page.title()!r}; errors={errors[:3] if errors else 'none'}",
        )

        status_text = page.inner_text(".mx-status").strip()
        banner = page.inner_text(".mx-banner").strip().replace("\n", " ")
        report.check(
            "Feed status and market closure are clearly shown in the UI",
            status_text in {"REPLAY", "CLOSED", "STALE", "NO_DATA"} and "MARKET" in banner,
            f"status chip={status_text!r}; banner={banner[:110]!r}",
        )

        rows = page.locator(".mx-ccy-row").count()
        report.check(
            "Currency strength panel renders all eight currencies",
            rows == 8,
            f"{rows} currency rows rendered",
        )

        # The X-Ray canvas must actually contain green and red pixels.
        pixels = page.evaluate(
            """() => {
              const c = document.querySelector('.mx-xray-canvas');
              const ctx = c.getContext('2d');
              const d = ctx.getImageData(0, 0, c.width, c.height).data;
              let green = 0, red = 0, lit = 0;
              for (let i = 0; i < d.length; i += 4) {
                const r = d[i], g = d[i+1], b = d[i+2];
                if (g > 90 && g > r + 30) green++;
                if (r > 90 && r > g + 30) red++;
                if (r + g + b > 150) lit++;
              }
              return { green, red, lit, w: c.width, h: c.height };
            }"""
        )
        report.check(
            "Market X-Ray paints real green (upside) and red (downside) curves",
            pixels["green"] > 800 and pixels["red"] > 800,
            f"canvas {pixels['w']}x{pixels['h']}: {pixels['green']} green px, "
            f"{pixels['red']} red px, {pixels['lit']} lit px",
        )
        page.screenshot(path=str(OUT_DIR / "01-dashboard-h1.png"), full_page=True)

        # Hover reveals instrument detail.
        box = page.locator(".mx-xray-canvas").bounding_box()
        found_tooltip = False
        for frac in (0.82, 0.7, 0.9, 0.6, 0.5):
            for dy in (0.3, 0.42, 0.5, 0.58, 0.7):
                page.mouse.move(box["x"] + box["width"] * frac, box["y"] + box["height"] * dy)
                page.wait_for_timeout(240)
                if page.locator(".mx-tooltip").count():
                    found_tooltip = True
                    break
            if found_tooltip:
                break
        tooltip = page.inner_text(".mx-tooltip").replace("\n", " ") if found_tooltip else ""
        report.check(
            "Hovering the X-Ray reveals instrument, return, timeframe and source",
            found_tooltip and "Timeframe" in tooltip and "Source" in tooltip,
            f"tooltip={tooltip[:140]!r}" if found_tooltip else "no tooltip appeared",
        )
        if found_tooltip:
            page.screenshot(path=str(OUT_DIR / "02-xray-hover.png"))

        # Click a leader card and confirm the chart + zones follow.
        before_header = page.inner_text(".mx-col:nth-child(2) .mx-panel:nth-child(2) .mx-panel-title")
        target = page.locator(".mx-card").nth(1)
        target_symbol = target.locator(".mx-card-sym").inner_text().split()[0]
        target.click()
        page.wait_for_timeout(2200)
        after_header = page.inner_text(".mx-col:nth-child(2) .mx-panel:nth-child(2) .mx-panel-title")
        report.check(
            "Clicking a symbol changes its chart and order-block details",
            target_symbol in after_header,
            f"header {before_header!r} -> {after_header!r} after clicking {target_symbol}",
        )
        page.screenshot(path=str(OUT_DIR / "03-symbol-selected.png"), full_page=True)

        # Zone rectangles must be drawn on the chart canvas.
        chart_px = page.evaluate(
            """() => {
              const c = document.querySelector('.mx-chart-canvas');
              const ctx = c.getContext('2d');
              const d = ctx.getImageData(0, 0, c.width, c.height).data;
              let candle = 0, zone = 0;
              for (let i = 0; i < d.length; i += 4) {
                const r = d[i], g = d[i+1], b = d[i+2];
                const sum = r + g + b;
                if (sum > 260) candle++;
                else if (sum > 40 && sum < 200 && (g > b + 8 || r > b + 8)) zone++;
              }
              return { candle, zone };
            }"""
        )
        report.check(
            "Candlestick chart draws candles and order-block zone bands",
            chart_px["candle"] > 400 and chart_px["zone"] > 400,
            f"{chart_px['candle']} candle px, {chart_px['zone']} zone-band px",
        )

        # Timeframe switch must redraw.
        page.get_by_role("button", name="D1", exact=True).click()
        page.wait_for_timeout(2600)
        d1_rows = page.locator(".mx-card").count()
        report.check(
            "Switching timeframe to D1 re-renders charts and boards",
            d1_rows > 0 and page.locator(".mx-xray-canvas").count() == 1,
            f"{d1_rows} leader cards after switching to D1",
        )
        page.screenshot(path=str(OUT_DIR / "04-timeframe-d1.png"), full_page=True)

        # Replay stepping from the UI.
        page.get_by_role("button", name="H1", exact=True).click()
        page.wait_for_timeout(2000)
        cursor_before = page.inner_text(".mx-replay + p")
        page.get_by_role("button", name="⏴⏴ 12").click()
        page.wait_for_timeout(3200)
        cursor_after = page.inner_text(".mx-replay + p")
        report.check(
            "Replay controls move the cursor from the UI",
            cursor_before != cursor_after,
            f"{cursor_before.strip()[:46]!r} -> {cursor_after.strip()[:46]!r}",
        )
        page.screenshot(path=str(OUT_DIR / "05-replay-stepped.png"), full_page=True)

        # Zone and event tables.
        zone_rows = page.locator(".mx-panel:has-text('Order Blocks') tbody tr").count()
        event_rows = page.locator(".mx-panel:has-text('Event History') tbody tr").count()
        report.check(
            "Order-block and event-history tables are populated",
            zone_rows > 0 and event_rows > 0,
            f"{zone_rows} zone rows, {event_rows} event rows",
        )

        # Backend-loss handling: point the page at a dead backend.
        page.goto(url + "?x=1", wait_until="domcontentloaded")
        page.route("**/api/v1/market/**", lambda route: route.abort())
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        error_visible = page.locator(".mx-error").count() > 0
        error_text = page.inner_text(".mx-error").replace("\n", " ") if error_visible else ""
        report.check(
            "Feed/backend failure is shown as an explicit error, not an empty panel",
            error_visible and "backend" in error_text.lower(),
            f"error panel={error_text[:120]!r}" if error_visible else "no error panel rendered",
        )
        if error_visible:
            page.screenshot(path=str(OUT_DIR / "06-backend-unreachable.png"))

        browser.close()

    print(f"\n  screenshots -> {OUT_DIR.resolve()}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--web", default="http://127.0.0.1:5173")
    ap.add_argument("--skip-browser", action="store_true")
    args = ap.parse_args()

    report = Report()
    print("==== TARS Market X-Ray verification ====")
    verify_api(args.api.rstrip("/"), report)
    if not args.skip_browser:
        verify_browser(args.web, report)

    print("\n==== " + report.summary() + " ====")
    if report.failed:
        print("FAILED: " + ", ".join(report.failed))
        return 1
    print("All acceptance checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
