"""
Unit tests for app.storage's Tier 3.1 (causal integrity) additions:
get_by_event_id/get_recent_as_of (bar anchoring), and the write-once
behavior of attach_risk_result/attach_execution_result. Also covers
Tier 3.2's cancel_trade()/migration and Tier 3.3's
open_trade_if_room()/get_open_or_pending_trade_count() (account-wide,
atomic position-limit enforcement). Storage-level tests only for the
plumbing that isn't already exercised end-to-end via
tests/test_candidates.py / tests/test_paper_trades.py.

Run with: pytest tests/test_storage.py -v
"""

import importlib
import os
import tempfile

import pytest

from app.models import MarketStatePayload


@pytest.fixture
def fresh_storage(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setenv("DB_PATH", tmp.name)

    import app.storage as storage
    importlib.reload(storage)
    storage.init_db()

    yield storage

    os.unlink(tmp.name)


def _bar(event_id, timestamp, close=100.0):
    return MarketStatePayload(
        schema_version="1.0", event_id=event_id, symbol="TEST", source="pine",
        timeframe="5m", timestamp=timestamp, bar_status="closed", event_type="bar_closed",
        secret="x", open=close, high=close + 1, low=close - 1, close=close,
        session_name="RTH", is_rth=True, trading_date=timestamp[:10],
    )


def test_get_by_event_id_returns_the_exact_bar(fresh_storage):
    storage = fresh_storage
    storage.save_event(_bar("evt-1", "2026-08-13T10:00:00Z", close=111.0))
    storage.save_event(_bar("evt-2", "2026-08-13T10:05:00Z", close=222.0))

    bar = storage.get_by_event_id("evt-1")
    assert bar["event_id"] == "evt-1"
    assert bar["close"] == 111.0


def test_get_by_event_id_returns_none_for_unknown_id(fresh_storage):
    storage = fresh_storage
    assert storage.get_by_event_id("does-not-exist") is None


def test_get_recent_as_of_excludes_bars_after_the_anchor(fresh_storage):
    """The core anchoring guarantee: bars that arrived AFTER the anchor
    timestamp must never leak into a window bounded by it — this is
    what keeps a delayed/queued Analysis run from silently peeking at
    a bar newer than the one that actually triggered it."""
    storage = fresh_storage
    storage.save_event(_bar("evt-1", "2026-08-13T10:00:00Z"))
    storage.save_event(_bar("evt-2", "2026-08-13T10:05:00Z"))
    storage.save_event(_bar("evt-3", "2026-08-13T10:10:00Z"))  # "arrives later"

    window = storage.get_recent_as_of("TEST", "5m", as_of_timestamp="2026-08-13T10:05:00Z", limit=10)
    event_ids = {b["event_id"] for b in window}
    assert event_ids == {"evt-1", "evt-2"}
    assert "evt-3" not in event_ids


def test_get_recent_as_of_respects_limit(fresh_storage):
    storage = fresh_storage
    for i in range(5):
        storage.save_event(_bar(f"evt-{i}", f"2026-08-13T10:0{i}:00Z"))

    window = storage.get_recent_as_of("TEST", "5m", as_of_timestamp="2026-08-13T10:04:00Z", limit=2)
    assert len(window) == 2


def test_attach_risk_result_not_found_for_unknown_candidate(fresh_storage):
    storage = fresh_storage
    assert storage.attach_risk_result("no-such-id", {"decision": "approve"}) == "not_found"


def test_attach_risk_result_ok_then_locked_after_trade_committed(fresh_storage):
    storage = fresh_storage
    storage.save_candidate(
        candidate_id="c1", symbol="TEST", timeframe="5m", bar=None,
        decision={"decision": "enter_long"},
    )
    assert storage.attach_risk_result("c1", {"decision": "approve", "stage": "gate"}) == "ok"

    storage.save_paper_trade({
        "trade_id": "t1", "candidate_id": "c1", "symbol": "TEST", "timeframe": "5m",
        "direction": "bullish", "size": 1, "order_type": "market",
        "entry_price": 100.0, "stop_loss": 95.0, "targets": [110.0],
        "status": "open", "opened_at": "2026-08-13T10:00:00Z", "fill_price": 100.0,
    })

    assert storage.attach_risk_result("c1", {"decision": "modify", "stage": "size"}) == "locked"
    # unchanged by the rejected attempt
    candidate = storage.get_candidate_by_id("c1")
    assert candidate["risk"]["decision"] == "approve"


def test_attach_execution_result_appends_history_without_losing_prior_entries(fresh_storage):
    storage = fresh_storage
    storage.save_candidate(
        candidate_id="c1", symbol="TEST", timeframe="5m", bar=None,
        decision={"decision": "enter_long"},
    )
    storage.attach_execution_result("c1", {"status": "invalid", "validation_error": "bad"})
    storage.attach_execution_result("c1", {"status": "planned", "entry_price": 100.5})

    candidate = storage.get_candidate_by_id("c1")
    assert candidate["execution"]["status"] == "planned"
    assert [e["status"] for e in candidate["execution_history"]] == ["invalid", "planned"]


def test_init_db_migration_is_idempotent_on_an_existing_db(fresh_storage):
    """Calling init_db() twice (e.g. app restart) must not error even
    though the ALTER TABLE columns already exist from the first call."""
    storage = fresh_storage
    storage.init_db()  # fixture already called it once; a second call must not raise
    storage.save_candidate(
        candidate_id="c1", symbol="TEST", timeframe="5m", bar=None,
        decision={"decision": "no_trade"},
    )
    assert storage.get_candidate_by_id("c1")["risk_history"] == []


# ---------------------------------------------------------------------------
# Tier 3.2: paper_trades migration (order_submitted_at/opened_at_processed/
# closed_at_processed) and cancel_trade()
# ---------------------------------------------------------------------------

def test_paper_trades_migration_is_idempotent_on_an_existing_db(fresh_storage):
    """Same idempotent-ALTER-TABLE guarantee as the trade_candidates
    migration above, but for the three Tier 3.2 paper_trades columns
    -- a second init_db() call (e.g. app restart) must not raise even
    though the columns already exist from the first call."""
    storage = fresh_storage
    storage.init_db()  # fixture already called it once; a second call must not raise
    storage.save_paper_trade({
        "trade_id": "t1", "candidate_id": "c1", "symbol": "TEST", "timeframe": "5m",
        "direction": "bullish", "size": 1, "order_type": "market",
        "entry_price": 100.0, "stop_loss": 95.0, "targets": [110.0],
        "status": "pending_fill", "order_submitted_at": "2026-08-13T10:00:00Z",
    })
    trade = storage.get_trade_by_id("t1")
    assert trade["order_submitted_at"] == "2026-08-13T10:00:00Z"
    assert trade["opened_at_processed"] is None
    assert trade["closed_at_processed"] is None


def test_cancel_trade_marks_pending_order_cancelled(fresh_storage):
    storage = fresh_storage
    storage.save_paper_trade({
        "trade_id": "t1", "candidate_id": "c1", "symbol": "TEST", "timeframe": "5m",
        "direction": "bullish", "size": 1, "order_type": "limit",
        "entry_price": 100.0, "stop_loss": 95.0, "targets": [110.0],
        "status": "pending_fill", "order_submitted_at": "2026-08-13T10:00:00Z",
    })

    result = storage.cancel_trade("t1", cancelled_at="2026-08-13T11:00:00Z", reason="expired_unfilled")
    assert result is True

    trade = storage.get_trade_by_id("t1")
    assert trade["status"] == "cancelled"
    assert trade["exit_reason"] == "expired_unfilled"
    assert trade["closed_at"] == "2026-08-13T11:00:00Z"
    # A cancelled order was never filled -- nothing to realize a price/P&L against.
    assert trade["exit_price"] is None
    assert trade["pnl_usd"] is None


def test_cancel_trade_is_a_noop_on_a_trade_that_already_filled(fresh_storage):
    """Idempotency/race guard, same pattern as close_trade only
    affecting 'open' rows: an order that filled in the meantime (e.g.
    a duplicate/retried bar delivery) must not be retroactively
    cancelled out from under an already-open position."""
    storage = fresh_storage
    storage.save_paper_trade({
        "trade_id": "t1", "candidate_id": "c1", "symbol": "TEST", "timeframe": "5m",
        "direction": "bullish", "size": 1, "order_type": "market",
        "entry_price": 100.0, "stop_loss": 95.0, "targets": [110.0],
        "status": "open", "opened_at": "2026-08-13T10:05:00Z", "fill_price": 100.25,
    })

    result = storage.cancel_trade("t1", cancelled_at="2026-08-13T11:00:00Z", reason="expired_unfilled")
    assert result is False
    assert storage.get_trade_by_id("t1")["status"] == "open"


def test_cancel_trade_returns_false_for_unknown_trade_id(fresh_storage):
    storage = fresh_storage
    assert storage.cancel_trade("no-such-trade", cancelled_at="2026-08-13T11:00:00Z", reason="expired_unfilled") is False


# ---------------------------------------------------------------------------
# Tier 3.3: get_open_or_pending_trade_count() and open_trade_if_room() —
# account-wide, atomic position-limit enforcement
# ---------------------------------------------------------------------------

def _trade_dict(trade_id, candidate_id, symbol, timeframe="5m"):
    return {
        "trade_id": trade_id, "candidate_id": candidate_id, "symbol": symbol, "timeframe": timeframe,
        "direction": "bullish", "size": 1, "order_type": "market",
        "entry_price": 100.0, "stop_loss": 95.0, "targets": [110.0],
        "status": "pending_fill", "order_submitted_at": "2026-08-13T10:00:00Z",
    }


def test_get_open_or_pending_trade_count_spans_every_symbol(fresh_storage):
    storage = fresh_storage
    storage.save_paper_trade(_trade_dict("t1", "c1", "TEST"))
    storage.save_paper_trade(_trade_dict("t2", "c2", "OTHER"))
    assert storage.get_open_or_pending_trade_count() == 2


def test_get_open_or_pending_trade_count_excludes_closed_and_cancelled(fresh_storage):
    storage = fresh_storage
    storage.save_paper_trade(_trade_dict("t1", "c1", "TEST"))
    storage.update_trade_fill("t1", fill_price=100.0, opened_at="2026-08-13T10:05:00Z")  # pending_fill -> open
    storage.close_trade("t1", exit_price=100.0, exit_reason="target_hit", pnl_usd=0.0, closed_at="2026-08-13T11:00:00Z")
    storage.save_paper_trade(_trade_dict("t2", "c2", "TEST"))
    storage.cancel_trade("t2", cancelled_at="2026-08-13T11:00:00Z", reason="expired_unfilled")
    assert storage.get_open_or_pending_trade_count() == 0


def test_open_trade_if_room_opens_when_under_capacity(fresh_storage):
    storage = fresh_storage
    status, result = storage.open_trade_if_room(_trade_dict("t1", "c1", "TEST"), max_open_positions=1)
    assert status == "opened"
    assert result["trade_id"] == "t1"
    assert storage.get_trade_by_id("t1")["status"] == "pending_fill"


def test_open_trade_if_room_at_capacity_inserts_nothing(fresh_storage):
    """The core atomicity guarantee: when capacity is full, the row
    must never be inserted at all -- not inserted-then-rejected."""
    storage = fresh_storage
    storage.open_trade_if_room(_trade_dict("t1", "c1", "TEST"), max_open_positions=1)

    status, result = storage.open_trade_if_room(_trade_dict("t2", "c2", "OTHER"), max_open_positions=1)
    assert status == "at_capacity"
    assert result is None
    assert storage.get_trade_by_id("t2") is None
    assert storage.get_open_or_pending_trade_count() == 1


def test_open_trade_if_room_capacity_check_is_account_wide_not_per_symbol(fresh_storage):
    """Regression test for the actual second-review finding: a second
    DIFFERENT symbol must be blocked by the same account-wide cap, not
    treated as having its own separate limit."""
    storage = fresh_storage
    status1, _ = storage.open_trade_if_room(_trade_dict("t1", "c1", "MNQ1!"), max_open_positions=1)
    status2, _ = storage.open_trade_if_room(_trade_dict("t2", "c2", "ES1!"), max_open_positions=1)
    assert status1 == "opened"
    assert status2 == "at_capacity"


def test_open_trade_if_room_idempotent_returns_original_trade(fresh_storage):
    storage = fresh_storage
    status1, trade1 = storage.open_trade_if_room(_trade_dict("t1", "c1", "TEST"), max_open_positions=5)
    status2, trade2 = storage.open_trade_if_room(_trade_dict("t2-should-be-ignored", "c1", "TEST"), max_open_positions=5)
    assert status1 == "opened"
    assert status2 == "already_exists"
    assert trade2["trade_id"] == "t1"  # the ORIGINAL trade, never a second one
    assert storage.get_trade_by_id("t2-should-be-ignored") is None


def test_open_trade_if_room_idempotency_wins_over_capacity(fresh_storage):
    """Same guarantee as the paper_trades-level test: idempotency must
    be checked (and win) BEFORE the capacity check, so re-submitting
    the same candidate's trade never gets rejected just because the
    account happens to be full -- of that very trade."""
    storage = fresh_storage
    storage.open_trade_if_room(_trade_dict("t1", "c1", "TEST"), max_open_positions=1)
    status, trade = storage.open_trade_if_room(_trade_dict("t2-ignored", "c1", "TEST"), max_open_positions=1)
    assert status == "already_exists"
    assert trade["trade_id"] == "t1"


# ---------------------------------------------------------------------------
# Tier 3.23 (fifth external review): rowid-based candidate access, the
# no-peeking boundary app.experiments now uses instead of created_at.
# ---------------------------------------------------------------------------

def _save_min_candidate(storage, candidate_id, symbol="TEST", timeframe="5m"):
    storage.save_candidate(
        candidate_id=candidate_id, symbol=symbol, timeframe=timeframe,
        bar=None, decision={"decision": "no_trade"},
    )


def test_get_max_candidate_rowid_zero_when_none_exist(fresh_storage):
    storage = fresh_storage
    assert storage.get_max_candidate_rowid("TEST", "5m") == 0


def test_get_max_candidate_rowid_tracks_insertion_count(fresh_storage):
    storage = fresh_storage
    _save_min_candidate(storage, "c1")
    _save_min_candidate(storage, "c2")
    assert storage.get_max_candidate_rowid("TEST", "5m") == 2
    _save_min_candidate(storage, "c3")
    assert storage.get_max_candidate_rowid("TEST", "5m") == 3


def test_get_max_candidate_rowid_scoped_to_symbol_and_timeframe(fresh_storage):
    storage = fresh_storage
    _save_min_candidate(storage, "c1", symbol="TEST", timeframe="5m")
    _save_min_candidate(storage, "c2", symbol="OTHER", timeframe="5m")
    assert storage.get_max_candidate_rowid("OTHER", "5m") == 2  # rowid is table-wide, not per-scope
    assert storage.get_max_candidate_rowid("NEVER-SEEN", "5m") == 0


def test_get_candidates_after_rowid_excludes_at_and_before_watermark(fresh_storage):
    storage = fresh_storage
    _save_min_candidate(storage, "c1")
    watermark = storage.get_max_candidate_rowid("TEST", "5m")
    _save_min_candidate(storage, "c2")
    _save_min_candidate(storage, "c3")

    after = storage.get_candidates_after_rowid("TEST", "5m", watermark)
    assert [c["candidate_id"] for c in after] == ["c2", "c3"]
    assert all(c["rowid"] > watermark for c in after)


def test_get_candidates_after_rowid_ignores_created_at(fresh_storage):
    """The exact property app.experiments relies on: rowid order, not
    a created_at value, decides what's "after" the watermark -- a
    candidate inserted before the watermark but backdated to claim a
    future created_at must still be excluded."""
    storage = fresh_storage
    _save_min_candidate(storage, "c1")
    watermark = storage.get_max_candidate_rowid("TEST", "5m")
    conn = storage.get_connection()
    conn.execute("UPDATE trade_candidates SET created_at = '2099-01-01 00:00:00' WHERE candidate_id = 'c1'")
    conn.commit()
    conn.close()

    after = storage.get_candidates_after_rowid("TEST", "5m", watermark)
    assert after == []


def test_count_candidates_after_rowid_matches_get_candidates_after_rowid_length(fresh_storage):
    storage = fresh_storage
    _save_min_candidate(storage, "c1")
    watermark = storage.get_max_candidate_rowid("TEST", "5m")
    _save_min_candidate(storage, "c2")
    _save_min_candidate(storage, "c3")
    assert storage.count_candidates_after_rowid("TEST", "5m", watermark) == 2
    assert storage.count_candidates_after_rowid("TEST", "5m", watermark) == len(
        storage.get_candidates_after_rowid("TEST", "5m", watermark)
    )


def test_get_candidates_page_first_page_starts_at_zero(fresh_storage):
    storage = fresh_storage
    _save_min_candidate(storage, "c1")
    _save_min_candidate(storage, "c2")
    page = storage.get_candidates_page("TEST", "5m", after_rowid=0, limit=200)
    assert [c["candidate_id"] for c in page["items"]] == ["c1", "c2"]
    assert page["count"] == 2
    assert page["has_more"] is False
    assert page["next_cursor"] is None


def test_get_candidates_page_paginates_forward_with_no_gap_or_duplicate(fresh_storage):
    storage = fresh_storage
    for i in range(5):
        _save_min_candidate(storage, f"c{i}")

    seen = []
    cursor = 0
    for _ in range(10):  # generous loop bound; must terminate well before this
        page = storage.get_candidates_page("TEST", "5m", after_rowid=cursor, limit=2)
        seen.extend(c["candidate_id"] for c in page["items"])
        if not page["has_more"]:
            assert page["next_cursor"] is None
            break
        cursor = page["next_cursor"]
    else:
        pytest.fail("pagination never terminated (has_more stayed true)")

    assert seen == ["c0", "c1", "c2", "c3", "c4"]  # every row exactly once, in order


def test_get_candidates_page_has_more_true_when_rows_remain(fresh_storage):
    storage = fresh_storage
    _save_min_candidate(storage, "c1")
    _save_min_candidate(storage, "c2")
    _save_min_candidate(storage, "c3")
    page = storage.get_candidates_page("TEST", "5m", after_rowid=0, limit=2)
    assert [c["candidate_id"] for c in page["items"]] == ["c1", "c2"]
    assert page["has_more"] is True
    watermark = storage.get_max_candidate_rowid("TEST", "5m")
    # next_cursor must be the rowid of the last returned item (c2), not c3's
    assert page["next_cursor"] == page["items"][-1]["rowid"] < watermark


def test_get_candidates_page_a_concurrent_insert_after_a_page_is_fetched_does_not_shift_it(fresh_storage):
    """The property real cursor pagination needs that DESC+LIMIT never
    had: a page already returned to a caller must not change meaning
    when new rows arrive later. Inserting c3 after paging through
    c1/c2 must not appear in a page re-fetched with the same cursor."""
    storage = fresh_storage
    _save_min_candidate(storage, "c1")
    _save_min_candidate(storage, "c2")
    page = storage.get_candidates_page("TEST", "5m", after_rowid=0, limit=200)
    assert [c["candidate_id"] for c in page["items"]] == ["c1", "c2"]
    cursor_after_page_one = storage.get_max_candidate_rowid("TEST", "5m")

    _save_min_candidate(storage, "c3")  # arrives after the page above was already read

    # Re-fetching the SAME first page (after_rowid=0) is unaffected in its first two items --
    # a real cursor walk continuing from cursor_after_page_one only ever sees c3, never a
    # reshuffled view of c1/c2.
    next_page = storage.get_candidates_page("TEST", "5m", after_rowid=cursor_after_page_one, limit=200)
    assert [c["candidate_id"] for c in next_page["items"]] == ["c3"]
    assert next_page["has_more"] is False


def test_get_candidates_page_scoped_to_symbol_and_timeframe(fresh_storage):
    storage = fresh_storage
    _save_min_candidate(storage, "c1", symbol="TEST", timeframe="5m")
    _save_min_candidate(storage, "c2", symbol="OTHER", timeframe="5m")
    page = storage.get_candidates_page("TEST", "5m", after_rowid=0, limit=200)
    assert [c["candidate_id"] for c in page["items"]] == ["c1"]


def test_experiments_migration_backfills_registered_watermark_rowid(fresh_storage):
    """Simulates a Tier 3.20-era experiment row (no
    registered_watermark_rowid, the column's DEFAULT 0) alongside real
    candidates, then re-runs init_db() -- the backfill must reconstruct
    a sensible watermark from created_at/registered_at, the best
    approximation available for data that predates rowid-based
    tracking (see init_db()'s migration comment)."""
    storage = fresh_storage
    _save_min_candidate(storage, "before-1")  # rowid 1, will predate registered_at below
    conn = storage.get_connection()
    conn.execute(
        "UPDATE trade_candidates SET created_at = '2026-01-01 00:00:00' WHERE candidate_id = 'before-1'"
    )
    conn.execute(
        """
        INSERT INTO experiments
            (experiment_id, symbol, timeframe, hypothesis, locked_config_json,
             target_metrics_json, stopping_rule_json, direction_source, registered_at)
        VALUES ('exp1', 'TEST', '5m', 'h', '{}', '{}', '{}', 'coordinator', '2026-06-01 00:00:00')
        """
    )
    conn.commit()
    conn.close()

    storage.init_db()  # re-run the migration against the same DB

    experiment = storage.get_experiment_by_id("exp1")
    assert experiment["registered_watermark_rowid"] == 1  # "before-1" (created before registered_at) counted
