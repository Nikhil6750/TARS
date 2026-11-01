"""Verify actual Tauri WebView2 labels through its opt-in local CDP test port.

Rejects ordinary browser pages. Native resizing uses the existing Tauri window
command, never emulated viewport dimensions. Run with an 18-instrument store.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from webview_test_target import require_packaged_webview


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cdp", required=True)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    evidence = {"sizes": [], "page_errors": []}
    try:
        evidence["package"] = require_packaged_webview(args.cdp, args.app)
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(args.cdp)
            pages = [p for context in browser.contexts for p in context.pages if "market.html" in p.url]
            assert len(pages) == 1, [p.url for p in pages]
            page = pages[0]
            assert page.evaluate("Boolean(window.__TAURI_INTERNALS__?.invoke)"), "Not a Tauri page"
            evidence["url"] = page.url
            evidence["tauri"] = True
            page.on("pageerror", lambda e: evidence["page_errors"].append(str(e)))
            labels = page.locator(".mx-xray-label")
            labels.first.wait_for(timeout=120000)
            assert labels.count() == 18, labels.count()
            for width, height in ((1024, 680), (1280, 720), (1440, 800), (1680, 930)):
                page.evaluate("""async ([width,height]) => {
                    await window.__TAURI_INTERNALS__.invoke('plugin:window|set_size', {
                      label:'market',value:{Logical:{width,height}}
                    });
                }""", [width, height])
                page.wait_for_timeout(800)
                labels.first.scroll_into_view_if_needed()
                geometry = page.evaluate("""() => {
                    const wrap=document.querySelector('.mx-xray-wrap');
                    const wr=wrap.getBoundingClientRect();
                    const labels=[...document.querySelectorAll('.mx-xray-label')].map(e=>{
                      const r=e.getBoundingClientRect(), s=getComputedStyle(e);
                      return {symbol:e.dataset.symbol,x:r.x,y:r.y,w:r.width,h:r.height,
                        font:s.fontSize,textFits:e.scrollWidth<=e.clientWidth,
                        withinPlot:r.left>=wr.left-.5 && r.right<=wr.right+.5};
                    });
                    const overlap=[];
                    for(let i=0;i<labels.length;i++) for(let j=i+1;j<labels.length;j++) {
                      const a=labels[i],b=labels[j];
                      if(a.x<b.x+b.w && a.x+a.w>b.x && a.y<b.y+b.h && a.y+a.h>b.y)
                        overlap.push([a.symbol,b.symbol]);
                    }
                    return {inner:[innerWidth,innerHeight],dpr:devicePixelRatio,
                      mode:document.querySelector('.mx-xray-rail')?'rail':'legend',
                      labels,overlap,horizontalOverflow:document.documentElement.scrollWidth>innerWidth};
                }""")
                geometry["requested"] = [width, height]
                evidence["sizes"].append(geometry)
                assert len(geometry["labels"]) == 18
                assert not geometry["overlap"], geometry
                assert not geometry["horizontalOverflow"], geometry
                assert all(e["textFits"] and e["withinPlot"] and e["w"] > 0 for e in geometry["labels"]), geometry
                clicked = []
                for symbol in [e["symbol"] for e in geometry["labels"]]:
                    button = page.locator(f'.mx-xray-label[data-symbol="{symbol}"]')
                    button.click()
                    assert button.get_attribute("aria-pressed") == "true", symbol
                    clicked.append(symbol)
                geometry["clicked"] = clicked
                page.evaluate("window.scrollTo(0,0)")
                page.screenshot(path=str(args.output.parent / f"webview-labels-{width}x{height}.png"), full_page=False)
                print(f"PASS {geometry['inner']}: 18 labels, 0 overlaps, 18 selections ({geometry['mode']})", flush=True)
            page.get_by_title("Fullscreen (F11)", exact=True).click()
            expect(page.get_by_title("Leave fullscreen (F11)", exact=True)).to_be_visible()
            evidence["fullscreen"] = page.evaluate("""async () => ({
                fullscreen:await window.__TAURI_INTERNALS__.invoke('plugin:window|is_fullscreen',{label:'market'}),
                minimized:await window.__TAURI_INTERNALS__.invoke('plugin:window|is_minimized',{label:'market'}),
                inner:[innerWidth,innerHeight],screen:[screen.width,screen.height],dpr:devicePixelRatio,
                labels:document.querySelectorAll('.mx-xray-label').length
            })""")
            assert evidence["fullscreen"]["fullscreen"] and not evidence["fullscreen"]["minimized"], evidence["fullscreen"]
            assert evidence["fullscreen"]["labels"] == 18
            page.screenshot(path=str(args.output.parent / "webview-labels-fullscreen.png"))
            page.get_by_title("Leave fullscreen (F11)", exact=True).click()
            expect(page.get_by_title("Fullscreen (F11)", exact=True)).to_be_visible()
            assert not page.evaluate("window.__TAURI_INTERNALS__.invoke('plugin:window|is_fullscreen',{label:'market'})")
            page.evaluate("window.__TAURI_INTERNALS__.invoke('plugin:window|set_size',{label:'market',value:{Logical:{width:1440,height:760}}})")
            page.evaluate("window.scrollTo(0,0)")
            print(f"PASS native fullscreen and restoration: {evidence['fullscreen']}", flush=True)
            assert not evidence["page_errors"], evidence["page_errors"]
        return 0
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(evidence, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
