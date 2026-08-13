"""
Unit tests for app.outcomes — both the original Sprint 14
hypothetical horizon estimate (previously untested) and the Tier 2.4
rebuild that prefers a real closed paper trade's actual P&L when one
exists. No LLM, no network. Uses a temporary SQLite file, same
pattern as test_candidates.py / test_paper_trades.py.

Run with: pytest tests/test_outcomes.py -v
"""

import importlib
import tempfile
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def fresh_env(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setenv("DB_PATH", tmp.name)

    import app.storage as storage
    importlib.reload(storage)
    storage.init_db()

    import app.paper_trades as paper_trades
    importlib.reload(paper_trades)

    import app.outcomes as outcomes
    importlib.reload(outcomes)

    yield storage, paper_trades, outcomes


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _save_bar(storage, symbol, timeframe, timestamp_dt, close):
    """Minimal fake market_state row — bypasses the Pydantic model,
    writes directly via the same table save_event uses, since the
    tests only need timestamp/close for the horizon lookups."""
    import json
    conn = storage.get_connection()
    payload = {
        "event_id": f"{symbol}:{timeframe}:{_iso(timestamp_dt)}",
        "symbol": symbol,
        "timeframe": timeframe,
        "timestamp": _iso(timestamp_dt),
        "close": close,
        "high": close, "low": close, "open": close,
    }
    conn.execute(
        "INSERT INTO market_state (event_id, symbol, timeframe, timestamp, payload_json) VALUES (?, ?, ?, ?, ?)",
        (payload["event_id"], symbol, timeframe, payload["timestamp"], json.dumps(payload)),
    )
    conn.commit()
    conn.close()


def _candidate(candidate_id, symbol, timeframe, decision, timestamp_dt):
    return {
        "candidate_id": candidate_id,
        "symbol": symbol,
        "timeframe": timeframe,
        "decision": {"decision": decision, "timestamp": _iso(timestamp_dt)},
    }


def _trade(candidate_id, symbol, timeframe, direction, size, entry, stop, targets):
    return {
        "trade_id": f"trade-{candidate_id}",
        "candidate_id": candidate_id,
        "symbol": symbol,
        "timeframe": timeframe,
        "direction": direction,
        "size": size,
        "order_type": "market",
        "entry_price": entry,
        "stop_loss": stop,
        "targets": targets,
        "status": "open",
        "opened_at": _iso(datetime.now(timezone.utc)),
        "fill_price": entry,
    }


# ---------------------------------------------------------------------------
# compute_outcome_at_horizon / compute_outcomes_for_decision (Sprint 14, previously untested)
# ---------------------------------------------------------------------------

def test_horizon_no_data_when_no_bar_at_decision_time(fresh_env):
    storage, pt, outcomes = fresh_env
    decision_time = datetime.now(timezone.utc) - timedelta(hours=2)
    result = outcomes.compute_outcome_at_horizon("TEST", "5m", _iso(decision_time), "enter_long", 15)
    assert result.outcome == "no_data"


def test_horizon_pending_when_not_enough_time_has_passed(fresh_env):
    storage, pt, outcomes = fresh_env
    decision_time = datetime.now(timezone.utc) - timedelta(minutes=1)
    _save_bar(storage, "TEST", "5m", decision_time, 100.0)
    result = outcomes.compute_outcome_at_horizon("TEST", "5m", _iso(decision_time), "enter_long", 60)
    assert result.outcome == "pending"


def test_horizon_correct_for_long_when_price_rose(fresh_env):
    storage, pt, outcomes = fresh_env
    decision_time = datetime.now(timezone.utc) - timedelta(minutes=30)
    horizon_time = decision_time + timedelta(minutes=15)
    _save_bar(storage, "TEST", "5m", decision_time, 100.0)
    _save_bar(storage, "TEST", "5m", horizon_time, 105.0)
    result = outcomes.compute_outcome_at_horizon("TEST", "5m", _iso(decision_time), "enter_long", 15)
    assert result.outcome == "correct"
    assert result.price_change == 5.0


def test_horizon_incorrect_for_short_when_price_rose(fresh_env):
    storage, pt, outcomes = fresh_env
    decision_time = datetime.now(timezone.utc) - timedelta(minutes=30)
    horizon_time = decision_time + timedelta(minutes=15)
    _save_bar(storage, "TEST", "5m", decision_time, 100.0)
    _save_bar(storage, "TEST", "5m", horizon_time, 105.0)
    result = outcomes.compute_outcome_at_horizon("TEST", "5m", _iso(decision_time), "enter_short", 15)
    assert result.outcome == "incorrect"


def test_horizon_flat_when_price_unchanged(fresh_env):
    storage, pt, outcomes = fresh_env
    decision_time = datetime.now(timezone.utc) - timedelta(minutes=30)
    horizon_time = decision_time + timedelta(minutes=15)
    _save_bar(storage, "TEST", "5m", decision_time, 100.0)
    _save_bar(storage, "TEST", "5m", horizon_time, 100.0)
    result = outcomes.compute_outcome_at_horizon("TEST", "5m", _iso(decision_time), "enter_long", 15)
    assert result.outcome == "flat"


def test_outcomes_for_decision_none_for_no_trade(fresh_env):
    storage, pt, outcomes = fresh_env
    assert outcomes.compute_outcomes_for_decision("TEST", "5m", {"decision": "no_trade", "timestamp": _iso(datetime.now(timezone.utc))}) is None


# ---------------------------------------------------------------------------
# compute_outcome_for_candidate (Tier 2.4 rebuild)
# ---------------------------------------------------------------------------

def test_candidate_outcome_none_for_no_trade(fresh_env):
    storage, pt, outcomes = fresh_env
    c = _candidate("c1", "TEST", "5m", "no_trade", datetime.now(timezone.utc))
    assert outcomes.compute_outcome_for_candidate(c) is None


def test_candidate_outcome_real_win_from_closed_trade(fresh_env):
    storage, pt, outcomes = fresh_env
    decision_time = datetime.now(timezone.utc) - timedelta(minutes=10)
    c = _candidate("c1", "TEST", "5m", "enter_long", decision_time)
    trade = _trade("c1", "TEST", "5m", "bullish", 1, 20020.0, 20000.0, [20100.0])
    storage.save_paper_trade(trade)
    storage.close_trade(trade["trade_id"], exit_price=20100.0, exit_reason="target_hit", pnl_usd=160.0, closed_at=_iso(datetime.now(timezone.utc)))

    result = outcomes.compute_outcome_for_candidate(c)
    assert result["source"] == "actual_trade"
    assert result["status"] == "closed"
    assert result["outcome"] == "win"
    assert result["pnl_usd"] == 160.0
    assert result["exit_reason"] == "target_hit"


def test_candidate_outcome_real_loss_from_closed_trade(fresh_env):
    storage, pt, outcomes = fresh_env
    c = _candidate("c1", "TEST", "5m", "enter_long", datetime.now(timezone.utc))
    trade = _trade("c1", "TEST", "5m", "bullish", 1, 20020.0, 20000.0, [20100.0])
    storage.save_paper_trade(trade)
    storage.close_trade(trade["trade_id"], exit_price=20000.0, exit_reason="stop_hit", pnl_usd=-40.0, closed_at=_iso(datetime.now(timezone.utc)))

    result = outcomes.compute_outcome_for_candidate(c)
    assert result["outcome"] == "loss"
    assert result["pnl_usd"] == -40.0


def test_candidate_outcome_cancelled_for_expired_unfilled_trade(fresh_env):
    """Tier 3.2: an order that expired before it ever filled
    (paper_trades.ORDER_EXPIRY_MINUTES) has a real trade row, but no
    position was ever taken -- must be reported distinctly from both
    a real win/loss/breakeven (nothing was filled to realize a P&L
    against) and "pending" (this IS resolved, just resolved as
    "never happened")."""
    storage, pt, outcomes = fresh_env
    c = _candidate("c1", "TEST", "5m", "enter_long", datetime.now(timezone.utc))
    trade = _trade("c1", "TEST", "5m", "bullish", 1, 20020.0, 20000.0, [20100.0])
    trade["status"] = "pending_fill"
    storage.save_paper_trade(trade)
    storage.cancel_trade(trade["trade_id"], cancelled_at=_iso(datetime.now(timezone.utc)), reason="expired_unfilled")

    result = outcomes.compute_outcome_for_candidate(c)
    assert result["source"] == "actual_trade"
    assert result["status"] == "cancelled"
    assert result["exit_reason"] == "expired_unfilled"
    assert result["outcome"] == "cancelled"


def test_candidate_outcome_pending_for_still_open_trade(fresh_env):
    storage, pt, outcomes = fresh_env
    c = _candidate("c1", "TEST", "5m", "enter_long", datetime.now(timezone.utc))
    trade = _trade("c1", "TEST", "5m", "bullish", 1, 20020.0, 20000.0, [20100.0])
    storage.save_paper_trade(trade)  # still "open", never closed

    result = outcomes.compute_outcome_for_candidate(c)
    assert result["source"] == "actual_trade"
    assert result["status"] == "open"
    assert result["outcome"] == "pending"


def test_candidate_outcome_falls_back_to_hypothetical_when_no_trade_exists(fresh_env):
    """The core Tier 2.4 behavior: a candidate that never became a
    trade (e.g. rejected by Risk, or never manually run) still gets
    an outcome estimate via the old horizon logic, clearly labeled."""
    storage, pt, outcomes = fresh_env
    decision_time = datetime.now(timezone.utc) - timedelta(minutes=30)
    horizon_time = decision_time + timedelta(minutes=15)
    _save_bar(storage, "TEST", "5m", decision_time, 100.0)
    _save_bar(storage, "TEST", "5m", horizon_time, 105.0)
    c = _candidate("c1", "TEST", "5m", "enter_long", decision_time)

    result = outcomes.compute_outcome_for_candidate(c, horizons=[15])
    assert result["source"] == "hypothetical"
    assert result["status"] == "no_trade_opened"
    assert result["horizons"][15]["outcome"] == "correct"


# ---------------------------------------------------------------------------
# summarize_outcomes
# ---------------------------------------------------------------------------

def test_summarize_computes_real_win_rate_and_total_pnl(fresh_env):
    storage, pt, outcomes = fresh_env
    win = {"source": "actual_trade", "status": "closed", "outcome": "win", "pnl_usd": 100.0}
    loss = {"source": "actual_trade", "status": "closed", "outcome": "loss", "pnl_usd": -40.0}
    pending = {"source": "actual_trade", "status": "open", "outcome": "pending"}

    summary = outcomes.summarize_outcomes([win, loss, pending, None])
    real = summary["real_trades"]
    assert real["closed_trades"] == 2
    assert real["wins"] == 1
    assert real["losses"] == 1
    assert real["total_pnl_usd"] == 60.0
    assert real["win_rate"] == 0.5
    assert real["still_open_or_pending"] == 1


def test_summarize_buckets_cancelled_separately_from_pending_and_closed(fresh_env):
    """Tier 3.2: a cancelled/expired order must land in its own
    cancelled_unfilled bucket -- neither closed_trades (no P&L was
    ever realized) nor still_open_or_pending (it's fully resolved,
    just resolved as "never happened")."""
    storage, pt, outcomes = fresh_env
    win = {"source": "actual_trade", "status": "closed", "outcome": "win", "pnl_usd": 100.0}
    cancelled = {"source": "actual_trade", "status": "cancelled", "outcome": "cancelled", "exit_reason": "expired_unfilled"}
    pending = {"source": "actual_trade", "status": "open", "outcome": "pending"}

    summary = outcomes.summarize_outcomes([win, cancelled, pending])
    real = summary["real_trades"]
    assert real["closed_trades"] == 1
    assert real["still_open_or_pending"] == 1
    assert real["cancelled_unfilled"] == 1


def test_summarize_computes_hypothetical_accuracy_per_horizon(fresh_env):
    storage, pt, outcomes = fresh_env
    h1 = {"source": "hypothetical", "status": "no_trade_opened", "horizons": {15: {"outcome": "correct"}, 30: {"outcome": "pending"}}}
    h2 = {"source": "hypothetical", "status": "no_trade_opened", "horizons": {15: {"outcome": "incorrect"}, 30: {"outcome": "correct"}}}

    summary = outcomes.summarize_outcomes([h1, h2])
    hyp = summary["hypothetical_never_traded"]
    assert hyp["candidates"] == 2
    assert hyp["by_horizon_minutes"][15]["correct"] == 1
    assert hyp["by_horizon_minutes"][15]["incorrect"] == 1
    assert hyp["by_horizon_minutes"][15]["accuracy"] == 0.5
    # horizon 30: one "pending" (h1) + one "correct" (h2) -> 1 resolved, 1 correct -> accuracy 1.0
    assert hyp["by_horizon_minutes"][30]["accuracy"] == 1.0
    assert hyp["by_horizon_minutes"][30]["pending"] == 1
