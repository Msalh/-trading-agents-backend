"""
Minimal SQLite storage for market_state events.

Deliberately simple for Sprint 1: one table, one index, idempotent
inserts keyed on event_id (the Pine script already guarantees this is
deterministic per symbol+timeframe+bar-close, so INSERT OR IGNORE is
enough to dedupe retried/duplicate webhook deliveries).
"""

import json
import sqlite3
from pathlib import Path
from typing import Optional

from app.models import MarketStatePayload

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "market_state.db"

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
