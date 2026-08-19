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
    order_submitted_at TEXT,
    opened_at TEXT,
    opened_at_processed TEXT,
    fill_price REAL,
    closed_at TEXT,
    closed_at_processed TEXT,
    exit_price REAL,
    exit_reason TEXT,
    pnl_usd REAL,
    provenance TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_symbol_timeframe_status
    ON paper_trades (symbol, timeframe, status);
CREATE INDEX IF NOT EXISTS idx_trades_candidate_id
    ON paper_trades (candidate_id);

CREATE TABLE IF NOT EXISTS llm_call_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    called_at TEXT NOT NULL DEFAULT (datetime('now')),
    agent TEXT NOT NULL,
    model TEXT NOT NULL,
    trigger_context TEXT,
    success INTEGER NOT NULL,
    error_message TEXT,
    latency_ms REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_creation_input_tokens INTEGER,
    cache_read_input_tokens INTEGER,
    web_search_requests INTEGER,
    estimated_cost_usd REAL
);
CREATE INDEX IF NOT EXISTS idx_llm_call_log_agent_called_at
    ON llm_call_log (agent, called_at DESC);

CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    hypothesis TEXT NOT NULL,
    locked_config_json TEXT NOT NULL,
    target_metrics_json TEXT NOT NULL,
    stopping_rule_json TEXT NOT NULL,
    direction_source TEXT NOT NULL,
    registered_at TEXT NOT NULL DEFAULT (datetime('now')),
    status TEXT NOT NULL DEFAULT 'active',
    resolved_at TEXT,
    resolution_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_experiments_symbol_timeframe
    ON experiments (symbol, timeframe, registered_at DESC);
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
        # Tier 3.2: event-time trade lifecycle timestamps, distinct
        # from the server-processing ones (created_at already existed
        # and stays the server-insert time; these three are new).
        for column in ("order_submitted_at", "opened_at_processed", "closed_at_processed"):
            try:
                conn.execute(f"ALTER TABLE paper_trades ADD COLUMN {column} TEXT")
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e):
                    raise
        # Tier 3.22 (fifth external review — "the manual test trade must
        # be tagged and excluded from system performance immediately"):
        # provenance distinguishes trades opened by the AUTO_EXECUTE_
        # ENABLED-gated background task ("auto_policy") from trades
        # opened via the manual /agents/risk/evaluate endpoint the
        # dashboard's per-agent "Run" buttons hit ("manual_dashboard") —
        # see app/paper_trades.open_trade_from_candidate()'s new
        # required `provenance` parameter, which both call sites in
        # main.py now pass explicitly.
        try:
            conn.execute("ALTER TABLE paper_trades ADD COLUMN provenance TEXT")
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e):
                raise
        # Backfill: any row that predates this migration (provenance
        # IS NULL) can ONLY have come from the manual endpoint — the
        # auto-execute background task is gated by AUTO_EXECUTE_ENABLED,
        # which has been false for this project's entire history to
        # date (repeatedly reconfirmed live via /system/status), so no
        # pre-migration row could possibly have come from that path.
        conn.execute("UPDATE paper_trades SET provenance = 'manual_dashboard' WHERE provenance IS NULL")
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


def get_bars_after(symbol: str, timeframe: str, after_timestamp: str, limit: int = 50) -> list[dict]:
    """Tier 3.10 (ATR-barrier backtest-lite): the walk-FORWARD
    counterpart to get_recent_as_of() — bars strictly AFTER
    after_timestamp, ascending (oldest first), for simulating a
    hypothetical trade bar-by-bar from its entry point forward. Never
    includes the bar at after_timestamp itself, matching the live
    paper-trade engine's convention that a trade can only fill against
    a bar that arrives after the one that triggered it, never the
    triggering bar itself (that bar has already closed by the time
    any decision is made from it — filling "into" it would be
    lookahead bias)."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT payload_json FROM market_state
            WHERE symbol = ? AND timeframe = ? AND timestamp > ?
            ORDER BY timestamp ASC
            LIMIT ?
            """,
            (symbol, timeframe, after_timestamp, limit),
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
    row_keys = row.keys()
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
        # Tier 3.2: order_submitted_at/opened_at/closed_at are EVENT
        # time (the triggering bar's own timestamp) — what the trade
        # lifecycle logic (fills, expiry, P&L, daily-loss bucketing)
        # actually reasons about. created_at above and the two
        # *_processed columns below are server-processing time —
        # operational/debugging data only, never used for trading
        # logic. row_keys guards let this keep working against a row
        # from a DB that hasn't run the Tier 3.2 migration yet.
        "order_submitted_at": row["order_submitted_at"] if "order_submitted_at" in row_keys else None,
        "opened_at": row["opened_at"],
        "opened_at_processed": row["opened_at_processed"] if "opened_at_processed" in row_keys else None,
        "fill_price": row["fill_price"],
        "closed_at": row["closed_at"],
        "closed_at_processed": row["closed_at_processed"] if "closed_at_processed" in row_keys else None,
        "exit_price": row["exit_price"],
        "exit_reason": row["exit_reason"],
        "pnl_usd": row["pnl_usd"],
        # Tier 3.22: "auto_policy" (AUTO_EXECUTE_ENABLED-gated background
        # task) or "manual_dashboard" (the dashboard's per-agent "Run"
        # buttons, via /agents/risk/evaluate) — see init_db()'s migration
        # for how pre-Tier-3.22 rows were backfilled. row_keys guard for
        # the same reason as the other guarded fields above.
        "provenance": row["provenance"] if "provenance" in row_keys else None,
    }


def _insert_paper_trade_row(conn: sqlite3.Connection, trade: dict) -> None:
    """Shared INSERT body for save_paper_trade() and the atomic
    open_trade_if_room() below — same statement, different connection/
    transaction-management wrapped around it."""
    conn.execute(
        """
        INSERT INTO paper_trades
            (trade_id, candidate_id, symbol, timeframe, direction, size,
             order_type, entry_price, stop_loss, targets_json, status,
             order_submitted_at, opened_at, fill_price, provenance)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            trade.get("order_submitted_at"),
            trade.get("opened_at"),
            trade.get("fill_price"),
            # Tier 3.22: no silent default here on purpose — every
            # caller (app/paper_trades.open_trade_from_candidate(), and
            # any test building a trade dict directly) must state its
            # provenance explicitly. A missing value stores NULL rather
            # than guessing, so a bug that forgets to set it is visible
            # in the data, not silently mislabeled as one or the other.
            trade.get("provenance"),
        ),
    )


def save_paper_trade(trade: dict) -> None:
    """Inserts a new paper trade — one row per opened candidate.

    As of Tier 3.2, app/paper_trades.py always passes status=
    "pending_fill" here — even market orders no longer fill
    instantly at candidate-creation time (see that module's
    docstring for why: the anchor bar has already closed by the time
    this runs, so filling "into" it would be lookahead bias).
    order_submitted_at is the EVENT time (the candidate's anchor bar
    timestamp) the order was actually placed at; opened_at/fill_price
    stay NULL until process_new_bar() fills the order against a real
    subsequent bar. This function itself stays generic (inserts
    whatever status/fields it's given) so existing tests that build a
    trade dict directly and pre-set status="open" keep working.

    Tier 3.3: this plain insert is no longer how
    open_trade_from_candidate() actually commits a new trade — that
    now goes through open_trade_if_room() below, which needs the
    idempotency/capacity check and the insert to be one atomic
    transaction. save_paper_trade() is kept for callers (tests, and
    any future caller) that don't need that guarantee and just want to
    insert a fully-formed trade row directly."""
    conn = get_connection()
    try:
        _insert_paper_trade_row(conn, trade)
        conn.commit()
    finally:
        conn.close()


def get_open_or_pending_trade_count() -> int:
    """Tier 3.3: ACCOUNT-WIDE count of trades still live (pending_fill
    or open), across every symbol/timeframe — not scoped like
    get_open_or_pending_trades() below. This is what MAX_OPEN_POSITIONS
    actually gates against now: the second external review's finding
    was that the old per-symbol+timeframe count let two different
    symbols each independently reach "the limit" and open a combined
    total well past it, when the account's position-limit budget is a
    single account-wide number (the same reasoning Tier 2.10 already
    applied to drawdown/daily-loss). This read is advisory only — used
    for Risk's free "gate" pre-check before Execution runs, where no
    trade is being committed yet. The actual enforcement happens
    atomically in open_trade_if_room()."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) as c FROM paper_trades WHERE status IN ('pending_fill', 'open')"
        ).fetchone()
        return row["c"]
    finally:
        conn.close()


def open_trade_if_room(trade: dict, max_open_positions: int) -> tuple[str, Optional[dict]]:
    """Tier 3.3: the single atomic commit point for opening a paper
    trade. Folds BOTH checks open_trade_from_candidate() used to make
    as two separate, non-atomic operations into one BEGIN IMMEDIATE
    transaction:

      - idempotency: does this candidate_id already have a trade?
      - account-wide capacity: are we already at max_open_positions
        live (pending_fill/open) trades, across every symbol/timeframe?

    Both were separately flagged by the second external review as
    race-prone: two near-simultaneous calls for the SAME candidate
    could each pass the idempotency check before either had inserted
    (opening two trades for one candidate); two DIFFERENT candidates
    could each pass the capacity check before either had inserted
    (opening one more position than max_open_positions allows).
    BEGIN IMMEDIATE grabs SQLite's write lock for the whole
    check-then-insert sequence, not just the insert, so a second
    concurrent caller is serialized behind this one rather than racing
    it — it either sees the row this call just committed (idempotency
    catches it) or the capacity this call just consumed (capacity
    catches it), never a stale pre-commit view of either.

    Returns (status, trade):
      - ("opened", trade) — inserted; trade is the dict passed in.
      - ("already_exists", existing_trade) — this candidate_id already
        had a trade; the ORIGINAL trade is returned, never a new one.
      - ("at_capacity", None) — max_open_positions already reached
        account-wide; nothing was inserted."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing_row = conn.execute(
            "SELECT * FROM paper_trades WHERE candidate_id = ?", (trade["candidate_id"],)
        ).fetchone()
        if existing_row is not None:
            conn.execute("ROLLBACK")
            return "already_exists", _row_to_trade(existing_row)

        count = conn.execute(
            "SELECT COUNT(*) as c FROM paper_trades WHERE status IN ('pending_fill', 'open')"
        ).fetchone()["c"]
        if count >= max_open_positions:
            conn.execute("ROLLBACK")
            return "at_capacity", None

        _insert_paper_trade_row(conn, trade)
        conn.execute("COMMIT")
        return "opened", trade
    except Exception:
        conn.execute("ROLLBACK")
        raise
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


def update_trade_fill(
    trade_id: str, fill_price: float, opened_at: str, opened_at_processed: str | None = None
) -> bool:
    """A pending order's price has been reached (limit) or it's a
    market order being filled at the next available bar (Tier 3.2) —
    marks it open. opened_at is EVENT time (the filling bar's own
    timestamp); opened_at_processed is when this server actually ran
    the check, kept only as operational data."""
    conn = get_connection()
    try:
        cur = conn.execute(
            """
            UPDATE paper_trades
            SET status = 'open', fill_price = ?, opened_at = ?, opened_at_processed = ?
            WHERE trade_id = ? AND status = 'pending_fill'
            """,
            (fill_price, opened_at, opened_at_processed, trade_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def close_trade(
    trade_id: str,
    exit_price: float,
    exit_reason: str,
    pnl_usd: float,
    closed_at: str,
    closed_at_processed: str | None = None,
) -> bool:
    """Realizes P&L and closes an open trade. Only affects rows still
    'open' — a trade already closed (e.g. a duplicate bar delivery
    processed twice) is left untouched rather than double-closed.
    closed_at is EVENT time as of Tier 3.2; closed_at_processed is the
    server-processing timestamp, operational data only."""
    conn = get_connection()
    try:
        cur = conn.execute(
            """
            UPDATE paper_trades
            SET status = 'closed', exit_price = ?, exit_reason = ?, pnl_usd = ?,
                closed_at = ?, closed_at_processed = ?
            WHERE trade_id = ? AND status = 'open'
            """,
            (exit_price, exit_reason, pnl_usd, closed_at, closed_at_processed, trade_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def cancel_trade(
    trade_id: str, cancelled_at: str, reason: str, cancelled_at_processed: str | None = None
) -> bool:
    """Tier 3.2: an order still pending_fill after ORDER_EXPIRY_MINUTES
    (event time) is cancelled rather than left resting forever — see
    app/paper_trades.py. Only affects rows still 'pending_fill' — an
    order that filled in the meantime is left alone, same idempotency
    guarantee close_trade already has for 'open'. Reuses the
    closed_at/closed_at_processed/exit_reason columns rather than
    adding yet more — exit_price/pnl_usd stay NULL/unset, since a
    cancelled order was never filled and has nothing to realize a
    price or P&L against."""
    conn = get_connection()
    try:
        cur = conn.execute(
            """
            UPDATE paper_trades
            SET status = 'cancelled', exit_reason = ?, closed_at = ?, closed_at_processed = ?
            WHERE trade_id = ? AND status = 'pending_fill'
            """,
            (reason, cancelled_at, cancelled_at_processed, trade_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tier 3.15: LLM call cost/usage telemetry
# ---------------------------------------------------------------------------

def record_llm_call(
    *,
    agent: str,
    model: str,
    trigger_context: str | None,
    success: bool,
    error_message: str | None,
    latency_ms: float | None,
    input_tokens: int | None,
    output_tokens: int | None,
    cache_creation_input_tokens: int | None,
    cache_read_input_tokens: int | None,
    web_search_requests: int | None,
    estimated_cost_usd: float | None,
) -> None:
    """One row per client.messages.create() call site, success or
    failure -- see app/llm_telemetry.track_llm_call(), which is what
    every agent module actually calls (this function is the storage
    layer underneath it, kept here for the same reason every other
    table's read/write pair lives in this module)."""
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO llm_call_log (
                agent, model, trigger_context, success, error_message, latency_ms,
                input_tokens, output_tokens, cache_creation_input_tokens,
                cache_read_input_tokens, web_search_requests, estimated_cost_usd
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                agent, model, trigger_context, 1 if success else 0, error_message, latency_ms,
                input_tokens, output_tokens, cache_creation_input_tokens,
                cache_read_input_tokens, web_search_requests, estimated_cost_usd,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_llm_call_summary(since: str | None = None) -> dict:
    """Aggregates llm_call_log by agent: call counts (total/success/
    failure), token totals, total estimated cost, average latency, and
    total web_search calls. `since` (ISO timestamp) restricts to calls
    at or after that time; omit for all-time. Returns an `overall`
    rollup plus one entry per agent under `by_agent`."""
    conn = get_connection()
    try:
        where = "WHERE called_at >= ?" if since else ""
        params = (since,) if since else ()
        rows = conn.execute(
            f"""
            SELECT
                agent,
                COUNT(*) AS total_calls,
                SUM(success) AS successful_calls,
                SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS failed_calls,
                COALESCE(SUM(input_tokens), 0) AS total_input_tokens,
                COALESCE(SUM(output_tokens), 0) AS total_output_tokens,
                COALESCE(SUM(web_search_requests), 0) AS total_web_search_requests,
                COALESCE(SUM(estimated_cost_usd), 0.0) AS total_estimated_cost_usd,
                AVG(latency_ms) AS avg_latency_ms
            FROM llm_call_log
            {where}
            GROUP BY agent
            ORDER BY agent
            """,
            params,
        ).fetchall()

        by_agent = {}
        overall = {
            "total_calls": 0, "successful_calls": 0, "failed_calls": 0,
            "total_input_tokens": 0, "total_output_tokens": 0,
            "total_web_search_requests": 0, "total_estimated_cost_usd": 0.0,
        }
        for row in rows:
            entry = {
                "total_calls": row["total_calls"],
                "successful_calls": row["successful_calls"] or 0,
                "failed_calls": row["failed_calls"] or 0,
                "total_input_tokens": row["total_input_tokens"],
                "total_output_tokens": row["total_output_tokens"],
                "total_web_search_requests": row["total_web_search_requests"],
                "total_estimated_cost_usd": round(row["total_estimated_cost_usd"], 4),
                "avg_latency_ms": round(row["avg_latency_ms"], 1) if row["avg_latency_ms"] is not None else None,
            }
            by_agent[row["agent"]] = entry
            overall["total_calls"] += entry["total_calls"]
            overall["successful_calls"] += entry["successful_calls"]
            overall["failed_calls"] += entry["failed_calls"]
            overall["total_input_tokens"] += entry["total_input_tokens"]
            overall["total_output_tokens"] += entry["total_output_tokens"]
            overall["total_web_search_requests"] += entry["total_web_search_requests"]
            overall["total_estimated_cost_usd"] = round(
                overall["total_estimated_cost_usd"] + entry["total_estimated_cost_usd"], 4
            )
        return {"since": since, "overall": overall, "by_agent": by_agent}
    finally:
        conn.close()


def get_recent_llm_calls(limit: int = 50, agent: str | None = None) -> list[dict]:
    """Most recent raw llm_call_log rows, newest first -- for spot-
    checking individual calls (e.g. a recent failure's error_message)
    rather than the aggregated summary above."""
    conn = get_connection()
    try:
        if agent:
            rows = conn.execute(
                """
                SELECT * FROM llm_call_log WHERE agent = ?
                ORDER BY called_at DESC, id DESC LIMIT ?
                """,
                (agent, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM llm_call_log ORDER BY called_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Experiments (Tier 3.20) -- pre-registered hypotheses with a locked config
# and a one-time, append-only resolution. See app/experiments.py for the
# business logic (registration validation, stopping-rule evaluation,
# resolution) -- these are the raw persistence primitives only.
# ---------------------------------------------------------------------------

def _row_to_experiment(row: sqlite3.Row) -> dict:
    return {
        "experiment_id": row["experiment_id"],
        "symbol": row["symbol"],
        "timeframe": row["timeframe"],
        "hypothesis": row["hypothesis"],
        "locked_config": json.loads(row["locked_config_json"]),
        "target_metrics": json.loads(row["target_metrics_json"]),
        "stopping_rule": json.loads(row["stopping_rule_json"]),
        "direction_source": row["direction_source"],
        "registered_at": row["registered_at"],
        "status": row["status"],
        "resolved_at": row["resolved_at"],
        "resolution": json.loads(row["resolution_json"]) if row["resolution_json"] else None,
    }


def save_experiment(
    experiment_id: str,
    symbol: str,
    timeframe: str,
    hypothesis: str,
    locked_config: dict,
    target_metrics: list,
    stopping_rule: dict,
    direction_source: str,
) -> dict:
    """Inserts one new experiment row, status='active'. registered_at
    is stamped by SQLite's own datetime('now') -- the SAME clock
    trade_candidates.created_at is stamped with -- so a later
    `candidate.created_at >= experiment.registered_at` comparison
    (app.experiments._prospective_candidates) compares two timestamps
    from the same clock, never the app server's local clock against
    SQLite's."""
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO experiments
                (experiment_id, symbol, timeframe, hypothesis, locked_config_json,
                 target_metrics_json, stopping_rule_json, direction_source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                experiment_id, symbol, timeframe, hypothesis,
                json.dumps(locked_config), json.dumps(target_metrics),
                json.dumps(stopping_rule), direction_source,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return get_experiment_by_id(experiment_id)


def get_experiment_by_id(experiment_id: str) -> Optional[dict]:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM experiments WHERE experiment_id = ?", (experiment_id,)
        ).fetchone()
        return _row_to_experiment(row) if row else None
    finally:
        conn.close()


def get_experiments(symbol: str | None = None, timeframe: str | None = None) -> list[dict]:
    """Every registered experiment, newest first -- append-only, so
    this is the complete history, not a "latest" view. Optionally
    filtered by symbol/timeframe."""
    conn = get_connection()
    try:
        if symbol and timeframe:
            rows = conn.execute(
                "SELECT * FROM experiments WHERE symbol = ? AND timeframe = ? ORDER BY registered_at DESC, rowid DESC",
                (symbol, timeframe),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM experiments ORDER BY registered_at DESC, rowid DESC"
            ).fetchall()
        return [_row_to_experiment(r) for r in rows]
    finally:
        conn.close()


def resolve_experiment(experiment_id: str, resolution: dict) -> Optional[dict]:
    """Write-once: the UPDATE only matches a row that's still
    status='active', so calling this twice (e.g. a retried request)
    never overwrites an existing resolution_json -- the second call is
    a no-op and the original resolution is returned untouched. Returns
    None if experiment_id doesn't exist at all."""
    conn = get_connection()
    try:
        conn.execute(
            """
            UPDATE experiments
            SET status = 'resolved', resolved_at = datetime('now'), resolution_json = ?
            WHERE experiment_id = ? AND status = 'active'
            """,
            (json.dumps(resolution), experiment_id),
        )
        conn.commit()
    finally:
        conn.close()
    return get_experiment_by_id(experiment_id)
