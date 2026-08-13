"""
Unit tests for app.paper_trades — the Tier 2.3 paper fill/P&L
lifecycle, substantially reworked in Tier 3.2 (fill realism: event
time, no more ready_now auto-fill, order expiry, slippage/commission,
gap-through-stop). No LLM, no network. Uses a temporary SQLite file,
same pattern as test_candidates.py.

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
        # Deterministic defaults for tests that don't care about the
        # exact slippage/commission numbers — overridden per-test where
        # the test IS specifically about them.
        monkeypatch.setenv("SLIPPAGE_POINTS", env_overrides.pop("SLIPPAGE_POINTS", "0.25"))
        monkeypatch.setenv("COMMISSION_PER_CONTRACT", env_overrides.pop("COMMISSION_PER_CONTRACT", "2.0"))
        monkeypatch.setenv("ORDER_EXPIRY_MINUTES", env_overrides.pop("ORDER_EXPIRY_MINUTES", "60"))
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
               risk_decision="approve", ready_now=True, bar_timestamp="2026-08-13T10:00:00Z"):
    return {
        "candidate_id": candidate_id,
        "symbol": symbol,
        "timeframe": timeframe,
        "bar": {"timestamp": bar_timestamp},  # Tier 3.1 anchor bar -- Tier 3.2 reads its timestamp as order_submitted_at
        "decision": {"direction": direction},
        "risk": {"decision": risk_decision, "suggested_size": size},
        "execution": {
            "status": execution_status,
            "order_type": order_type,
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "targets": targets,
            "ready_now": ready_now,  # Tier 3.2: no longer read/acted on by this module -- kept for realistic shape
        },
    }


def _bar(timestamp, open_, high, low):
    return {"timestamp": timestamp, "open": open_, "high": high, "low": low}


# ---------------------------------------------------------------------------
# open_trade_from_candidate — Tier 3.2: ALWAYS starts pending_fill now
# ---------------------------------------------------------------------------

def test_market_order_starts_pending_not_open(fresh_paper_trades):
    """The core Tier 3.2 behavior change: even a market order no
    longer fills instantly at candidate-creation time (the anchor bar
    has already closed -- filling "into" it would be lookahead bias).
    It waits for process_new_bar() to fill it against a REAL
    subsequent bar."""
    storage, pt, _ = fresh_paper_trades()
    c = _candidate("c1", "TEST", "5m", "bullish", 1, "market", 20020.0, 20000.0, [20100.0])
    trade = pt.open_trade_from_candidate(c)
    assert trade["status"] == "pending_fill"
    assert trade["fill_price"] is None
    assert trade["opened_at"] is None
    assert trade["order_submitted_at"] == "2026-08-13T10:00:00Z"


def test_ready_now_no_longer_causes_immediate_fill(fresh_paper_trades):
    """Regression test for the actual second-review finding: Execution
    setting ready_now=True on a limit order must NOT be trusted as
    proof the market actually traded there. Before Tier 3.2 this
    opened the trade immediately at the proposed price; now it starts
    pending_fill exactly like ready_now=False."""
    storage, pt, _ = fresh_paper_trades()
    c = _candidate("c1", "TEST", "5m", "bullish", 1, "limit", 20020.0, 20000.0, [20100.0], ready_now=True)
    trade = pt.open_trade_from_candidate(c)
    assert trade["status"] == "pending_fill"


def test_limit_order_starts_pending(fresh_paper_trades):
    storage, pt, _ = fresh_paper_trades()
    c = _candidate("c1", "TEST", "5m", "bullish", 1, "limit", 19950.0, 19930.0, [20050.0], ready_now=False)
    trade = pt.open_trade_from_candidate(c)
    assert trade["status"] == "pending_fill"
    assert trade["fill_price"] is None
    assert trade["opened_at"] is None


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
    assert result["status"] == "pending_fill"  # Tier 3.2: not "open" anymore, even for a market order


# ---------------------------------------------------------------------------
# process_new_bar — fills (event time, slippage)
# ---------------------------------------------------------------------------

def test_market_order_fills_on_next_bar_at_its_open_plus_slippage(fresh_paper_trades):
    storage, pt, _ = fresh_paper_trades()
    c = _candidate("c1", "TEST", "5m", "bullish", 1, "market", 20020.0, 20000.0, [20100.0],
                    bar_timestamp="2026-08-13T10:00:00Z")
    pt.open_trade_from_candidate(c)

    changed = pt.process_new_bar("TEST", "5m", _bar("2026-08-13T10:05:00Z", open_=20022.0, high=20030.0, low=20018.0))
    assert len(changed) == 1
    assert changed[0]["status"] == "open"
    assert changed[0]["fill_price"] == 20022.25  # bar open (20022.0) + 0.25 slippage, against the buyer
    assert changed[0]["opened_at"] == "2026-08-13T10:05:00Z"  # EVENT time (the filling bar's own timestamp)


def test_short_market_order_fill_slippage_is_against_the_seller(fresh_paper_trades):
    storage, pt, _ = fresh_paper_trades()
    c = _candidate("c1", "TEST", "5m", "bearish", 1, "market", 20050.0, 20070.0, [19950.0])
    pt.open_trade_from_candidate(c)

    changed = pt.process_new_bar("TEST", "5m", _bar("2026-08-13T10:05:00Z", open_=20050.0, high=20060.0, low=20040.0))
    assert changed[0]["fill_price"] == 20049.75  # bar open - 0.25, worse for a short seller


def test_limit_fill_has_no_slippage_fills_exactly_at_the_limit(fresh_paper_trades):
    """A resting limit order is filled at its stated price by
    definition -- no slippage modeled, unlike a market fill."""
    storage, pt, _ = fresh_paper_trades()
    c = _candidate("c1", "TEST", "5m", "bullish", 1, "limit", 19950.0, 19930.0, [20050.0], ready_now=False)
    pt.open_trade_from_candidate(c)

    # Bar doesn't reach the limit yet -- stays pending
    pt.process_new_bar("TEST", "5m", _bar("2026-08-13T10:05:00Z", open_=19990.0, high=20000.0, low=19980.0))
    trades = storage.get_open_or_pending_trades("TEST", "5m")
    assert trades[0]["status"] == "pending_fill"

    # Bar's low reaches the limit -> fills exactly at 19950.0
    changed = pt.process_new_bar("TEST", "5m", _bar("2026-08-13T10:10:00Z", open_=19970.0, high=19970.0, low=19945.0))
    assert changed[0]["fill_price"] == 19950.0
    assert changed[0]["opened_at"] == "2026-08-13T10:10:00Z"


def test_pending_short_fills_when_price_rallies_to_limit(fresh_paper_trades):
    storage, pt, _ = fresh_paper_trades()
    c = _candidate("c1", "TEST", "5m", "bearish", 1, "limit", 20050.0, 20070.0, [19950.0], ready_now=False)
    pt.open_trade_from_candidate(c)
    changed = pt.process_new_bar("TEST", "5m", _bar("2026-08-13T10:05:00Z", open_=20030.0, high=20055.0, low=20010.0))
    assert changed[0]["status"] == "open"
    assert changed[0]["fill_price"] == 20050.0


# ---------------------------------------------------------------------------
# process_new_bar — stop/target closes, gap-through-stop, P&L (net of
# slippage + commission)
# ---------------------------------------------------------------------------

def test_long_closes_on_stop_hit_no_gap(fresh_paper_trades):
    storage, pt, _ = fresh_paper_trades()
    c = _candidate("c1", "TEST", "5m", "bullish", 2, "market", 20020.0, 20000.0, [20100.0])
    pt.open_trade_from_candidate(c)
    pt.process_new_bar("TEST", "5m", _bar("2026-08-13T10:05:00Z", open_=20020.0, high=20025.0, low=20015.0))  # fills at 20020.25

    changed = pt.process_new_bar("TEST", "5m", _bar("2026-08-13T10:10:00Z", open_=20005.0, high=20010.0, low=19995.0))  # no gap: open(20005) > stop(20000)
    assert len(changed) == 1
    assert changed[0]["exit_reason"] == "stop_hit"
    assert changed[0]["closed_at"] == "2026-08-13T10:10:00Z"
    # raw_exit = min(open, stop) = 20000 (no gap); -0.25 slippage = 19999.75
    # pnl = (19999.75 - 20020.25) * $2/pt * 2 contracts - $4 round-trip commission = -82 - 4 = -86.0
    assert changed[0]["pnl_usd"] == -86.0
    assert pt.get_open_trade_count("TEST", "5m") == 0


def test_long_stop_gap_uses_bar_open_not_the_stop_price(fresh_paper_trades):
    """The conservative gap-through-stop fix: if the bar's OPEN has
    already gapped past the stop, the realistic fill is the open (a
    real stop-market order doesn't get its exact requested price
    through a gap), not the stop level itself."""
    storage, pt, _ = fresh_paper_trades()
    c = _candidate("c1", "TEST", "5m", "bullish", 1, "market", 20020.0, 20000.0, [20100.0])
    pt.open_trade_from_candidate(c)
    pt.process_new_bar("TEST", "5m", _bar("2026-08-13T10:05:00Z", open_=20020.0, high=20025.0, low=20015.0))  # fills at 20020.25

    changed = pt.process_new_bar("TEST", "5m", _bar("2026-08-13T10:10:00Z", open_=19990.0, high=20010.0, low=19985.0))  # gapped below stop
    # raw_exit = min(open=19990, stop=20000) = 19990 (the gap); -0.25 slippage = 19989.75
    assert changed[0]["exit_price"] == 19989.75
    # pnl = (19989.75 - 20020.25) * 2 * 1 - 2 = -61 - 2 = -63.0
    assert changed[0]["pnl_usd"] == -63.0


def test_long_closes_on_target_hit_no_slippage_at_target(fresh_paper_trades):
    storage, pt, _ = fresh_paper_trades()
    c = _candidate("c1", "TEST", "5m", "bullish", 1, "market", 20020.0, 20000.0, [20100.0])
    pt.open_trade_from_candidate(c)
    pt.process_new_bar("TEST", "5m", _bar("2026-08-13T10:05:00Z", open_=20020.0, high=20025.0, low=20015.0))  # fills at 20020.25

    changed = pt.process_new_bar("TEST", "5m", _bar("2026-08-13T10:10:00Z", open_=20095.0, high=20105.0, low=20090.0))
    assert changed[0]["exit_reason"] == "target_hit"
    assert changed[0]["exit_price"] == 20100.0  # exact target, no slippage credited
    # pnl = (20100 - 20020.25) * 2 * 1 - 2 = 159.5 - 2 = 157.5
    assert changed[0]["pnl_usd"] == 157.5


def test_short_closes_on_target_hit(fresh_paper_trades):
    storage, pt, _ = fresh_paper_trades()
    c = _candidate("c1", "TEST", "5m", "bearish", 1, "market", 20050.0, 20070.0, [19950.0])
    pt.open_trade_from_candidate(c)
    pt.process_new_bar("TEST", "5m", _bar("2026-08-13T10:05:00Z", open_=20050.0, high=20060.0, low=20040.0))  # fills at 20049.75

    changed = pt.process_new_bar("TEST", "5m", _bar("2026-08-13T10:10:00Z", open_=19955.0, high=19960.0, low=19945.0))
    assert changed[0]["exit_reason"] == "target_hit"
    assert changed[0]["exit_price"] == 19950.0
    # pnl = (20049.75 - 19950) * 2 * 1 - 2 = 199.5 - 2 = 197.5
    assert changed[0]["pnl_usd"] == 197.5


def test_short_closes_on_stop_hit_no_gap(fresh_paper_trades):
    storage, pt, _ = fresh_paper_trades()
    c = _candidate("c1", "TEST", "5m", "bearish", 1, "market", 20050.0, 20070.0, [19950.0])
    pt.open_trade_from_candidate(c)
    pt.process_new_bar("TEST", "5m", _bar("2026-08-13T10:05:00Z", open_=20050.0, high=20060.0, low=20040.0))  # fills at 20049.75

    changed = pt.process_new_bar("TEST", "5m", _bar("2026-08-13T10:10:00Z", open_=20055.0, high=20075.0, low=20050.0))  # no gap
    assert changed[0]["exit_reason"] == "stop_hit"
    # raw_exit = max(open=20055, stop=20070) = 20070 (no gap); +0.25 slippage = 20070.25
    assert changed[0]["exit_price"] == 20070.25
    # pnl = (20049.75 - 20070.25) * 2 * 1 - 2 = -41 - 2 = -43.0
    assert changed[0]["pnl_usd"] == -43.0


def test_gap_bar_hitting_both_stop_and_target_assumes_stop_first(fresh_paper_trades):
    """Conservative assumption documented in paper_trades.py: with
    only OHLC (no tick data), a bar that spans both the stop and the
    target is treated as the stop having been hit first."""
    storage, pt, _ = fresh_paper_trades()
    c = _candidate("c1", "TEST", "5m", "bullish", 1, "market", 20020.0, 20000.0, [20100.0])
    pt.open_trade_from_candidate(c)
    pt.process_new_bar("TEST", "5m", _bar("2026-08-13T10:05:00Z", open_=20020.0, high=20025.0, low=20015.0))

    changed = pt.process_new_bar("TEST", "5m", _bar("2026-08-13T10:10:00Z", open_=20005.0, high=20150.0, low=19990.0))
    assert changed[0]["exit_reason"] == "stop_hit"


def test_fill_and_immediate_stop_in_the_same_bar(fresh_paper_trades):
    """A limit fills and then reverses to stop within the SAME bar --
    both transitions should be reflected in one process_new_bar call."""
    storage, pt, _ = fresh_paper_trades()
    c = _candidate("c1", "TEST", "5m", "bullish", 1, "limit", 19950.0, 19930.0, [20050.0], ready_now=False)
    pt.open_trade_from_candidate(c)

    changed = pt.process_new_bar("TEST", "5m", _bar("2026-08-13T10:05:00Z", open_=19955.0, high=19960.0, low=19925.0))
    assert len(changed) == 2
    assert changed[0]["status"] == "open"  # the fill, exactly at the limit (19950.0)
    assert changed[1]["status"] == "closed"
    assert changed[1]["exit_reason"] == "stop_hit"
    # raw_exit = min(open=19955, stop=19930) = 19930 (no gap); -0.25 = 19929.75
    # pnl = (19929.75 - 19950.0) * 2 * 1 - 2 = -40.5 - 2 = -42.5
    assert changed[1]["pnl_usd"] == -42.5


def test_untouched_open_trade_stays_open(fresh_paper_trades):
    storage, pt, _ = fresh_paper_trades()
    c = _candidate("c1", "TEST", "5m", "bullish", 1, "market", 20020.0, 20000.0, [20100.0])
    pt.open_trade_from_candidate(c)
    pt.process_new_bar("TEST", "5m", _bar("2026-08-13T10:05:00Z", open_=20020.0, high=20025.0, low=20015.0))  # fills

    changed = pt.process_new_bar("TEST", "5m", _bar("2026-08-13T10:10:00Z", open_=20022.0, high=20030.0, low=20017.0))
    assert changed == []
    assert pt.get_open_trade_count("TEST", "5m") == 1


def test_closed_trade_untouched_by_further_bars(fresh_paper_trades):
    """A duplicate/retried bar delivery must never double-close (and
    double-count P&L for) a trade that's already closed."""
    storage, pt, _ = fresh_paper_trades()
    c = _candidate("c1", "TEST", "5m", "bullish", 1, "market", 20020.0, 20000.0, [20100.0])
    pt.open_trade_from_candidate(c)
    pt.process_new_bar("TEST", "5m", _bar("2026-08-13T10:05:00Z", open_=20020.0, high=20025.0, low=20015.0))
    pt.process_new_bar("TEST", "5m", _bar("2026-08-13T10:10:00Z", open_=20005.0, high=20010.0, low=19995.0))  # stop hit, closes

    changed_again = pt.process_new_bar("TEST", "5m", _bar("2026-08-13T10:15:00Z", open_=20200.0, high=20205.0, low=20190.0))
    assert changed_again == []  # nothing left open/pending to process


# ---------------------------------------------------------------------------
# Tier 3.2: pending-order expiry (event time, not wall-clock)
# ---------------------------------------------------------------------------

def test_pending_limit_expires_after_order_expiry_minutes(fresh_paper_trades):
    storage, pt, _ = fresh_paper_trades(ORDER_EXPIRY_MINUTES="30")
    c = _candidate("c1", "TEST", "5m", "bullish", 1, "limit", 19950.0, 19930.0, [20050.0],
                    bar_timestamp="2026-08-13T10:00:00Z")
    pt.open_trade_from_candidate(c)

    # 45 minutes of EVENT time later, price never reached the limit
    changed = pt.process_new_bar("TEST", "5m", _bar("2026-08-13T10:45:00Z", open_=20000.0, high=20010.0, low=19990.0))
    assert len(changed) == 1
    assert changed[0]["status"] == "cancelled"
    assert changed[0]["exit_reason"] == "expired_unfilled"
    assert pt.get_open_trade_count("TEST", "5m") == 0  # frees up capacity

    stored = storage.get_trade_by_candidate_id("c1")
    assert stored["status"] == "cancelled"
    assert stored["closed_at"] == "2026-08-13T10:45:00Z"  # EVENT time


def test_pending_limit_not_yet_expired_stays_pending(fresh_paper_trades):
    storage, pt, _ = fresh_paper_trades(ORDER_EXPIRY_MINUTES="30")
    c = _candidate("c1", "TEST", "5m", "bullish", 1, "limit", 19950.0, 19930.0, [20050.0],
                    bar_timestamp="2026-08-13T10:00:00Z")
    pt.open_trade_from_candidate(c)

    changed = pt.process_new_bar("TEST", "5m", _bar("2026-08-13T10:15:00Z", open_=20000.0, high=20010.0, low=19990.0))
    assert changed == []
    assert pt.get_open_trade_count("TEST", "5m") == 1
    assert storage.get_trade_by_candidate_id("c1")["status"] == "pending_fill"


def test_expiry_uses_event_time_not_real_wall_clock(fresh_paper_trades):
    """The actual point of this tier: expiry must be computed from bar
    timestamps, never from real datetime.now() — a fixed test
    timestamp from the past must not appear "expired" just because
    real time has moved on since it was written, and a small EVENT-time
    gap must not expire even though wall-clock time barely passed while
    the test ran."""
    storage, pt, _ = fresh_paper_trades(ORDER_EXPIRY_MINUTES="30")
    c = _candidate("c1", "TEST", "5m", "bullish", 1, "limit", 19950.0, 19930.0, [20050.0],
                    bar_timestamp="2020-01-01T10:00:00Z")  # long "ago" in real wall-clock terms
    pt.open_trade_from_candidate(c)

    # Only 10 EVENT minutes later -- must NOT be expired, despite being
    # years in the past relative to when this test actually runs.
    changed = pt.process_new_bar("TEST", "5m", _bar("2020-01-01T10:10:00Z", open_=20000.0, high=20010.0, low=19990.0))
    assert changed == []
    assert storage.get_trade_by_candidate_id("c1")["status"] == "pending_fill"


def test_market_order_with_no_anchor_bar_timestamp_never_expires(fresh_paper_trades):
    """Defensive/fail-safe path: a candidate with no bar (shouldn't
    normally happen) stores order_submitted_at=None -- expiry must
    skip rather than guess, since cancelling is a destructive action."""
    storage, pt, _ = fresh_paper_trades(ORDER_EXPIRY_MINUTES="1")
    c = _candidate("c1", "TEST", "5m", "bullish", 1, "limit", 19950.0, 19930.0, [20050.0])
    c["bar"] = None
    pt.open_trade_from_candidate(c)
    assert storage.get_trade_by_candidate_id("c1")["order_submitted_at"] is None

    changed = pt.process_new_bar("TEST", "5m", _bar("2026-08-13T12:00:00Z", open_=20000.0, high=20010.0, low=19990.0))
    assert changed == []
    assert storage.get_trade_by_candidate_id("c1")["status"] == "pending_fill"


# ---------------------------------------------------------------------------
# Tier 3.2: commission in isolation (slippage zeroed out for clarity)
# ---------------------------------------------------------------------------

def test_commission_deducted_from_pnl(fresh_paper_trades):
    storage, pt, _ = fresh_paper_trades(SLIPPAGE_POINTS="0", COMMISSION_PER_CONTRACT="3.5")
    c = _candidate("c1", "TEST", "5m", "bullish", 2, "market", 20020.0, 20000.0, [20100.0])
    pt.open_trade_from_candidate(c)
    pt.process_new_bar("TEST", "5m", _bar("2026-08-13T10:05:00Z", open_=20020.0, high=20025.0, low=20015.0))  # fills at 20020.0 (no slippage)

    changed = pt.process_new_bar("TEST", "5m", _bar("2026-08-13T10:10:00Z", open_=20095.0, high=20105.0, low=20090.0))
    # pnl before commission: (20100 - 20020) * 2 * 2 = 320.0; commission = 3.5 * 2 = 7.0
    assert changed[0]["pnl_usd"] == 313.0
