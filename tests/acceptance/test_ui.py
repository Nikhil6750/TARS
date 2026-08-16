from __future__ import annotations

import os

import pytest


pytestmark = pytest.mark.acceptance


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
        page = browser.new_page(viewport=viewport)
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
        browser.close()
