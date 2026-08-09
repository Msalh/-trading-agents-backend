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
