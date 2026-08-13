"""
Minimal SQLite storage for market_state events.

Deliberately simple for Sprint 1: one table, one index, idempotent
inserts keyed on event_id (the Pine script already guarantees this is
deterministic per symbol+timeframe+bar-close, so INSERT OR IGNORE is
enough to dedupe retried/duplicate webhook deliveries).

Sprint 3.5: DB_PATH is now overridable via the DB_PATH environment
variable, so it can point at a Railway Volume mount (e.g. /data) —
without a persistent volume, Railway wipes the local filesystem on
every redeploy, taking the SQLite file with it.
"""

import json
import os
import sqlite3
from pathlib import Path
from typing import Optional

from app.models import MarketStatePayload

_DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "market_state.db"
DB_PATH = Path(os.environ.get("DB_PATH", str(_DEFAULT_DB_PATH)))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS market_state (
    event_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    received_at TEXT NOT NULL DEFAULT (datetime('now')),
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_symbol_timeframe_ts
    ON market_state (symbol, timeframe, timestamp DESC);

CREATE TABLE IF NOT EXISTS agent_opinions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    opinion_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_symbol_timeframe_ts
    ON agent_opinions (agent, symbol, timeframe, timestamp DESC);

CREATE TABLE IF NOT EXISTS coordinator_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    decision_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_symbol_timeframe_ts
    ON coordinator_decisions (symbol, timeframe, timestamp DESC);

CREATE TABLE IF NOT EXISTS trade_candidates (
    candidate_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    bar_json TEXT,
    decision_json TEXT NOT NULL,
    risk_json TEXT,
    execution_json TEXT,
    risk_history_json TEXT,
    execution_history_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_candidates_symbol_timeframe_created
    ON trade_candidates (symbol, timeframe, created_at DESC);

CREATE TABLE IF NOT EXISTS paper_trades (
    trade_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    direction TEXT NOT NULL,
    size INTEGER NOT NULL,
    order_type TEXT NOT NULL,
    entry_price REAL NOT NULL,
    stop_loss REAL NOT NULL,
    targets_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    opened_at TEXT,
    fill_price REAL,
    closed_at TEXT,
    exit_price REAL,
    exit_reason TEXT,
    pnl_usd REAL
);
CREATE INDEX IF NOT EXISTS idx_trades_symbol_timeframe_status
    ON paper_trades (symbol, timeframe, status);
CREATE INDEX IF NOT EXISTS idx_trades_candidate_id
    ON paper_trades (candidate_id);
"""


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_connection()
    try:
        conn.executescript(_SCHEMA)
        # Tier 3.1: trade_candidates predates this migration on any
        # already-deployed DB (Railway's volume) — CREATE TABLE IF NOT
        # EXISTS above only applies to brand-new databases, so existing
        # tables need these two columns added explicitly. Idempotent:
        # SQLite raises "duplicate column name" if they're already
        # there, which is exactly the "already migrated" case.
        for column in ("risk_history_json", "execution_history_json"):
            try:
                conn.execute(f"ALTER TABLE trade_candidates ADD COLUMN {column} TEXT")
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e):
                    raise
        conn.commit()
    finally:
        conn.close()


def save_event(payload: MarketStatePayload) -> bool:
    """Store a validated payload. Returns True if newly inserted,
    False if it was a duplicate event_id (already stored)."""
    conn = get_connection()
    try:
        # Never persist the secret.
        stored = payload.model_dump(exclude={"secret"})
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO market_state
                (event_id, symbol, timeframe, timestamp, payload_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                payload.event_id,
                payload.symbol,
                payload.timeframe,
                payload.timestamp,
                json.dumps(stored),
            ),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def get_latest(symbol: str, timeframe: str) -> Optional[dict]:
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT payload_json FROM market_state
            WHERE symbol = ? AND timeframe = ?
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (symbol, timeframe),
        ).fetchone()
        return json.loads(row["payload_json"]) if row else None
    finally:
        conn.close()


def get_recent(symbol: str, timeframe: str, limit: int = 20) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT payload_json FROM market_state
            WHERE symbol = ? AND timeframe = ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (symbol, timeframe, limit),
        ).fetchall()
        return [json.loads(r["payload_json"]) for r in rows]
    finally:
        conn.close()


def get_by_event_id(event_id: str) -> Optional[dict]:
    """Tier 3.1 (causal integrity). Fetch the EXACT bar a webhook
    delivery stored, by its own event_id — as opposed to get_latest(),
    which returns whatever the newest row happens to be at the moment
    it's called. The auto-analysis background task anchors itself to
    this instead of "latest" so a second bar arriving while the task
    is queued/running can never make it analyze or freeze a different
    bar than the one that actually triggered it."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT payload_json FROM market_state WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        return json.loads(row["payload_json"]) if row else None
    finally:
        conn.close()


def get_recent_as_of(symbol: str, timeframe: str, as_of_timestamp: str, limit: int = 20) -> list[dict]:
    """Same as get_recent(), but bounded to bars whose own timestamp is
    at or before as_of_timestamp — Tier 3.1's other half of anchoring:
    even if newer bars have landed by the time this actually runs, the
    Analysis window used stays exactly what it would have been at the
    anchor moment, never accidentally peeking at future bars."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT payload_json FROM market_state
            WHERE symbol = ? AND timeframe = ? AND timestamp <= ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (symbol, timeframe, as_of_timestamp, limit),
        ).fetchall()
        return [json.loads(r["payload_json"]) for r in rows]
    finally:
        conn.close()


def save_opinion(agent: str, symbol: str, timeframe: str, timestamp: str, opinion: dict) -> None:
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO agent_opinions (agent, symbol, timeframe, timestamp, opinion_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (agent, symbol, timeframe, timestamp, json.dumps(opinion)),
        )
        conn.commit()
    finally:
        conn.close()


def get_latest_opinion(agent: str, symbol: str, timeframe: str) -> Optional[dict]:
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT opinion_json FROM agent_opinions
            WHERE agent = ? AND symbol = ? AND timeframe = ?
            ORDER BY timestamp DESC, id DESC
            LIMIT 1
            """,
            (agent, symbol, timeframe),
        ).fetchone()
        return json.loads(row["opinion_json"]) if row else None
    finally:
        conn.close()


def save_decision(symbol: str, timeframe: str, timestamp: str, decision: dict) -> None:
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO coordinator_decisions (symbol, timeframe, timestamp, decision_json)
            VALUES (?, ?, ?, ?)
            """,
            (symbol, timeframe, timestamp, json.dumps(decision)),
        )
        conn.commit()
    finally:
        conn.close()


def get_last_opinion_timestamps(symbol: str) -> dict:
    """Returns the created_at time of the most recent opinion for
    each agent, keyed by agent name. Used by the system status
    endpoint — doesn't care about symbol/timeframe matching exactly,
    just wants a quick health signal per agent."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT agent, MAX(created_at) as last_run
            FROM agent_opinions
            WHERE symbol = ?
            GROUP BY agent
            """,
            (symbol,),
        ).fetchall()
        return {r["agent"]: r["last_run"] for r in rows}
    finally:
        conn.close()


def get_last_webhook_received(symbol: str) -> Optional[str]:
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT MAX(received_at) as last_received
            FROM market_state
            WHERE symbol = ?
            """,
            (symbol,),
        ).fetchone()
        return row["last_received"] if row else None
    finally:
        conn.close()


def get_bar_at_or_before(symbol: str, timeframe: str, timestamp: str) -> Optional[dict]:
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT payload_json FROM market_state
            WHERE symbol = ? AND timeframe = ? AND timestamp <= ?
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (symbol, timeframe, timestamp),
        ).fetchone()
        return json.loads(row["payload_json"]) if row else None
    finally:
        conn.close()


def get_bar_at_or_after(symbol: str, timeframe: str, timestamp: str) -> Optional[dict]:
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT payload_json FROM market_state
            WHERE symbol = ? AND timeframe = ? AND timestamp >= ?
            ORDER BY timestamp ASC
            LIMIT 1
            """,
            (symbol, timeframe, timestamp),
        ).fetchone()
        return json.loads(row["payload_json"]) if row else None
    finally:
        conn.close()


def get_recent_decisions(symbol: str, timeframe: str, limit: int = 20) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT decision_json FROM coordinator_decisions
            WHERE symbol = ? AND timeframe = ?
            ORDER BY timestamp DESC, id DESC
            LIMIT ?
            """,
            (symbol, timeframe, limit),
        ).fetchall()
        return [json.loads(r["decision_json"]) for r in rows]
    finally:
        conn.close()


def get_recent_opinions(agent: str, symbol: str, timeframe: str, limit: int = 20) -> list[dict]:
    """Historical opinions for one agent — unlike get_latest_opinion,
    returns more than just the newest row. Used for after-the-fact
    investigation (e.g. did the LLM actually re-run each time, or did
    the same opinion get re-saved) since there's no other way to see
    what an agent said at a specific past moment."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT opinion_json FROM agent_opinions
            WHERE agent = ? AND symbol = ? AND timeframe = ?
            ORDER BY timestamp DESC, id DESC
            LIMIT ?
            """,
            (agent, symbol, timeframe, limit),
        ).fetchall()
        return [json.loads(r["opinion_json"]) for r in rows]
    finally:
        conn.close()


def delete_market_state_event(event_id: str) -> bool:
    """Deletes a single market_state row by its event_id. Returns True
    if a row was actually deleted, False if no row matched."""
    conn = get_connection()
    try:
        cur = conn.execute("DELETE FROM market_state WHERE event_id = ?", (event_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def wipe_all_data() -> dict:
    """Deletes every row from market_state, agent_opinions,
    coordinator_decisions, trade_candidates, and paper_trades.
    Irreversible — used once to clear test/synthetic data before a
    real trading session starts. Returns the number of rows removed
    from each table."""
    conn = get_connection()
    try:
        counts = {}
        for table in (
            "market_state",
            "agent_opinions",
            "coordinator_decisions",
            "trade_candidates",
            "paper_trades",
        ):
            cur = conn.execute(f"SELECT COUNT(*) as c FROM {table}")
            counts[table] = cur.fetchone()["c"]
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
        return counts
    finally:
        conn.close()


def save_candidate(
    candidate_id: str,
    symbol: str,
    timeframe: str,
    bar: dict | None,
    decision: dict,
) -> None:
    """Persists a new trade candidate — an atomic, immutable snapshot
    of the bar and the exact opinions/decision it was built from.
    risk_json/execution_json start empty and are filled in later by
    attach_risk_result/attach_execution_result, on this SAME row —
    never a new independent record, so there's exactly one candidate
    per decision moment, not a scattered set of "latest" lookups."""
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO trade_candidates
                (candidate_id, symbol, timeframe, bar_json, decision_json, risk_json, execution_json)
            VALUES (?, ?, ?, ?, ?, NULL, NULL)
            """,
            (
                candidate_id,
                symbol,
                timeframe,
                json.dumps(bar) if bar is not None else None,
                json.dumps(decision),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _row_to_candidate(row: sqlite3.Row) -> dict:
    row_keys = row.keys()
    return {
        "candidate_id": row["candidate_id"],
        "symbol": row["symbol"],
        "timeframe": row["timeframe"],
        "created_at": row["created_at"],
        "bar": json.loads(row["bar_json"]) if row["bar_json"] else None,
        "decision": json.loads(row["decision_json"]),
        "risk": json.loads(row["risk_json"]) if row["risk_json"] else None,
        "execution": json.loads(row["execution_json"]) if row["execution_json"] else None,
        # Tier 3.1: full append-only transition history — "risk"/
        # "execution" above stay the CURRENT stage's result for
        # backward compatibility (dashboard/tests read them as a single
        # opinion), but nothing is ever lost when a later stage's
        # result is attached: the gate opinion is still here after the
        # size opinion lands, every execution attempt is still here
        # even if a retry replaced the current one.
        "risk_history": (
            json.loads(row["risk_history_json"])
            if "risk_history_json" in row_keys and row["risk_history_json"]
            else []
        ),
        "execution_history": (
            json.loads(row["execution_history_json"])
            if "execution_history_json" in row_keys and row["execution_history_json"]
            else []
        ),
    }


def get_latest_candidate(symbol: str, timeframe: str) -> Optional[dict]:
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT * FROM trade_candidates
            WHERE symbol = ? AND timeframe = ?
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (symbol, timeframe),
        ).fetchone()
        return _row_to_candidate(row) if row else None
    finally:
        conn.close()


def get_candidate_by_id(candidate_id: str) -> Optional[dict]:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM trade_candidates WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        return _row_to_candidate(row) if row else None
    finally:
        conn.close()


def get_recent_candidates(symbol: str, timeframe: str, limit: int = 20) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT * FROM trade_candidates
            WHERE symbol = ? AND timeframe = ?
            ORDER BY created_at DESC, rowid DESC
            LIMIT ?
            """,
            (symbol, timeframe, limit),
        ).fetchall()
        return [_row_to_candidate(r) for r in rows]
    finally:
        conn.close()


def _attach_candidate_result(
    candidate_id: str, opinion: dict, current_column: str, history_column: str
) -> str:
    """Shared write-once-after-commit logic for attach_risk_result/
    attach_execution_result — Tier 3.1 (causal integrity, part 2).

    Before a paper trade exists for a candidate, both Risk and
    Execution results are free to be (re-)attached as many times as
    the two-stage gate/size flow (or a retried Execution call) needs —
    each attach APPENDS to that field's history rather than discarding
    the previous one, so e.g. the original gate opinion is still
    visible after the size opinion lands.

    Once a paper trade HAS been opened from this candidate
    (open_trade_from_candidate — see app/paper_trades.py), the trade's
    entry/stop/size is fixed and can never itself be edited. Letting
    Risk or Execution attach a NEW/different opinion after that point
    would make the candidate's risk_json/execution_json describe a
    trade that was never actually taken — the exact "candidate says B,
    committed trade is A" mismatch flagged in the second external
    review. So once committed, this refuses (returns "locked") instead
    of silently overwriting.

    Returns "ok", "not_found", or "locked"."""
    conn = get_connection()
    try:
        committed = conn.execute(
            "SELECT 1 FROM paper_trades WHERE candidate_id = ? LIMIT 1", (candidate_id,)
        ).fetchone()
        if committed is not None:
            return "locked"

        row = conn.execute(
            f"SELECT {history_column} FROM trade_candidates WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        if row is None:
            return "not_found"

        history = json.loads(row[history_column]) if row[history_column] else []
        history.append(opinion)
        conn.execute(
            f"UPDATE trade_candidates SET {current_column} = ?, {history_column} = ? "
            "WHERE candidate_id = ?",
            (json.dumps(opinion), json.dumps(history), candidate_id),
        )
        conn.commit()
        return "ok"
    finally:
        conn.close()


def attach_risk_result(candidate_id: str, risk_opinion: dict) -> str:
    """Writes the Risk evaluation onto the SAME candidate row it was
    evaluated against — not a new record. Returns "ok", "not_found"
    (no such candidate_id — caller should treat that as an error, never
    silently create a new row), or "locked" (a paper trade already
    exists for this candidate — see _attach_candidate_result)."""
    return _attach_candidate_result(candidate_id, risk_opinion, "risk_json", "risk_history_json")


def attach_execution_result(candidate_id: str, execution_opinion: dict) -> str:
    """Same pattern as attach_risk_result, for the Execution plan."""
    return _attach_candidate_result(
        candidate_id, execution_opinion, "execution_json", "execution_history_json"
    )


# ---------------------------------------------------------------------------
# Paper trades — Tier 2.3
# ---------------------------------------------------------------------------

def _row_to_trade(row: sqlite3.Row) -> dict:
    return {
        "trade_id": row["trade_id"],
        "candidate_id": row["candidate_id"],
        "symbol": row["symbol"],
        "timeframe": row["timeframe"],
        "direction": row["direction"],
        "size": row["size"],
        "order_type": row["order_type"],
        "entry_price": row["entry_price"],
        "stop_loss": row["stop_loss"],
        "targets": json.loads(row["targets_json"]),
        "status": row["status"],
        "created_at": row["created_at"],
        "opened_at": row["opened_at"],
        "fill_price": row["fill_price"],
        "closed_at": row["closed_at"],
        "exit_price": row["exit_price"],
        "exit_reason": row["exit_reason"],
        "pnl_usd": row["pnl_usd"],
    }


def save_paper_trade(trade: dict) -> None:
    """Inserts a new paper trade — one row per opened candidate.
    status is "open" (market/ready-now orders, filled immediately) or
    "pending_fill" (a limit order still waiting for price to reach
    it). opened_at/fill_price are set now for an immediate fill, NULL
    for pending_fill — process_new_bar() fills those in later."""
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO paper_trades
                (trade_id, candidate_id, symbol, timeframe, direction, size,
                 order_type, entry_price, stop_loss, targets_json, status,
                 opened_at, fill_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade["trade_id"],
                trade["candidate_id"],
                trade["symbol"],
                trade["timeframe"],
                trade["direction"],
                trade["size"],
                trade["order_type"],
                trade["entry_price"],
                trade["stop_loss"],
                json.dumps(trade["targets"]),
                trade["status"],
                trade.get("opened_at"),
                trade.get("fill_price"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_trade_by_candidate_id(candidate_id: str) -> Optional[dict]:
    """The idempotency check — a candidate must never spawn more than
    one paper trade, however many times open_trade_from_candidate()
    is called for it (e.g. a re-run of Risk's size stage)."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM paper_trades WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        return _row_to_trade(row) if row else None
    finally:
        conn.close()


def get_trade_by_id(trade_id: str) -> Optional[dict]:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM paper_trades WHERE trade_id = ?",
            (trade_id,),
        ).fetchone()
        return _row_to_trade(row) if row else None
    finally:
        conn.close()


def get_open_or_pending_trades(symbol: str, timeframe: str) -> list[dict]:
    """Trades still "live" — either waiting to fill or already open.
    This is what the Risk gate's open-position check now counts,
    replacing the old hand-updated CURRENT_OPEN_POSITIONS env var."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT * FROM paper_trades
            WHERE symbol = ? AND timeframe = ? AND status IN ('pending_fill', 'open')
            ORDER BY created_at DESC
            """,
            (symbol, timeframe),
        ).fetchall()
        return [_row_to_trade(r) for r in rows]
    finally:
        conn.close()


def get_recent_trades(symbol: str, timeframe: str, limit: int = 20) -> list[dict]:
    """Closed trades, newest first — the realized P&L history."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT * FROM paper_trades
            WHERE symbol = ? AND timeframe = ? AND status = 'closed'
            ORDER BY closed_at DESC
            LIMIT ?
            """,
            (symbol, timeframe, limit),
        ).fetchall()
        return [_row_to_trade(r) for r in rows]
    finally:
        conn.close()


def get_all_closed_trades_chronological() -> list[dict]:
    """ALL closed paper trades across every symbol/timeframe, oldest
    first — Tier 2.10 (account-level risk controls) needs this for
    account-wide drawdown/daily-loss math, which is deliberately not
    scoped to one symbol the same way get_recent_trades() is: the
    account's risk budget (ACCOUNT_BALANCE/MAX_DRAWDOWN) is a single
    account-wide number regardless of how many symbols end up trading
    against it."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM paper_trades WHERE status = 'closed' ORDER BY closed_at ASC"
        ).fetchall()
        return [_row_to_trade(r) for r in rows]
    finally:
        conn.close()


def update_trade_fill(trade_id: str, fill_price: float, opened_at: str) -> bool:
    """A pending_fill limit order's price has been reached — marks it
    open at the limit price (standard paper-trading assumption: filled
    exactly at the limit, no slippage modeled)."""
    conn = get_connection()
    try:
        cur = conn.execute(
            """
            UPDATE paper_trades
            SET status = 'open', fill_price = ?, opened_at = ?
            WHERE trade_id = ? AND status = 'pending_fill'
            """,
            (fill_price, opened_at, trade_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def close_trade(trade_id: str, exit_price: float, exit_reason: str, pnl_usd: float, closed_at: str) -> bool:
    """Realizes P&L and closes an open trade. Only affects rows still
    'open' — a trade already closed (e.g. a duplicate bar delivery
    processed twice) is left untouched rather than double-closed."""
    conn = get_connection()
    try:
        cur = conn.execute(
            """
            UPDATE paper_trades
            SET status = 'closed', exit_price = ?, exit_reason = ?, pnl_usd = ?, closed_at = ?
            WHERE trade_id = ? AND status = 'open'
            """,
            (exit_price, exit_reason, pnl_usd, closed_at, trade_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
