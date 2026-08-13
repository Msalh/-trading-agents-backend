"""
Focused endpoint tests for app.main — the Tier 2.9 (calendar
integrity) calendar_warning field on the webhook response, and the
Tier 2.10 (account-level risk controls) GET /account/risk endpoint.
Not a full endpoint test suite (that's covered piecemeal by the
per-module test files plus this project's ad-hoc smoke tests); this
file exists specifically for behavior wired at the main.py handler
level rather than inside a module that already has its own test file.

Run with: pytest tests/test_main.py -v
"""

import importlib
import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setenv("DB_PATH", tmp.name)
    monkeypatch.setenv("WEBHOOK_SECRET", "test-secret")

    import app.storage as storage
    importlib.reload(storage)
    storage.init_db()

    for mod_name in (
        "app.timing_agent",
        "app.trading_calendar",
        "app.coordinator",
        "app.candidates",
        "app.outcomes",
        "app.replay",
        "app.paper_trades",
        "app.risk_agent",
        "app.account_risk",
        "app.main",
    ):
        importlib.reload(importlib.import_module(mod_name))

    import app.main as main
    yield TestClient(main.app)

    os.unlink(tmp.name)


def _payload(timestamp, trading_date, event_id="evt-1"):
    return {
        "schema_version": "1.0",
        "event_id": event_id,
        "symbol": "MNQ1!",
        "source": "pine",
        "timeframe": "5m",
        "timestamp": timestamp,
        "bar_status": "closed",
        "event_type": "bar_closed",
        "secret": "test-secret",
        "open": 20000.0, "high": 20010.0, "low": 19995.0, "close": 20005.0,
        "session_name": "RTH", "is_rth": True, "trading_date": trading_date,
    }


def test_webhook_calendar_warning_none_when_consistent(client):
    r = client.post("/webhook/tradingview", json=_payload("2026-08-11T14:00:00Z", "2026-08-11"))
    assert r.status_code == 200
    assert r.json()["calendar_warning"] is None


def test_webhook_calendar_warning_set_on_mismatch(client):
    """19:30 NY time (23:30Z in EDT) belongs to the NEXT trading day
    under the CME/Globex rollover convention -- a payload claiming the
    SAME day is a data-integrity mismatch."""
    r = client.post(
        "/webhook/tradingview",
        json=_payload("2026-08-11T23:30:00Z", "2026-08-11", event_id="evt-2"),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["calendar_warning"] is not None
    assert "2026-08-11" in body["calendar_warning"]
    assert "2026-08-12" in body["calendar_warning"]
    # the bar is still stored despite the warning -- flagged, not rejected
    assert body["status"] == "stored"


def test_webhook_still_stores_bar_despite_calendar_mismatch(client):
    r = client.post(
        "/webhook/tradingview",
        json=_payload("2026-08-11T23:30:00Z", "2026-08-11", event_id="evt-3"),
    )
    assert r.status_code == 200
    import app.storage as storage
    bar = storage.get_latest(symbol="MNQ1!", timeframe="5m")
    assert bar is not None
    assert bar["event_id"] == "evt-3"


# ---------------------------------------------------------------------------
# Tier 2.10: GET /account/risk
# ---------------------------------------------------------------------------

def test_account_risk_no_trades_yet(client):
    r = client.get("/account/risk")
    assert r.status_code == 200
    body = r.json()
    assert body["current_drawdown_used"] == 0.0
    assert body["daily_loss_used"] == 0.0
    assert body["remaining_drawdown_room"] == body["max_drawdown"]
    assert body["remaining_daily_loss_room"] == body["daily_loss_limit"]
    assert body["closed_trades_considered"] == 0


def test_account_risk_no_secret_required(client):
    """Read-only, same pattern as /trades/* and /candidates/* — must
    NOT 401 without X-Webhook-Secret."""
    r = client.get("/account/risk")
    assert r.status_code == 200


def test_account_risk_reflects_a_closed_losing_trade(client):
    import app.storage as storage
    trade = {
        "trade_id": "t1", "candidate_id": "c1", "symbol": "MNQ1!", "timeframe": "5m",
        "direction": "bullish", "size": 1, "order_type": "market",
        "entry_price": 20000.0, "stop_loss": 19980.0, "targets": [20050.0],
        "status": "open", "opened_at": "2026-08-11T14:00:00Z", "fill_price": 20000.0,
    }
    storage.save_paper_trade(trade)
    storage.close_trade(
        "t1", exit_price=19980.0, exit_reason="stop_hit", pnl_usd=-40.0,
        closed_at="2026-08-11T14:30:00Z",
    )

    r = client.get("/account/risk")
    body = r.json()
    assert body["current_drawdown_used"] == 40.0
    assert body["closed_trades_considered"] == 1
