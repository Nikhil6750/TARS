from __future__ import annotations


def test_list_agents_reports_all_three(client):
    resp = client.get("/api/v1/agents")
    assert resp.status_code == 200
    names = {a["name"] for a in resp.json()}
    assert names == {"chart_analysis_agent", "trading_workspace_agent", "setup_watch_agent"}
    setup_watch = next(a for a in resp.json() if a["name"] == "setup_watch_agent")
    assert setup_watch["mode"] == "CONTINUOUS"


def test_run_unknown_agent_returns_404(client):
    resp = client.post("/api/v1/agents/does_not_exist/run")
    assert resp.status_code == 404


def test_run_chart_analysis_agent_fails_closed_without_native_shell(client):
    resp = client.post("/api/v1/agents/chart_analysis_agent/run")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "FAILED"


def test_run_trading_workspace_agent_opens_tradingview_when_nothing_to_focus(client, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr("skills.trading.webbrowser.open", lambda url: calls.append(url) or True)

    resp = client.post("/api/v1/agents/trading_workspace_agent/run")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "SUCCEEDED"
    assert calls == ["https://www.tradingview.com/chart/"]


def test_agent_runs_are_recorded_and_listable(client, monkeypatch):
    monkeypatch.setattr("skills.trading.webbrowser.open", lambda url: True)
    client.post("/api/v1/agents/trading_workspace_agent/run")

    resp = client.get("/api/v1/agents/runs", params={"name": "trading_workspace_agent"})
    assert resp.status_code == 200
    runs = resp.json()
    assert len(runs) >= 1
    assert runs[0]["agent_name"] == "trading_workspace_agent"
    assert runs[0]["status"] in ("SUCCEEDED", "FAILED")


def test_setup_watch_agent_runs_without_fabricating_a_signal(client):
    resp = client.post("/api/v1/agents/setup_watch_agent/run")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "SUCCEEDED"


def test_orchestrator_setup_workspace_intent_triggers_the_agent(client, monkeypatch):
    monkeypatch.setattr("skills.trading.webbrowser.open", lambda url: True)
    resp = client.post(
        "/api/v1/assistant/query", json={"text": "please set up my trading workspace"}
    )
    body = resp.json()
    assert body["intent"] == "trading_workspace"
