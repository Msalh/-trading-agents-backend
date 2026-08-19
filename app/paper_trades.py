"""
Paper Trade Lifecycle — Tier 2.3 (external review's prioritized
sequence, next after Tier 2.2). Reworked substantially in Tier 3.2
(second external review, fill realism) and Tier 3.3 (account-wide
atomic position limits) — see those sections below.

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
    for it. As of Tier 3.2, this only ever creates the ORDER
    (status="pending_fill") — it no longer decides a fill happened.
  - process_new_bar() — called on EVERY new bar for a symbol/
    timeframe, unconditionally (not gated by the Timing/kill-zone
    check that gates Analysis). Price doesn't pause outside kill
    zones, so neither should fill/stop/target monitoring for an order
    that's already resting or a trade that's already open.

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
already checks it earlier using the account-wide live count as an
advisory pre-check, but the real enforcement is this module's atomic
commit (see Tier 3.3 below), since Execution's LLM call happens in
between the two Risk stages and another candidate could commit a
trade during that window.

Tier 3.2 (second external review, "paper fills were still materially
unrealistic"): four changes, all aimed at the same goal — a closed
paper trade's pnl_usd should be defensible as a realistic estimate,
not just an exactly-computed but rigged number.

  1. Event time, not server time. Every lifecycle timestamp
     (order_submitted_at/opened_at/closed_at) is now the triggering
     BAR's own timestamp, not datetime.now() at the moment this code
     happened to run. Fill/expiry/close decisions, and the daily-loss
     trading-day bucketing that reads them (app/account_risk.py), all
     reason in event time now — a delayed or replayed bar is
     attributed to when it actually happened, not to whenever the
     server got around to processing it. Server-processing timestamps
     are still recorded, but in separate *_processed columns, purely
     as operational data — nothing in this module's trading logic
     reads them.
  2. ready_now is no longer a fill trigger. Execution's belief that a
     limit is "ready" doesn't prove the market actually traded there.
     Every order — market or limit — now starts "pending_fill" and
     only fills against a REAL subsequent bar. For a market order that
     means the very next bar's open (never the anchor bar itself,
     which has already closed by the time this code runs — filling
     "into" a bar that's already in the past would be lookahead bias).
     For a limit order it means the same price-cross check as before,
     just without the ready_now shortcut.
  3. Pending orders expire. Before this tier a limit could rest
     forever, filling hours or days after the setup that justified it
     had long since stopped being true. ORDER_EXPIRY_MINUTES (event
     time, measured from order_submitted_at) cancels it instead.
  4. Realistic fill/exit pricing. Market entries and stop exits both
     apply SLIPPAGE_POINTS against the trader (stops are effectively
     market orders once triggered) — limit and target fills stay
     exact, no slippage, since a resting order is filled at its stated
     price by definition. A stop is also gap-adjusted: if a bar's OPEN
     already breached the stop level, the realistic exit is the open
     (worse for the trader), not the stop price itself — the existing
     "never assume the better outcome" convention extended to gaps.
     COMMISSION_PER_CONTRACT (round-trip) is subtracted from pnl_usd
     on every close.

Not solved in Tier 3.2 (tracked for Tier 3.3, next): fully
transactional/account-wide position-limit reservation, and richer
expiry policies (session-close / kill-zone-close / setup-invalidation)
— ORDER_EXPIRY_MINUTES is a plain fixed window, not any of those (the
expiry-policy item is still open after 3.3 too).

Tier 3.3 (account-wide atomic position/risk limits — items 6-7 of the
user's own recommended ordering, closing the second external review's
"MAX_OPEN_POSITIONS not account-wide" and "position-limit enforcement
race-prone" findings): two changes.

  1. MAX_OPEN_POSITIONS is now account-wide. open_trade_from_candidate()
     used to check get_open_trade_count(symbol, timeframe) — scoped to
     ONE symbol+timeframe — so two different symbols could each
     independently reach "the limit" and the account could end up with
     a combined position count well past MAX_OPEN_POSITIONS. The real
     enforcement now counts live (pending_fill/open) trades across
     EVERY symbol/timeframe. get_open_trade_count() is kept as-is for
     informational/dashboard use (open positions for THIS symbol) — it
     is no longer what any risk decision is gated on.
  2. The check-then-insert is now one atomic transaction, not two
     separate operations. storage.open_trade_if_room() (BEGIN
     IMMEDIATE) folds the idempotency check (does this candidate_id
     already have a trade?) and the account-wide capacity check into
     the SAME transaction as the insert, closing the exact race the
     review flagged: two near-simultaneous candidates could previously
     each read "under capacity" before either had committed, opening
     one more position than the limit allows (or, for the same
     candidate, opening two trades for one candidate). See that
     function's docstring in app/storage.py for the full mechanism.
"""

import os
import uuid
from datetime import datetime, timezone

from app.storage import (
    cancel_trade,
    close_trade,
    get_open_or_pending_trade_count,
    get_open_or_pending_trades,
    get_trade_by_candidate_id,
    open_trade_if_room,
    update_trade_fill,
)

MNQ_POINT_VALUE = 2.0  # USD per index point per contract (Micro E-mini Nasdaq-100) — kept in sync with app/risk_agent.py's constant of the same name and meaning.

MAX_OPEN_POSITIONS = int(os.environ.get("MAX_OPEN_POSITIONS", "1"))

# Tier 3.22 provenance values — see open_trade_from_candidate()'s
# docstring. Named constants so main.py's two call sites can't typo a
# label that silently fails to match anything a report filters on.
PROVENANCE_AUTO_POLICY = "auto_policy"
PROVENANCE_MANUAL_DASHBOARD = "manual_dashboard"

# Tier 3.2 additions. Defaults are deliberately conservative
# approximations, not calibrated to a real broker's actual schedule —
# tunable via env var like everything else in this project.
ORDER_EXPIRY_MINUTES = int(os.environ.get("ORDER_EXPIRY_MINUTES", "60"))
SLIPPAGE_POINTS = float(os.environ.get("SLIPPAGE_POINTS", "0.25"))  # ~1 tick on MNQ
COMMISSION_PER_CONTRACT = float(os.environ.get("COMMISSION_PER_CONTRACT", "2.0"))  # round-trip, per contract


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_event_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _event_minutes_elapsed(earlier_ts: str, later_ts: str) -> float | None:
    """None (never expire) on anything unparseable — expiry is a
    destructive action (cancels a live order), so ambiguous/malformed
    timestamps fail safe rather than triggering a cancel."""
    try:
        return (_parse_event_ts(later_ts) - _parse_event_ts(earlier_ts)).total_seconds() / 60
    except (ValueError, AttributeError, TypeError):
        return None


def get_open_trade_count(symbol: str, timeframe: str) -> int:
    """Trades still live (pending_fill or open) for ONE symbol+
    timeframe. Informational/dashboard use only as of Tier 3.3 — no
    risk decision is gated on this scoped count anymore, since
    MAX_OPEN_POSITIONS is account-wide (see get_account_open_trade_count()
    and open_trade_from_candidate() below)."""
    return len(get_open_or_pending_trades(symbol=symbol, timeframe=timeframe))


def get_account_open_trade_count() -> int:
    """Tier 3.3: ACCOUNT-WIDE count of trades still live (pending_fill
    or open), across every symbol/timeframe — this is what
    MAX_OPEN_POSITIONS actually gates against. Used by Risk's gate
    stage (main.py) as a free, advisory pre-check before Execution
    runs; the real, atomic enforcement happens in
    open_trade_from_candidate() below when a trade is actually
    committed."""
    return get_open_or_pending_trade_count()


def open_trade_from_candidate(candidate: dict, provenance: str) -> dict | None:
    """Opens a new PENDING order from a candidate whose Risk result is
    approve/modify and whose Execution result is a validated
    status="planned" order. Returns the existing trade (not a new
    one) if this candidate already has one — idempotent by
    candidate_id. Returns None if the account-wide open-position limit
    is already at capacity (should be rare — Risk's gate stage already
    checked this before Execution even ran, but re-checked here as the
    real enforcement point, since Execution's LLM call happens in
    between and another candidate could have committed a trade during
    that window).

    Tier 3.2: always creates status="pending_fill", regardless of
    order_type or Execution's ready_now flag — see the module
    docstring for why. process_new_bar() is what actually fills it,
    against a real subsequent bar.

    Tier 3.3: the idempotency check and the account-wide capacity
    check are no longer two separate operations racing each other —
    both, plus the insert, happen inside storage.open_trade_if_room()'s
    single atomic transaction. This function just builds the candidate
    trade dict and asks that function to commit it if there's room.

    Tier 3.22 (fifth external review — a manual dashboard pipeline test
    on 2026-08-18 produced a real closed trade that was indistinguishable
    from autonomous execution in any report): `provenance` is now a
    REQUIRED argument, not inferred or defaulted, so every call site
    must say explicitly which of the two ways a trade can be opened it
    is — "auto_policy" for the AUTO_EXECUTE_ENABLED-gated background
    task, "manual_dashboard" for the manual /agents/risk/evaluate
    endpoint the dashboard's per-agent "Run" buttons hit. This is a
    code-verifiable split only — it cannot distinguish a deliberate
    manual pipeline TEST from a deliberate manual DISCRETIONARY trade
    decision, since that's a human-intent question the backend has no
    way to observe; both land under "manual_dashboard"."""
    candidate_id = candidate["candidate_id"]
    symbol, timeframe = candidate["symbol"], candidate["timeframe"]
    execution = candidate["execution"]
    risk = candidate["risk"]
    direction = candidate["decision"].get("direction")
    size = risk.get("suggested_size")

    # The candidate's own anchor bar (Tier 3.1) — event time for when
    # this order was actually submitted, not whenever this code
    # happens to run.
    anchor_bar = candidate.get("bar") or {}

    trade = {
        "trade_id": str(uuid.uuid4()),
        "candidate_id": candidate_id,
        "symbol": symbol,
        "timeframe": timeframe,
        "direction": direction,
        "size": size,
        "order_type": execution["order_type"],
        "entry_price": execution["entry_price"],  # Execution's PROPOSED price — reference only; fill_price is the realized one.
        "stop_loss": execution["stop_loss"],
        "targets": execution["targets"],
        "status": "pending_fill",
        "order_submitted_at": anchor_bar.get("timestamp"),
        "opened_at": None,
        "fill_price": None,
        "provenance": provenance,
    }
    status, result = open_trade_if_room(trade, MAX_OPEN_POSITIONS)
    if status == "at_capacity":
        return None
    return result  # "opened" -> this trade; "already_exists" -> the original one


def _nearest_target(direction: str, targets: list[float]) -> float | None:
    if not targets:
        return None
    return min(targets) if direction == "bullish" else max(targets)


def _pnl_usd(direction: str, entry_price: float, exit_price: float, size: int) -> float:
    diff = (exit_price - entry_price) if direction == "bullish" else (entry_price - exit_price)
    return round(diff * MNQ_POINT_VALUE * size, 2)


def _round_trip_commission(size: int) -> float:
    return round(COMMISSION_PER_CONTRACT * size, 2)


def _apply_entry_slippage(raw_price: float, order_type: str, direction: str) -> float:
    """Only market fills get slippage — a limit order is filled at its
    stated price by definition (that's what "limit" means), so there's
    nothing to model there."""
    if order_type != "market":
        return raw_price
    return round(raw_price + SLIPPAGE_POINTS, 4) if direction == "bullish" else round(raw_price - SLIPPAGE_POINTS, 4)


def _apply_stop_slippage(raw_price: float, direction: str) -> float:
    """A stop is effectively a market order once triggered — real
    stops don't get a guaranteed price. Always moves the fill AGAINST
    the trader, never in their favor."""
    return round(raw_price - SLIPPAGE_POINTS, 4) if direction == "bullish" else round(raw_price + SLIPPAGE_POINTS, 4)


def process_new_bar(symbol: str, timeframe: str, bar: dict) -> list[dict]:
    """Advances every live (pending_fill/open) trade for this
    symbol/timeframe against one new bar's OHLC. Returns the trades
    that changed state this call (filled/closed/cancelled), for
    logging — callers don't need to do anything with the return
    value, storage is already updated.

    Tier 3.2: bar["timestamp"] is now the EVENT time recorded on every
    state transition this function makes (fill, close, cancel) — see
    the module docstring."""
    high, low, open_ = bar.get("high"), bar.get("low"), bar.get("open")
    bar_ts = bar.get("timestamp")
    if high is None or low is None or open_ is None:
        return []

    changed: list[dict] = []
    for trade in get_open_or_pending_trades(symbol=symbol, timeframe=timeframe):
        if trade["status"] == "pending_fill":
            submitted_at = trade.get("order_submitted_at")
            if submitted_at and bar_ts:
                elapsed = _event_minutes_elapsed(submitted_at, bar_ts)
                if elapsed is not None and elapsed >= ORDER_EXPIRY_MINUTES:
                    cancel_trade(
                        trade["trade_id"], cancelled_at=bar_ts, reason="expired_unfilled",
                        cancelled_at_processed=_now_iso(),
                    )
                    changed.append({**trade, "status": "cancelled", "exit_reason": "expired_unfilled", "closed_at": bar_ts})
                    continue

            direction = trade["direction"]
            if trade["order_type"] == "market":
                crossed, raw_fill = True, open_  # unconditional — a market order fills wherever price is now
            else:
                entry = trade["entry_price"]
                crossed = (low <= entry) if direction == "bullish" else (high >= entry)
                raw_fill = entry
            if not crossed:
                continue  # still waiting — no stop/target check until it's actually open

            fill_price = _apply_entry_slippage(raw_fill, trade["order_type"], direction)
            update_trade_fill(trade["trade_id"], fill_price=fill_price, opened_at=bar_ts, opened_at_processed=_now_iso())
            trade = {**trade, "status": "open", "fill_price": fill_price, "opened_at": bar_ts}
            changed.append(trade)

        if trade["status"] != "open":
            continue  # a limit that didn't cross this bar — still pending

        direction = trade["direction"]
        stop = trade["stop_loss"]
        target = _nearest_target(direction, trade["targets"])
        fill_price = trade["fill_price"]
        size = trade["size"]

        stop_hit = (low <= stop) if direction == "bullish" else (high >= stop)
        target_hit = target is not None and ((high >= target) if direction == "bullish" else (low <= target))

        if stop_hit:
            # Gap-through-stop: if the bar's OPEN already breached the
            # stop, that's the realistic (worse) fill — a real stop
            # order doesn't get its exact requested price in a gap.
            raw_exit = min(open_, stop) if direction == "bullish" else max(open_, stop)
            exit_price = _apply_stop_slippage(raw_exit, direction)
            pnl = _pnl_usd(direction, fill_price, exit_price, size) - _round_trip_commission(size)
            close_trade(
                trade["trade_id"], exit_price=exit_price, exit_reason="stop_hit", pnl_usd=pnl,
                closed_at=bar_ts, closed_at_processed=_now_iso(),
            )
            changed.append({**trade, "status": "closed", "exit_price": exit_price, "exit_reason": "stop_hit", "pnl_usd": pnl, "closed_at": bar_ts})
        elif target_hit:
            # No favorable-gap credit at the target either — same
            # "never assume the better outcome" convention this
            # function already used before Tier 3.2.
            exit_price = target
            pnl = _pnl_usd(direction, fill_price, exit_price, size) - _round_trip_commission(size)
            close_trade(
                trade["trade_id"], exit_price=exit_price, exit_reason="target_hit", pnl_usd=pnl,
                closed_at=bar_ts, closed_at_processed=_now_iso(),
            )
            changed.append({**trade, "status": "closed", "exit_price": exit_price, "exit_reason": "target_hit", "pnl_usd": pnl, "closed_at": bar_ts})

    return changed
