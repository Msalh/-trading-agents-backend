"""
Paper Trade Lifecycle — Tier 2.3 (external review's prioritized
sequence, next after Tier 2.2).

Everything up through Execution/Risk produces an opinion about what
SHOULD happen — nothing until now has tracked what actually happened
to a trade once it was approved. This module is that missing piece:
it opens a paper position the moment Risk's size stage approves or
modifies a candidate, then advances it bar-by-bar (fill, stop hit,
target hit) using only OHLC data already stored from the TradingView
webhook. No broker, no real orders — same "paper only" boundary as
Execution Agent.

Two entry points:
  - open_trade_from_candidate() — called once, right after Risk's
    size stage returns approve/modify (see main.py). Idempotent per
    candidate_id: re-running Risk's size stage on the same candidate
    (e.g. a dashboard double-click) must never open a second position
    for it.
  - process_new_bar() — called on EVERY new bar for a symbol/
    timeframe, unconditionally (not gated by the Timing/kill-zone
    check that gates Analysis). Price doesn't pause outside kill
    zones, so neither should stop/target monitoring for a trade
    that's already open.

Order-of-events assumption: when a single bar's high/low range
contains BOTH the stop and the nearest target, the stop is assumed to
have been hit first. This is the standard conservative assumption in
systems working from OHLC bars rather than tick data — there's no way
to know the true intrabar sequence, and assuming the better outcome
would silently overstate performance.

Fill/target checks only ever use the NEAREST target (min for a long,
max for a short) — same "most conservative" convention Execution
Agent's geometry validation already uses for its reward:risk check.
A trade with multiple targets only fully closes at the first one; no
partial-close/scale-out modeling in this pass (noted as a known
simplification, not solved here).

MAX_OPEN_POSITIONS is enforced here as the final gate before actually
committing a paper position — Risk's gate stage (evaluate_risk_gate)
already checks it earlier using the same get_open_trade_count(), but
double-checking here means a race between two nearly-simultaneous
candidates can't both slip through and open two positions.
"""

import os
import uuid
from datetime import datetime, timezone

from app.storage import (
    close_trade,
    get_open_or_pending_trades,
    get_trade_by_candidate_id,
    save_paper_trade,
    update_trade_fill,
)

MNQ_POINT_VALUE = 2.0  # USD per index point per contract (Micro E-mini Nasdaq-100) — kept in sync with app/risk_agent.py's constant of the same name and meaning.

MAX_OPEN_POSITIONS = int(os.environ.get("MAX_OPEN_POSITIONS", "1"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_open_trade_count(symbol: str, timeframe: str) -> int:
    """Trades still live (pending_fill or open) — this is what Risk's
    gate stage now checks against MAX_OPEN_POSITIONS, replacing the
    old hand-updated CURRENT_OPEN_POSITIONS env var."""
    return len(get_open_or_pending_trades(symbol=symbol, timeframe=timeframe))


def open_trade_from_candidate(candidate: dict) -> dict | None:
    """Opens a new paper trade from a candidate whose Risk result is
    approve/modify and whose Execution result is a validated
    status="planned" order. Returns the existing trade (not a new
    one) if this candidate already has one — idempotent by
    candidate_id. Returns None if the open-position limit is already
    at capacity (should be rare — Risk's gate stage already checked
    this before Execution even ran, but re-checked here as the last
    line of defense against a race)."""
    candidate_id = candidate["candidate_id"]
    existing = get_trade_by_candidate_id(candidate_id)
    if existing is not None:
        return existing

    symbol, timeframe = candidate["symbol"], candidate["timeframe"]
    if get_open_trade_count(symbol, timeframe) >= MAX_OPEN_POSITIONS:
        return None

    execution = candidate["execution"]
    risk = candidate["risk"]
    direction = candidate["decision"].get("direction")
    size = risk.get("suggested_size")

    order_type = execution["order_type"]
    entry_price = execution["entry_price"]
    ready_now = execution.get("ready_now", order_type == "market")
    is_immediate = order_type == "market" or bool(ready_now)

    now_iso = _now_iso()
    trade = {
        "trade_id": str(uuid.uuid4()),
        "candidate_id": candidate_id,
        "symbol": symbol,
        "timeframe": timeframe,
        "direction": direction,
        "size": size,
        "order_type": order_type,
        "entry_price": entry_price,
        "stop_loss": execution["stop_loss"],
        "targets": execution["targets"],
        "status": "open" if is_immediate else "pending_fill",
        "opened_at": now_iso if is_immediate else None,
        "fill_price": entry_price if is_immediate else None,
    }
    save_paper_trade(trade)
    return trade


def _nearest_target(direction: str, targets: list[float]) -> float | None:
    if not targets:
        return None
    return min(targets) if direction == "bullish" else max(targets)


def _pnl_usd(direction: str, entry_price: float, exit_price: float, size: int) -> float:
    diff = (exit_price - entry_price) if direction == "bullish" else (entry_price - exit_price)
    return round(diff * MNQ_POINT_VALUE * size, 2)


def process_new_bar(symbol: str, timeframe: str, bar: dict) -> list[dict]:
    """Advances every live (pending_fill/open) trade for this
    symbol/timeframe against one new bar's high/low range. Returns
    the trades that changed state this call (filled and/or closed),
    for logging — callers don't need to do anything with the return
    value, storage is already updated."""
    high, low = bar.get("high"), bar.get("low")
    if high is None or low is None:
        return []

    changed: list[dict] = []
    for trade in get_open_or_pending_trades(symbol=symbol, timeframe=timeframe):
        if trade["status"] == "pending_fill":
            entry = trade["entry_price"]
            crossed = (low <= entry) if trade["direction"] == "bullish" else (high >= entry)
            if not crossed:
                continue  # still waiting — no stop/target check until it's actually open
            now_iso = _now_iso()
            update_trade_fill(trade["trade_id"], fill_price=entry, opened_at=now_iso)
            trade = {**trade, "status": "open", "fill_price": entry, "opened_at": now_iso}
            changed.append(trade)

        direction = trade["direction"]
        stop = trade["stop_loss"]
        target = _nearest_target(direction, trade["targets"])
        fill_price = trade["fill_price"]
        size = trade["size"]

        stop_hit = (low <= stop) if direction == "bullish" else (high >= stop)
        target_hit = target is not None and ((high >= target) if direction == "bullish" else (low <= target))

        if stop_hit:
            pnl = _pnl_usd(direction, fill_price, stop, size)
            now_iso = _now_iso()
            close_trade(trade["trade_id"], exit_price=stop, exit_reason="stop_hit", pnl_usd=pnl, closed_at=now_iso)
            changed.append({**trade, "status": "closed", "exit_price": stop, "exit_reason": "stop_hit", "pnl_usd": pnl, "closed_at": now_iso})
        elif target_hit:
            pnl = _pnl_usd(direction, fill_price, target, size)
            now_iso = _now_iso()
            close_trade(trade["trade_id"], exit_price=target, exit_reason="target_hit", pnl_usd=pnl, closed_at=now_iso)
            changed.append({**trade, "status": "closed", "exit_price": target, "exit_reason": "target_hit", "pnl_usd": pnl, "closed_at": now_iso})

    return changed
