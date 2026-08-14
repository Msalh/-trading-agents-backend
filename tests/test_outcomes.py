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


def _candidate_with_opinions(candidate_id, symbol, timeframe, decision_timestamp_dt, opinions_used):
    """Tier 3.5 candidate shape — unlike _candidate() above, this
    includes opinions_used, since compute_per_agent_accuracy() reads
    each individual agent's frozen opinion rather than the blended
    decision. `opinions_used` values are dicts like
    {"direction": "bullish", "timestamp": <iso str or None>}."""
    return {
        "candidate_id": candidate_id,
        "symbol": symbol,
        "timeframe": timeframe,
        "decision": {
            "decision": "enter_long",
            "timestamp": _iso(decision_timestamp_dt),
            "opinions_used": opinions_used,
        },
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


# ---------------------------------------------------------------------------
# compute_per_agent_accuracy (Tier 3.5 / 3.6)
#
# Response shape as of Tier 3.6: {"by_candidate": {agent: {horizon:
# {...}}}, "by_distinct_opinion": (same shape), "distinct_opinion_counts":
# {agent: int}}. Most tests below assert against "by_candidate" (the
# original Tier 3.5 tally, one data point per candidate) since that's
# what most of these scenarios exercise; the dedup-specific tests near
# the end assert "by_distinct_opinion" and "distinct_opinion_counts"
# directly.
# ---------------------------------------------------------------------------

def test_per_agent_accuracy_scores_each_agent_against_its_own_call(fresh_env):
    """Analysis bullish + price rose -> correct. News bearish + price
    rose -> incorrect. Both are scored independently of any blended
    Coordinator decision -- there isn't one being read here at all."""
    storage, pt, outcomes = fresh_env
    decision_time = datetime.now(timezone.utc) - timedelta(minutes=30)
    horizon_time = decision_time + timedelta(minutes=15)
    _save_bar(storage, "TEST", "5m", decision_time, 100.0)
    _save_bar(storage, "TEST", "5m", horizon_time, 105.0)

    c = _candidate_with_opinions(
        "c1", "TEST", "5m", decision_time,
        {
            "analysis": {"direction": "bullish", "timestamp": _iso(decision_time)},
            "news": {"direction": "bearish", "timestamp": _iso(decision_time)},
        },
    )

    summary = outcomes.compute_per_agent_accuracy([c], horizons=[15])
    by_candidate = summary["by_candidate"]
    assert by_candidate["analysis"][15]["correct"] == 1
    assert by_candidate["analysis"][15]["accuracy"] == 1.0
    assert by_candidate["news"][15]["incorrect"] == 1
    assert by_candidate["news"][15]["accuracy"] == 0.0
    # A single, never-reused opinion each -- by_distinct_opinion must agree exactly.
    assert summary["by_distinct_opinion"]["analysis"][15] == by_candidate["analysis"][15]
    assert summary["by_distinct_opinion"]["news"][15] == by_candidate["news"][15]
    assert summary["distinct_opinion_counts"] == {"analysis": 1, "news": 1, "macro": 0}


def test_per_agent_accuracy_skips_neutral_opinions(fresh_env):
    storage, pt, outcomes = fresh_env
    decision_time = datetime.now(timezone.utc) - timedelta(minutes=30)
    horizon_time = decision_time + timedelta(minutes=15)
    _save_bar(storage, "TEST", "5m", decision_time, 100.0)
    _save_bar(storage, "TEST", "5m", horizon_time, 105.0)

    c = _candidate_with_opinions(
        "c1", "TEST", "5m", decision_time,
        {"analysis": {"direction": "neutral", "timestamp": _iso(decision_time)}},
    )

    summary = outcomes.compute_per_agent_accuracy([c], horizons=[15])
    counts = summary["by_candidate"]["analysis"][15]
    assert counts["correct"] == 0 and counts["incorrect"] == 0
    assert counts["accuracy"] is None
    assert summary["distinct_opinion_counts"]["analysis"] == 0


def test_per_agent_accuracy_skips_missing_agents_but_still_reports_all_three_keys(fresh_env):
    """A candidate with only an `analysis` opinion never touches
    news/macro -- they must still appear in both sections of the
    returned summary (all zero counts, accuracy None), not be silently
    absent, since a caller comparing agents needs every agent's key
    present even when a given dataset never exercised it."""
    storage, pt, outcomes = fresh_env
    decision_time = datetime.now(timezone.utc) - timedelta(minutes=30)
    horizon_time = decision_time + timedelta(minutes=15)
    _save_bar(storage, "TEST", "5m", decision_time, 100.0)
    _save_bar(storage, "TEST", "5m", horizon_time, 105.0)

    c = _candidate_with_opinions(
        "c1", "TEST", "5m", decision_time,
        {"analysis": {"direction": "bullish", "timestamp": _iso(decision_time)}},
    )

    summary = outcomes.compute_per_agent_accuracy([c], horizons=[15])
    empty = {"correct": 0, "incorrect": 0, "flat": 0, "pending": 0, "no_data": 0, "accuracy": None}
    for section in ("by_candidate", "by_distinct_opinion"):
        assert set(summary[section].keys()) == {"analysis", "news", "macro"}
        assert summary[section]["news"][15] == empty
        assert summary[section]["macro"][15] == empty
    assert summary["distinct_opinion_counts"] == {"analysis": 1, "news": 0, "macro": 0}


def test_per_agent_accuracy_falls_back_to_decision_timestamp_when_opinion_has_none(fresh_env):
    """Older candidates predate per-opinion timestamps -- the opinion
    dict may have timestamp=None (or the key absent). Must fall back
    to the candidate's own decision timestamp rather than being
    skipped entirely."""
    storage, pt, outcomes = fresh_env
    decision_time = datetime.now(timezone.utc) - timedelta(minutes=30)
    horizon_time = decision_time + timedelta(minutes=15)
    _save_bar(storage, "TEST", "5m", decision_time, 100.0)
    _save_bar(storage, "TEST", "5m", horizon_time, 105.0)

    c = _candidate_with_opinions(
        "c1", "TEST", "5m", decision_time,
        {"analysis": {"direction": "bullish", "timestamp": None}},
    )

    summary = outcomes.compute_per_agent_accuracy([c], horizons=[15])
    assert summary["by_candidate"]["analysis"][15]["correct"] == 1


def test_per_agent_accuracy_skips_opinion_with_no_timestamp_anywhere(fresh_env):
    """If neither the opinion nor the candidate's decision carries a
    timestamp, there's no anchor to evaluate against -- must be
    skipped, not raise."""
    storage, pt, outcomes = fresh_env
    c = _candidate_with_opinions(
        "c1", "TEST", "5m", datetime.now(timezone.utc),
        {"analysis": {"direction": "bullish", "timestamp": None}},
    )
    c["decision"]["timestamp"] = None

    summary = outcomes.compute_per_agent_accuracy([c], horizons=[15])
    counts = summary["by_candidate"]["analysis"][15]
    assert counts["correct"] == 0 and counts["incorrect"] == 0 and counts["no_data"] == 0
    assert summary["distinct_opinion_counts"]["analysis"] == 0


def test_per_agent_accuracy_aggregates_across_multiple_candidates(fresh_env):
    storage, pt, outcomes = fresh_env
    t1 = datetime.now(timezone.utc) - timedelta(minutes=60)
    t2 = datetime.now(timezone.utc) - timedelta(minutes=30)
    _save_bar(storage, "TEST", "5m", t1, 100.0)
    _save_bar(storage, "TEST", "5m", t1 + timedelta(minutes=15), 105.0)  # analysis c1: correct
    _save_bar(storage, "TEST", "5m", t2, 200.0)
    _save_bar(storage, "TEST", "5m", t2 + timedelta(minutes=15), 195.0)  # analysis c2: incorrect (bullish, price fell)

    c1 = _candidate_with_opinions("c1", "TEST", "5m", t1, {"analysis": {"direction": "bullish", "timestamp": _iso(t1)}})
    c2 = _candidate_with_opinions("c2", "TEST", "5m", t2, {"analysis": {"direction": "bullish", "timestamp": _iso(t2)}})

    summary = outcomes.compute_per_agent_accuracy([c1, c2], horizons=[15])
    by_candidate = summary["by_candidate"]["analysis"][15]
    assert by_candidate["correct"] == 1
    assert by_candidate["incorrect"] == 1
    assert by_candidate["accuracy"] == 0.5
    # Two genuinely distinct opinions (different timestamps) -- dedup changes nothing here.
    assert summary["by_distinct_opinion"]["analysis"][15] == by_candidate
    assert summary["distinct_opinion_counts"]["analysis"] == 2


def test_per_agent_accuracy_never_touches_trades_or_mutates_candidates(fresh_env):
    """Read-only guarantee, same pattern as replay_candidate() /
    sweep_thresholds() -- no paper trade should ever be created as a
    side effect of this purely analytical function."""
    storage, pt, outcomes = fresh_env
    decision_time = datetime.now(timezone.utc) - timedelta(minutes=30)
    _save_bar(storage, "TEST", "5m", decision_time, 100.0)
    c = _candidate_with_opinions(
        "c1", "TEST", "5m", decision_time,
        {"analysis": {"direction": "bullish", "timestamp": _iso(decision_time)}},
    )
    before = dict(c["decision"])

    outcomes.compute_per_agent_accuracy([c], horizons=[15])

    assert c["decision"] == before
    assert storage.get_trade_by_candidate_id("c1") is None


def test_per_agent_accuracy_empty_candidates_returns_zero_counts_for_all_agents(fresh_env):
    storage, pt, outcomes = fresh_env
    summary = outcomes.compute_per_agent_accuracy([], horizons=[15, 30])
    for section in ("by_candidate", "by_distinct_opinion"):
        assert set(summary[section].keys()) == {"analysis", "news", "macro"}
        for agent in ("analysis", "news", "macro"):
            for h in (15, 30):
                assert summary[section][agent][h]["accuracy"] is None
    assert summary["distinct_opinion_counts"] == {"analysis": 0, "news": 0, "macro": 0}


# ---------------------------------------------------------------------------
# Tier 3.6: by_distinct_opinion dedup
# ---------------------------------------------------------------------------

def test_per_agent_accuracy_dedup_reused_opinion_counted_once_in_distinct_view(fresh_env):
    """The real-world case this tier was built for: News/Macro run on
    their own schedule and the SAME opinion (identical timestamp/
    direction) gets frozen into several consecutive candidates while
    still fresh. by_candidate should tally it once per candidate (the
    original Tier 3.5 behavior); by_distinct_opinion must tally it
    exactly once regardless of how many candidates reused it."""
    storage, pt, outcomes = fresh_env
    opinion_time = datetime.now(timezone.utc) - timedelta(minutes=60)
    horizon_time = opinion_time + timedelta(minutes=15)
    _save_bar(storage, "TEST", "5m", opinion_time, 100.0)
    _save_bar(storage, "TEST", "5m", horizon_time, 105.0)  # news bullish, price rose -> correct

    # Three separate candidates (different decision timestamps, as three
    # different webhook bars would produce), all sharing the exact same
    # frozen news opinion -- exactly how a still-fresh News opinion gets
    # reused across consecutive 5-minute bars in production.
    same_news_opinion = {"direction": "bullish", "timestamp": _iso(opinion_time)}
    candidates = [
        _candidate_with_opinions("c1", "TEST", "5m", opinion_time + timedelta(minutes=5), {"news": same_news_opinion}),
        _candidate_with_opinions("c2", "TEST", "5m", opinion_time + timedelta(minutes=10), {"news": same_news_opinion}),
        _candidate_with_opinions("c3", "TEST", "5m", opinion_time + timedelta(minutes=15), {"news": same_news_opinion}),
    ]

    summary = outcomes.compute_per_agent_accuracy(candidates, horizons=[15])
    assert summary["by_candidate"]["news"][15]["correct"] == 3
    assert summary["by_distinct_opinion"]["news"][15]["correct"] == 1
    assert summary["distinct_opinion_counts"]["news"] == 1


def test_per_agent_accuracy_dedup_key_includes_direction_not_just_timestamp(fresh_env):
    """Two opinions sharing a timestamp but disagreeing on direction
    (shouldn't happen in practice, but the dedup key must not silently
    collapse them into one) are still two distinct opinions."""
    storage, pt, outcomes = fresh_env
    opinion_time = datetime.now(timezone.utc) - timedelta(minutes=30)
    horizon_time = opinion_time + timedelta(minutes=15)
    _save_bar(storage, "TEST", "5m", opinion_time, 100.0)
    _save_bar(storage, "TEST", "5m", horizon_time, 105.0)

    candidates = [
        _candidate_with_opinions("c1", "TEST", "5m", opinion_time, {"macro": {"direction": "bullish", "timestamp": _iso(opinion_time)}}),
        _candidate_with_opinions("c2", "TEST", "5m", opinion_time, {"macro": {"direction": "bearish", "timestamp": _iso(opinion_time)}}),
    ]

    summary = outcomes.compute_per_agent_accuracy(candidates, horizons=[15])
    assert summary["distinct_opinion_counts"]["macro"] == 2
    assert summary["by_distinct_opinion"]["macro"][15]["correct"] == 1
    assert summary["by_distinct_opinion"]["macro"][15]["incorrect"] == 1


def test_per_agent_accuracy_dedup_does_not_cross_contaminate_agents(fresh_env):
    """Analysis and News sharing an identical (timestamp, direction)
    pair by coincidence must still be tracked as separate distinct-
    opinion pools per agent."""
    storage, pt, outcomes = fresh_env
    opinion_time = datetime.now(timezone.utc) - timedelta(minutes=30)
    horizon_time = opinion_time + timedelta(minutes=15)
    _save_bar(storage, "TEST", "5m", opinion_time, 100.0)
    _save_bar(storage, "TEST", "5m", horizon_time, 105.0)

    c = _candidate_with_opinions(
        "c1", "TEST", "5m", opinion_time,
        {
            "analysis": {"direction": "bullish", "timestamp": _iso(opinion_time)},
            "news": {"direction": "bullish", "timestamp": _iso(opinion_time)},
        },
    )

    summary = outcomes.compute_per_agent_accuracy([c], horizons=[15])
    assert summary["distinct_opinion_counts"]["analysis"] == 1
    assert summary["distinct_opinion_counts"]["news"] == 1


# ---------------------------------------------------------------------------
# compute_agent_opinion_detail (Tier 3.7)
# ---------------------------------------------------------------------------

def test_opinion_detail_returns_one_record_per_distinct_opinion(fresh_env):
    storage, pt, outcomes = fresh_env
    t1 = datetime.now(timezone.utc) - timedelta(minutes=60)
    t2 = datetime.now(timezone.utc) - timedelta(minutes=30)
    _save_bar(storage, "TEST", "5m", t1, 100.0)
    _save_bar(storage, "TEST", "5m", t1 + timedelta(minutes=15), 105.0)
    _save_bar(storage, "TEST", "5m", t2, 200.0)
    _save_bar(storage, "TEST", "5m", t2 + timedelta(minutes=15), 195.0)

    c1 = _candidate_with_opinions("c1", "TEST", "5m", t1, {"analysis": {"direction": "bullish", "timestamp": _iso(t1), "confidence": 70, "flags": []}})
    c2 = _candidate_with_opinions("c2", "TEST", "5m", t2, {"analysis": {"direction": "bullish", "timestamp": _iso(t2), "confidence": 40, "flags": ["choppy"]}})

    opinions = outcomes.compute_agent_opinion_detail([c1, c2], agent="analysis", horizons=[15])
    assert len(opinions) == 2
    # sorted oldest first
    assert opinions[0]["opinion_timestamp"] == _iso(t1)
    assert opinions[0]["confidence"] == 70
    assert opinions[0]["flags"] == []
    assert opinions[0]["outcome_by_horizon"][15] == "correct"
    assert opinions[1]["opinion_timestamp"] == _iso(t2)
    assert opinions[1]["confidence"] == 40
    assert opinions[1]["flags"] == ["choppy"]
    assert opinions[1]["outcome_by_horizon"][15] == "incorrect"


def test_opinion_detail_tracks_reuse_count_across_candidates(fresh_env):
    """The per-opinion version of Tier 3.6's distinct_opinion_counts --
    a single reused News/Macro opinion should show how many candidates
    in the window actually reused it."""
    storage, pt, outcomes = fresh_env
    opinion_time = datetime.now(timezone.utc) - timedelta(minutes=60)
    horizon_time = opinion_time + timedelta(minutes=15)
    _save_bar(storage, "TEST", "5m", opinion_time, 100.0)
    _save_bar(storage, "TEST", "5m", horizon_time, 105.0)

    same_opinion = {"direction": "bullish", "timestamp": _iso(opinion_time), "confidence": 55, "flags": []}
    candidates = [
        _candidate_with_opinions("c1", "TEST", "5m", opinion_time + timedelta(minutes=5), {"news": same_opinion}),
        _candidate_with_opinions("c2", "TEST", "5m", opinion_time + timedelta(minutes=10), {"news": same_opinion}),
        _candidate_with_opinions("c3", "TEST", "5m", opinion_time + timedelta(minutes=15), {"news": same_opinion}),
    ]

    opinions = outcomes.compute_agent_opinion_detail(candidates, agent="news", horizons=[15])
    assert len(opinions) == 1
    assert opinions[0]["reused_by_candidate_count"] == 3


def test_opinion_detail_skips_neutral_and_missing_opinions(fresh_env):
    storage, pt, outcomes = fresh_env
    decision_time = datetime.now(timezone.utc) - timedelta(minutes=30)
    c1 = _candidate_with_opinions("c1", "TEST", "5m", decision_time, {"analysis": {"direction": "neutral", "timestamp": _iso(decision_time)}})
    c2 = _candidate_with_opinions("c2", "TEST", "5m", decision_time, {})

    opinions = outcomes.compute_agent_opinion_detail([c1, c2], agent="analysis", horizons=[15])
    assert opinions == []


def test_opinion_detail_rejects_unknown_agent(fresh_env):
    storage, pt, outcomes = fresh_env
    with pytest.raises(ValueError):
        outcomes.compute_agent_opinion_detail([], agent="timing", horizons=[15])


def test_opinion_detail_excludes_reasoning_and_key_data(fresh_env):
    """Deliberately compact -- reasoning/key_data must never leak into
    the record even if present on the source opinion."""
    storage, pt, outcomes = fresh_env
    decision_time = datetime.now(timezone.utc) - timedelta(minutes=30)
    _save_bar(storage, "TEST", "5m", decision_time, 100.0)
    c = _candidate_with_opinions(
        "c1", "TEST", "5m", decision_time,
        {"analysis": {
            "direction": "bullish", "timestamp": _iso(decision_time), "confidence": 60,
            "flags": [], "reasoning": "a long explanation", "key_data": {"vwap": 100.5},
        }},
    )

    opinions = outcomes.compute_agent_opinion_detail([c], agent="analysis", horizons=[15])
    assert "reasoning" not in opinions[0]
    assert "key_data" not in opinions[0]
