"""
Focused endpoint tests for app.main — currently just the Tier 2.9
(calendar integrity) calendar_warning field on the webhook response.
Not a full endpoint test suite (that's covered piecemeal by the
per-module test files plus this project's ad-hoc smoke tests); this
file exists specifically because calendar_warning is wired at the
webhook-handler level, not inside a module that already has its own
test file.

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
