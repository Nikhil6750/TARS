"""Exercise replay controls in the real packaged Tauri WebView2 via local CDP.

The optional missing-symbol seed is a test preference, never fabricated market
data. The test moves the shared replay cursor; run with other viewers idle.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from webview_test_target import require_packaged_webview


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cdp", required=True)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed-missing-symbol", action="store_true")
    args = parser.parse_args()
    checks = []
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def check(name, detail):
        checks.append({"name": name, "pass": True, "detail": detail})
        print(f"PASS {name}: {detail}", flush=True)

    try:
        check("Native package owns WebView2", require_packaged_webview(args.cdp, args.app))
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(args.cdp)
            pages = [p for context in browser.contexts for p in context.pages if "market.html" in p.url]
            assert len(pages) == 1
            page = pages[0]
            assert page.evaluate("Boolean(window.__TAURI_INTERNALS__?.invoke)")
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            if args.seed_missing_symbol:
                page.evaluate("""() => localStorage.setItem('tars.market-xray.preferences.v1',
                    JSON.stringify({timeframe:'H1',symbol:'CHFJPY',strengthWindow:'1D',showInvalidated:false}))""")
                page.reload(wait_until="domcontentloaded")
            play = page.get_by_role("button", name=re.compile("play"))
            play.wait_for(timeout=120000)
            selected = page.evaluate("JSON.parse(localStorage.getItem('tars.market-xray.preferences.v1')).symbol")
            assert selected == "XAUUSD", selected
            check("Saved unavailable symbol recovers", {"selected": selected, "seeded": args.seed_missing_symbol})
            cursor = page.locator(".mx-replay-note .mx-num")
            window = page.get_by_role("button", name=re.compile("XAUUSD · 27 Oct"))
            window.click()
            expect(cursor).to_have_text("2026-10-27 10:00", timeout=30000)
            expect(page.locator(".mx-replay-note")).to_contain_text("15:30 IST")
            check("Open historical window", cursor.inner_text())
            page.get_by_role("button", name="Next candle", exact=True).click()
            expect(cursor).to_have_text("2026-10-27 11:00", timeout=30000)
            check("Next Candle", cursor.inner_text())
            page.get_by_role("button", name="⏴ 1", exact=True).click()
            expect(cursor).to_have_text("2026-10-27 10:00", timeout=30000)
            check("Step Back", cursor.inner_text())

            def seek(at):
                page.get_by_label("Seek to UTC time", exact=True).fill(at)
                page.get_by_role("button", name="seek UTC", exact=True).click()
                expect(cursor).to_have_text(at.replace("T", " "), timeout=30000)

            seek("2026-10-27T11:00")
            check("Seek", cursor.inner_text())
            play.click()
            expect(cursor).not_to_have_text("2026-10-27 11:00", timeout=30000)
            page.get_by_role("button", name=re.compile("pause")).click()
            expect(play).to_be_visible()
            page.wait_for_timeout(1500)  # allow a request already in flight to settle
            paused = cursor.inner_text()
            page.wait_for_timeout(3500)
            assert cursor.inner_text() == paused
            check("Play then Pause", paused)
            for tf in ("M5", "H4", "H1"):
                page.locator("#mx-section-xray").get_by_role("button", name=tf, exact=True).click()
                expect(page.locator(".mx-panel-title", has_text=f"XAUUSD · {tf}")).to_be_visible(timeout=30000)
                expect(page.locator(".mx-replay-note")).to_contain_text(f"whole {tf} bars")
                check(f"Switch timeframe {tf}", cursor.inner_text())
            seek("2026-10-27T13:00")
            play.click()
            expect(cursor).to_have_text("2026-10-27 14:00", timeout=30000)
            expect(play).to_be_visible(timeout=30000)
            page.wait_for_timeout(3500)
            expect(cursor).to_have_text("2026-10-27 14:00")
            check("Play stops at window end", cursor.inner_text())
            expect(page.get_by_text("Currency strength unavailable:", exact=False)).to_be_visible()
            expect(page.get_by_text("Cross-instrument rankings are unavailable.", exact=False)).to_be_visible()
            expect(page.get_by_text("DUKASCOPY (HISTORICAL)", exact=True)).to_be_visible()
            expect(page.get_by_text("RECONSTRUCTED ANALYSIS · NOT A RECORDED PREDICTION", exact=True)).to_be_visible()
            assert not page.locator('.mx-provenance-tag[data-source="LIVE_FEED"]').count()
            check("Insufficient comparison data and honest provenance", "No FX strength; coverage warns cross-instrument rankings unavailable; historical Dukascopy")
            seek("2026-10-27T10:00")
            page.evaluate("window.scrollTo(0,0)")
            page.screenshot(path=str(args.output.parent / "webview-oct27-replay.png"), full_page=False)
            assert not errors, errors
            check("WebView2 JavaScript errors", errors)
        return 0
    except Exception as error:
        checks.append({"name": "Verification error", "pass": False, "detail": repr(error)})
        raise
    finally:
        args.output.write_text(json.dumps(checks, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
