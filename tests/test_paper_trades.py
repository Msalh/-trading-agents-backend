"""
Unit tests for app.paper_trades — the Tier 2.3 paper fill/P&L
lifecycle. No LLM, no network. Uses a temporary SQLite file, same
pattern as test_candidates.py.

Run with: pytest tests/test_paper_trades.py -v
"""

import importlib
import os
import tempfile

import pytest


@pytest.fixture
def fresh_paper_trades(monkeypatch):
    def _make(**env_overrides):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        monkeypatch.setenv("DB_PATH", tmp.name)
        for k, v in env_overrides.items():
            monkeypatch.setenv(k, v)

        import app.storage as storage
        importlib.reload(storage)
        storage.init_db()

        import app.paper_trades as paper_trades
        importlib.reload(paper_trades)

        return storage, paper_trades, tmp.name

    return _make


def _candidate(candidate_id, symbol, timeframe, direction, size, order_type,
               entry_price, stop_loss, targets, execution_status="planned",
               risk_decision="approve", ready_now=True):
    return {
        "candidate_id": candidate_id,
        "symbol": symbol,
        "timeframe": timeframe,
        "decision": {"direction": direction},
        "risk": {"decision": risk_decision, "suggested_size": size},
        "execution": {
            "status": execution_status,
            "order_type": order_type,
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "targets": targets,
            "ready_now": ready_now,
        },
    }


# ---------------------------------------------------------------------------
# open_trade_from_candidate
# ---------------------------------------------------------------------------

def test_market_order_opens_immediately(fresh_paper_trades):
    storage, pt, _ = fresh_paper_trades()
    c = _candidate("c1", "TEST", "5m", "bullish", 1, "market", 20020.0, 20000.0, [20100.0])
    trade = pt.open_trade_from_candidate(c)
    assert trade["status"] == "open"
    assert trade["fill_price"] == 20020.0
    assert trade["opened_at"] is not None


def test_limit_order_not_ready_starts_pending(fresh_paper_trades):
    storage, pt, _ = fresh_paper_trades()
    c = _candidate("c1", "TEST", "5m", "bullish", 1, "limit", 19950.0, 19930.0, [20050.0], ready_now=False)
    trade = pt.open_trade_from_candidate(c)
    assert trade["status"] == "pending_fill"
    assert trade["fill_price"] is None
    assert trade["opened_at"] is None


def test_limit_order_ready_now_opens_immediately(fresh_paper_trades):
    storage, pt, _ = fresh_paper_trades()
    c = _candidate("c1", "TEST", "5m", "bullish", 1, "limit", 20020.0, 20000.0, [20100.0], ready_now=True)
    trade = pt.open_trade_from_candidate(c)
    assert trade["status"] == "open"


def test_idempotent_on_same_candidate_id(fresh_paper_trades):
    storage, pt, _ = fresh_paper_trades()
    c = _candidate("c1", "TEST", "5m", "bullish", 1, "market", 20020.0, 20000.0, [20100.0])
    first = pt.open_trade_from_candidate(c)
    second = pt.open_trade_from_candidate(c)
    assert first["trade_id"] == second["trade_id"]
    assert pt.get_open_trade_count("TEST", "5m") == 1


def test_refuses_when_at_capacity(fresh_paper_trades):
    storage, pt, _ = fresh_paper_trades(MAX_OPEN_POSITIONS="1")
    c1 = _candidate("c1", "TEST", "5m", "bullish", 1, "market", 20020.0, 20000.0, [20100.0])
    c2 = _candidate("c2", "TEST", "5m", "bullish", 1, "market", 20030.0, 20010.0, [20110.0])
    pt.open_trade_from_candidate(c1)
    result = pt.open_trade_from_candidate(c2)
    assert result is None
    assert pt.get_open_trade_count("TEST", "5m") == 1


def test_capacity_allows_a_second_trade_for_a_different_symbol(fresh_paper_trades):
    storage, pt, _ = fresh_paper_trades(MAX_OPEN_POSITIONS="1")
    c1 = _candidate("c1", "TEST", "5m", "bullish", 1, "market", 20020.0, 20000.0, [20100.0])
    c2 = _candidate("c2", "OTHER", "5m", "bullish", 1, "market", 30.0, 28.0, [36.0])
    pt.open_trade_from_candidate(c1)
    result = pt.open_trade_from_candidate(c2)
    assert result is not None
    assert result["status"] == "open"


# ---------------------------------------------------------------------------
# process_new_bar — fills
# ---------------------------------------------------------------------------

def test_pending_long_fills_when_price_dips_to_limit(fresh_paper_trades):
    storage, pt, _ = fresh_paper_trades()
    c = _candidate("c1", "TEST", "5m", "bullish", 1, "limit", 19950.0, 19930.0, [20050.0], ready_now=False)
    pt.open_trade_from_candidate(c)

    # Bar doesn't reach the limit yet
    pt.process_new_bar("TEST", "5m", {"high": 20000.0, "low": 19980.0})
    still = pt.get_open_trade_count("TEST", "5m")
    assert still == 1
    trades = storage.get_open_or_pending_trades("TEST", "5m")
    assert trades[0]["status"] == "pending_fill"

    # Bar's low reaches the limit -> fills
    pt.process_new_bar("TEST", "5m", {"high": 19970.0, "low": 19945.0})
    trades = storage.get_open_or_pending_trades("TEST", "5m")
    assert trades[0]["status"] == "open"
    assert trades[0]["fill_price"] == 19950.0


def test_pending_short_fills_when_price_rallies_to_limit(fresh_paper_trades):
    storage, pt, _ = fresh_paper_trades()
    c = _candidate("c1", "TEST", "5m", "bearish", 1, "limit", 20050.0, 20070.0, [19950.0], ready_now=False)
    pt.open_trade_from_candidate(c)
    pt.process_new_bar("TEST", "5m", {"high": 20055.0, "low": 20010.0})
    trades = storage.get_open_or_pending_trades("TEST", "5m")
    assert trades[0]["status"] == "open"
    assert trades[0]["fill_price"] == 20050.0


# ---------------------------------------------------------------------------
# process_new_bar — stop/target closes and P&L
# ---------------------------------------------------------------------------

def test_long_closes_on_stop_hit_with_negative_pnl(fresh_paper_trades):
    storage, pt, _ = fresh_paper_trades()
    c = _candidate("c1", "TEST", "5m", "bullish", 2, "market", 20020.0, 20000.0, [20100.0])
    pt.open_trade_from_candidate(c)

    changed = pt.process_new_bar("TEST", "5m", {"high": 20025.0, "low": 19995.0})
    assert len(changed) == 1
    assert changed[0]["exit_reason"] == "stop_hit"
    # (20000 - 20020) * $2/pt * 2 contracts = -$80
    assert changed[0]["pnl_usd"] == -80.0
    assert pt.get_open_trade_count("TEST", "5m") == 0

    history = storage.get_recent_trades("TEST", "5m")
    assert history[0]["status"] == "closed"
    assert history[0]["pnl_usd"] == -80.0


def test_long_closes_on_target_hit_with_positive_pnl(fresh_paper_trades):
    storage, pt, _ = fresh_paper_trades()
    c = _candidate("c1", "TEST", "5m", "bullish", 1, "market", 20020.0, 20000.0, [20100.0])
    pt.open_trade_from_candidate(c)

    changed = pt.process_new_bar("TEST", "5m", {"high": 20105.0, "low": 20050.0})
    assert changed[0]["exit_reason"] == "target_hit"
    # (20100 - 20020) * $2/pt * 1 = $160
    assert changed[0]["pnl_usd"] == 160.0


def test_short_closes_on_target_hit_with_positive_pnl(fresh_paper_trades):
    storage, pt, _ = fresh_paper_trades()
    c = _candidate("c1", "TEST", "5m", "bearish", 1, "market", 20050.0, 20070.0, [19950.0])
    pt.open_trade_from_candidate(c)

    changed = pt.process_new_bar("TEST", "5m", {"high": 20040.0, "low": 19945.0})
    assert changed[0]["exit_reason"] == "target_hit"
    # (20050 - 19950) * $2/pt * 1 = $200
    assert changed[0]["pnl_usd"] == 200.0


def test_short_closes_on_stop_hit_with_negative_pnl(fresh_paper_trades):
    storage, pt, _ = fresh_paper_trades()
    c = _candidate("c1", "TEST", "5m", "bearish", 1, "market", 20050.0, 20070.0, [19950.0])
    pt.open_trade_from_candidate(c)

    changed = pt.process_new_bar("TEST", "5m", {"high": 20075.0, "low": 20055.0})
    assert changed[0]["exit_reason"] == "stop_hit"
    assert changed[0]["pnl_usd"] == -40.0


def test_gap_bar_hitting_both_stop_and_target_assumes_stop_first(fresh_paper_trades):
    """Conservative assumption documented in paper_trades.py: with
    only OHLC (no tick data), a bar that spans both the stop and the
    target is treated as the stop having been hit first."""
    storage, pt, _ = fresh_paper_trades()
    c = _candidate("c1", "TEST", "5m", "bullish", 1, "market", 20020.0, 20000.0, [20100.0])
    pt.open_trade_from_candidate(c)

    changed = pt.process_new_bar("TEST", "5m", {"high": 20150.0, "low": 19990.0})
    assert changed[0]["exit_reason"] == "stop_hit"


def test_fill_and_immediate_stop_in_the_same_bar(fresh_paper_trades):
    """A limit fills and then reverses to stop within the SAME bar --
    both transitions should be reflected in one process_new_bar call."""
    storage, pt, _ = fresh_paper_trades()
    c = _candidate("c1", "TEST", "5m", "bullish", 1, "limit", 19950.0, 19930.0, [20050.0], ready_now=False)
    pt.open_trade_from_candidate(c)

    changed = pt.process_new_bar("TEST", "5m", {"high": 19960.0, "low": 19925.0})
    assert len(changed) == 2
    assert changed[0]["status"] == "open"  # the fill
    assert changed[1]["status"] == "closed"
    assert changed[1]["exit_reason"] == "stop_hit"


def test_untouched_open_trade_stays_open(fresh_paper_trades):
    storage, pt, _ = fresh_paper_trades()
    c = _candidate("c1", "TEST", "5m", "bullish", 1, "market", 20020.0, 20000.0, [20100.0])
    pt.open_trade_from_candidate(c)

    changed = pt.process_new_bar("TEST", "5m", {"high": 20030.0, "low": 20015.0})
    assert changed == []
    assert pt.get_open_trade_count("TEST", "5m") == 1


def test_closed_trade_untouched_by_further_bars(fresh_paper_trades):
    """A duplicate/retried bar delivery must never double-close (and
    double-count P&L for) a trade that's already closed."""
    storage, pt, _ = fresh_paper_trades()
    c = _candidate("c1", "TEST", "5m", "bullish", 1, "market", 20020.0, 20000.0, [20100.0])
    pt.open_trade_from_candidate(c)
    pt.process_new_bar("TEST", "5m", {"high": 20025.0, "low": 19995.0})  # stop hit, closes

    changed_again = pt.process_new_bar("TEST", "5m", {"high": 20200.0, "low": 20190.0})
    assert changed_again == []  # nothing left open/pending to process
