from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import pytest

from tools.tars_test_client import TarsTestClient

pytestmark = pytest.mark.acceptance


def test_actual_frontend_client_consumes_actual_backend_protocol(
    client: TarsTestClient,
    event_factory: Callable[[str, str, str], dict[str, Any]],
) -> None:
    """Exercise the shipped TS client against the live backend, not a copied parser."""

    playwright = pytest.importorskip("playwright.sync_api")
    symbol = f"WSC{uuid4().hex[:8].upper()}"
    snapshot = event_factory("SETUP_VALID", "VALID", symbol)
    developing = event_factory("SETUP_DEVELOPING", "PENDING", symbol)
    valid = event_factory("SETUP_VALID", "VALID", symbol)
    client.send_event(snapshot)

    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        page.goto(os.environ["TARS_FRONTEND_URL"], wait_until="domcontentloaded")
        page.evaluate(
            """
            async ({ wsUrl }) => {
              const { TARSWebSocketClient } = await import('/src/services/websocket.ts');
              const observed = {
                events: [],
                statuses: [],
                protocolErrors: [],
                latencies: [],
              };
              const socket = new TARSWebSocketClient(wsUrl);
              socket.onTradingEvent((event) => observed.events.push(event));
              socket.onConnectionChange((status, latency, error) => {
                observed.statuses.push({ status, error: error || null });
                if (latency > 0) observed.latencies.push(latency);
              });
              socket.onProtocolError((title, details) => {
                observed.protocolErrors.push({ title, details });
              });
              window.__tarsCertification = { socket, observed };
              socket.connect();
            }
            """,
            {"wsUrl": client.websocket_url},
        )

        try:
            page.wait_for_function(
                "window.__tarsCertification.observed.statuses.some(x => x.status === 'connected')",
                timeout=5_000,
            )
            page.wait_for_function(
                """id => window.__tarsCertification.observed.events
                    .some(event => event.event_id === id)""",
                arg=snapshot["event_id"],
                timeout=5_000,
            )

            client.send_event(developing)
            client.send_event(valid)
            page.wait_for_function(
                """ids => ids.every(id => window.__tarsCertification.observed.events
                    .some(event => event.event_id === id))""",
                arg=[developing["event_id"], valid["event_id"]],
                timeout=5_000,
            )

            invalidated = client.invalidate(valid["event_id"], "WS_COMPAT")
            page.wait_for_function(
                """id => window.__tarsCertification.observed.events
                    .some(event => event.event_id === id && event.state === 'SETUP_INVALIDATED')""",
                arg=invalidated["event_id"],
                timeout=5_000,
            )

            # The actual client emits a heartbeat every ten seconds. A positive
            # measured latency proves that the backend pong shape was consumed.
            page.wait_for_function(
                "window.__tarsCertification.observed.latencies.length > 0",
                timeout=12_000,
            )

            context.set_offline(True)
            page.wait_for_function(
                """window.__tarsCertification.observed.statuses
                    .some(x => x.status === 'offline' || x.status === 'reconnecting')""",
                timeout=5_000,
            )
            context.set_offline(False)
            page.wait_for_function(
                """window.__tarsCertification.observed.statuses
                    .filter(x => x.status === 'connected').length >= 2""",
                timeout=8_000,
            )

            result = page.evaluate("window.__tarsCertification.observed")
            assert not result["protocolErrors"]
        finally:
            page.evaluate("window.__tarsCertification?.socket.disconnect()")
            context.close()
            browser.close()
