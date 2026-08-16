from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.acceptance

FABRICATED_VISIBLE_METRIC = re.compile(
    r"(?:sharpe|\bdsr\b|expectancy|win\s+rate|drawdown|profitability|"
    r"strategy\s+performance).{0,120}?(?:[<>]=?\s*)?-?\d+(?:\.\d+)?%?",
    re.IGNORECASE | re.DOTALL,
)


def configure_real_backend(context) -> None:
    base_url = os.environ["TARS_BASE_URL"].rstrip("/")
    websocket_url = base_url.replace("http://", "ws://", 1).replace(
        "https://", "wss://", 1
    ) + "/ws"
    context.add_init_script(
        """settings => localStorage.setItem(
            'tars_settings_v1', JSON.stringify(settings)
        )""",
        {"apiEndpoint": base_url, "serverEndpoint": websocket_url},
    )


@pytest.mark.parametrize(
    ("name", "viewport"),
    (
        ("iphone", {"width": 390, "height": 844}),
        ("desktop", {"width": 1440, "height": 900}),
    ),
)
def test_ui_is_renderable_at_target_viewport(
    name: str, viewport: dict[str, int]
) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    console_errors: list[str] = []
    page_errors: list[str] = []
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        context = browser.new_context(viewport=viewport)
        configure_real_backend(context)
        page = context.new_page()
        page.on(
            "console",
            lambda message: console_errors.append(message.text)
            if message.type == "error"
            else None,
        )
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        response = page.goto(
            os.environ["TARS_FRONTEND_URL"],
            wait_until="networkidle",
            timeout=10_000,
        )
        assert response is not None and response.ok
        assert page.locator("body").inner_text().strip(), f"{name} UI rendered no content"
        overflow = page.evaluate(
            "document.documentElement.scrollWidth > document.documentElement.clientWidth"
        )
        assert not overflow, f"{name} UI has horizontal viewport overflow"
        assert not page_errors
        assert not console_errors
        context.close()
        browser.close()


def test_real_backend_mode_does_not_present_sample_performance_metrics() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        configure_real_backend(context)
        page = context.new_page()
        page.goto(
            os.environ["TARS_FRONTEND_URL"],
            wait_until="domcontentloaded",
            timeout=10_000,
        )
        page.get_by_role("button", name="Memory & Research").click()
        visible = page.locator("body").inner_text()
        match = FABRICATED_VISIBLE_METRIC.search(visible)
        assert match is None, f"real mode displayed a sample metric: {match.group(0)!r}"
        context.close()
        browser.close()
