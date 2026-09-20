from __future__ import annotations


def test_capabilities_endpoint_reports_description_and_health(client):
    resp = client.get("/api/v1/actions/capabilities")
    assert resp.status_code == 200
    skills = {s["name"]: s for s in resp.json()["skills"]}

    assert set(skills) >= {
        "windows_app",
        "filesystem",
        "browser",
        "terminal",
        "desktop_control",
        "trading",
    }
    for entry in skills.values():
        assert "description" in entry
        assert "health" in entry
        assert "available" in entry["health"]

    # The app always constructs a FrontendCommandBridge at startup (it's
    # what carries capture/DOM commands to a *connected* client), so
    # "wired in" is true here even with no client connected -- that's a
    # separate, runtime connection-count fact, not something health()
    # reports. What health() *does* honestly distinguish is whether each
    # skill's optional MemoryService/ChartAnalysisService/TradingContext
    # dependency was constructed at all.
    assert skills["browser"]["health"]["available"] is True
    assert skills["windows_app"]["health"]["capture_actions_available"] is True
    assert skills["trading"]["health"]["capture_available"] is True
    assert skills["trading"]["health"]["memory_available"] is True
    assert skills["trading"]["health"]["chart_analysis_available"] is True
    assert skills["trading"]["health"]["trading_context_available"] is True
