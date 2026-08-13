"""
Unit tests for app.account_risk — Tier 2.10 (account-level risk
controls). Pure functions over trade-dict lists, no DB required for
most tests (trades passed in directly); a couple of tests exercise the
live storage-backed path with a temp DB, same pattern as other test
files in this project.

Run with: pytest tests/test_account_risk.py -v
"""

import importlib
import os
import tempfile

import pytest

from app.account_risk import (
    compute_current_drawdown_used,
    compute_daily_loss_used,
    compute_realized_pnl_today,
)


def _trade(pnl_usd, closed_at):
    return {"pnl_usd": pnl_usd, "closed_at": closed_at}


# ---------------------------------------------------------------------------
# compute_current_drawdown_used
# ---------------------------------------------------------------------------

def test_drawdown_zero_when_no_trades():
    assert compute_current_drawdown_used(trades=[]) == 0.0


def test_drawdown_zero_when_net_positive_and_monotonic():
    trades = [_trade(100.0, "2026-08-11T14:00:00Z"), _trade(50.0, "2026-08-11T15:00:00Z")]
    assert compute_current_drawdown_used(trades) == 0.0


def test_drawdown_peak_to_trough_not_just_net_loss():
    """The core reason this is peak-to-trough and not "sum of losses":
    up $500, then down $200 from that peak, net is still +$300 overall
    -- but there IS $200 of real drawdown from the high-water mark,
    which a naive "are we net negative" check would miss entirely."""
    trades = [
        _trade(500.0, "2026-08-11T14:00:00Z"),   # cumulative 500, peak 500
        _trade(-200.0, "2026-08-11T15:00:00Z"),  # cumulative 300, drawdown 200
    ]
    assert compute_current_drawdown_used(trades) == 200.0


def test_drawdown_uses_current_trough_not_deepest_historical_one():
    """A full recovery after a deep drawdown resets it back toward
    zero -- drawdown is relative to distance from peak RIGHT NOW, not
    "worst it's ever been"."""
    trades = [
        _trade(100.0, "2026-08-11T14:00:00Z"),   # cumulative 100, peak 100
        _trade(-400.0, "2026-08-11T15:00:00Z"),  # cumulative -300, drawdown 400
        _trade(450.0, "2026-08-11T16:00:00Z"),   # cumulative 150, new peak 150, drawdown 0
    ]
    assert compute_current_drawdown_used(trades) == 0.0


def test_drawdown_order_matters_uses_closed_at_chronological_order():
    """Trades passed in are trusted to already be chronological
    (get_all_closed_trades_chronological orders by closed_at ASC) --
    confirms the function walks them in the given order, not by
    re-sorting or by insertion order into some other structure."""
    ordered = [_trade(-300.0, "2026-08-11T14:00:00Z"), _trade(100.0, "2026-08-11T15:00:00Z")]
    # cumulative: -300 (peak stays 0, drawdown 300) -> -200 (peak still 0, drawdown 200)
    assert compute_current_drawdown_used(ordered) == 200.0


def test_drawdown_missing_pnl_usd_treated_as_zero():
    trades = [{"closed_at": "2026-08-11T14:00:00Z"}]  # no pnl_usd key at all
    assert compute_current_drawdown_used(trades) == 0.0


# ---------------------------------------------------------------------------
# compute_realized_pnl_today / compute_daily_loss_used
# ---------------------------------------------------------------------------

def test_realized_pnl_today_sums_only_matching_trading_day():
    trades = [
        _trade(-100.0, "2026-08-11T14:00:00Z"),  # same NY trading day as as_of
        _trade(50.0, "2026-08-11T15:00:00Z"),     # same day
        _trade(-999.0, "2026-08-10T14:00:00Z"),   # a different day -- must be excluded
    ]
    result = compute_realized_pnl_today("2026-08-11T18:00:00Z", trades=trades)
    assert result == -50.0


def test_realized_pnl_today_respects_session_rollover_convention():
    """A trade closed at 19:30 NY time (23:30Z in EDT) belongs to the
    NEXT trading day under the CME/Globex convention (Tier 2.9) --
    reused here, not reinvented."""
    trades = [_trade(-200.0, "2026-08-11T23:30:00Z")]  # rolls to 2026-08-12
    assert compute_realized_pnl_today("2026-08-11T14:00:00Z", trades=trades) == 0.0
    assert compute_realized_pnl_today("2026-08-12T14:00:00Z", trades=trades) == -200.0


def test_realized_pnl_today_ignores_trades_with_no_closed_at():
    trades = [{"pnl_usd": -500.0, "closed_at": None}]
    assert compute_realized_pnl_today("2026-08-11T14:00:00Z", trades=trades) == 0.0


def test_daily_loss_used_is_nonnegative_even_on_a_winning_day():
    trades = [_trade(300.0, "2026-08-11T14:00:00Z")]
    assert compute_daily_loss_used("2026-08-11T18:00:00Z", trades=trades) == 0.0


def test_daily_loss_used_matches_negated_realized_pnl_on_a_losing_day():
    trades = [_trade(-150.0, "2026-08-11T14:00:00Z"), _trade(-50.0, "2026-08-11T15:00:00Z")]
    assert compute_daily_loss_used("2026-08-11T18:00:00Z", trades=trades) == 200.0


# ---------------------------------------------------------------------------
# Live storage-backed path (account-wide, across symbols)
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_storage(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setenv("DB_PATH", tmp.name)

    import app.storage as storage
    importlib.reload(storage)
    storage.init_db()

    import app.account_risk as account_risk
    importlib.reload(account_risk)

    yield storage, account_risk

    os.unlink(tmp.name)


def _save_closed_trade(storage, trade_id, symbol, pnl_usd, closed_at):
    trade = {
        "trade_id": trade_id, "candidate_id": f"cand-{trade_id}", "symbol": symbol,
        "timeframe": "5m", "direction": "bullish", "size": 1, "order_type": "market",
        "entry_price": 20000.0, "stop_loss": 19980.0, "targets": [20050.0],
        "status": "open", "opened_at": closed_at, "fill_price": 20000.0,
    }
    storage.save_paper_trade(trade)
    storage.close_trade(trade_id, exit_price=20000.0 + pnl_usd, exit_reason="target_hit",
                         pnl_usd=pnl_usd, closed_at=closed_at)


def test_live_drawdown_spans_all_symbols_not_just_one(fresh_storage):
    """Account-level risk is account-wide -- a closed trade on a
    DIFFERENT symbol must still count toward the same drawdown figure,
    unlike get_recent_trades() which is scoped per symbol/timeframe."""
    storage, account_risk = fresh_storage
    _save_closed_trade(storage, "t1", "MNQ1!", 100.0, "2026-08-11T14:00:00Z")
    _save_closed_trade(storage, "t2", "ES1!", -300.0, "2026-08-11T15:00:00Z")

    # cumulative: +100 (peak 100) -> -200 (peak still 100) -> drawdown 300
    assert account_risk.compute_current_drawdown_used() == 300.0
