"""
Account Risk — Tier 2.10 (account-level risk controls, external
review, Aug 2026).

Two gaps in risk_agent.py's account-level checks, both now closed:

1. Live drawdown tracking. CURRENT_DRAWDOWN_USED has always been a
   hand-updated env var ("update by hand as the real account balance/
   drawdown changes" — risk_agent.py's own docstring) that could
   silently drift from reality — the exact same problem Tier 2.3
   already fixed for open-position count (CURRENT_OPEN_POSITIONS ->
   the live paper_trades count). Now that real paper trades carry real
   closed P&L, drawdown can be computed the same way: as the standard
   peak-to-trough figure over the account-wide (all symbols, all
   timeframes — the risk budget is one account-wide number) cumulative
   realized P&L curve, not a number someone has to remember to update.
   compute_current_drawdown_used() replaces the CURRENT_DRAWDOWN_USED
   env var as the live default, mirroring exactly how
   evaluate_risk_gate()'s current_open_positions parameter already
   works: the live value is computed and passed in by the caller
   (main.py), with the env var kept only as a fallback for callers
   that don't have trade history yet (e.g. standalone tests).

2. Daily loss limit. No control existed at all for "stop opening new
   trades for the rest of the day once today's realized losses cross a
   threshold" — MAX_DRAWDOWN is a single account-wide ceiling with no
   time-boxing, so a single very bad day could consume the entire
   drawdown budget without a faster, dedicated circuit breaker.
   compute_realized_pnl_today() buckets closed trades into NY trading
   days using the exact same CME/Globex session-rollover convention
   app/trading_calendar.py already established for bar timestamps
   (Tier 2.9) — the two concepts (which trading day a bar belongs to,
   which trading day a trade close belongs to) are the same question,
   so this reuses that logic rather than inventing a second one.

Both live-computed values are non-authoritative estimates in the same
sense the whole paper-trading system is: they reflect ONLY what this
system itself has recorded as opened/closed paper trades. If the real
account also trades manually or through another system, these numbers
will legitimately diverge from the real account state — same caveat
that already applies to the Tier 2.3 live open-position count.
"""

import os

from app.storage import get_all_closed_trades_chronological
from app.trading_calendar import expected_trading_date

# Separate from MAX_DRAWDOWN (the account-wide ceiling) — a faster,
# time-boxed circuit breaker so one bad day can't quietly consume the
# whole drawdown budget before anyone notices. No prior env var to
# stay compatible with; this control didn't exist before this tier.
DAILY_LOSS_LIMIT = float(os.environ.get("DAILY_LOSS_LIMIT", "1000"))


def compute_current_drawdown_used(trades: list[dict] | None = None) -> float:
    """Standard peak-to-trough drawdown: walk the account-wide closed-
    trade P&L curve in chronological order, tracking the running peak
    of cumulative P&L, and return how far below that peak the current
    cumulative P&L sits. Never negative — being at or above the peak
    (including net-positive overall) means zero drawdown used, not a
    "negative" credit toward next time.

    trades is normally omitted (fetches live from storage) — accepting
    it as a parameter keeps this pure/testable without a DB, the same
    shape as coordinator._score_opinions accepting opinions directly."""
    if trades is None:
        trades = get_all_closed_trades_chronological()

    cumulative = 0.0
    peak = 0.0
    for trade in trades:
        cumulative += trade.get("pnl_usd") or 0.0
        peak = max(peak, cumulative)

    return round(max(0.0, peak - cumulative), 2)


def compute_realized_pnl_today(as_of_timestamp: str, trades: list[dict] | None = None) -> float:
    """Net realized P&L (can be positive) for closed trades whose
    closed_at falls on the same NY/CME trading day as as_of_timestamp
    — see app/trading_calendar.expected_trading_date for the exact
    session-rollover convention. as_of_timestamp is required (not
    defaulted to "now" internally) so callers control what "today"
    means and tests stay deterministic — main.py passes the real
    current time."""
    if trades is None:
        trades = get_all_closed_trades_chronological()

    today = expected_trading_date(as_of_timestamp)
    total = 0.0
    for trade in trades:
        closed_at = trade.get("closed_at")
        if not closed_at:
            continue
        try:
            trade_day = expected_trading_date(closed_at)
        except (ValueError, AttributeError, TypeError):
            continue
        if trade_day == today:
            total += trade.get("pnl_usd") or 0.0

    return round(total, 2)


def compute_daily_loss_used(as_of_timestamp: str, trades: list[dict] | None = None) -> float:
    """Non-negative "how much of today's loss budget is already
    spent" — a net-positive day returns 0, same non-negative
    convention as compute_current_drawdown_used, so both compose
    directly against a *_LIMIT/*_ROOM style budget check in
    risk_agent.py."""
    realized_today = compute_realized_pnl_today(as_of_timestamp, trades=trades)
    return round(max(0.0, -realized_today), 2)
