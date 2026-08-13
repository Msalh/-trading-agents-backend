"""
Sprint 1: receive market_state events from the TradingView Pine
Script broadcaster, validate them, store them, and expose them for
the Analysis Agent (built next) and the dashboard (built later).

Sprint 2: added the Timing/Session Agent — pure local logic (no LLM)
that runs on every webhook and decides whether the bar falls inside
a session worth analyzing. This is the gate described in the roadmap:
before any expensive LLM agent runs, Timing decides go/no-go.

Sprint 3: added the Analysis Agent — the first LLM-backed agent. It
reads recent market_state bars and asks Claude for a technical
direction/confidence/reasoning read, stored as an "opinion" for the
future Coordinator to consume.

Sprint 4: added the News Agent (uses Claude's hosted web_search tool
— no separate news API needed) and an independent background
scheduler (APScheduler, in-process) that runs it on its own clock,
decoupled from the webhook that drives Analysis.

Sprint 5: added the Macro/Correlation Agent (DXY, US10Y, SPX/NDX),
same web_search pattern as News, sharing the same scheduler.

Sprint 6: added the Coordinator — pure aggregation (no LLM call of
its own) that combines the four agents' latest opinions into a
weighted score and a preliminary decision.

Sprint 7: added the Risk Agent — deterministic risk math (no LLM,
same reasoning as Timing), holding full veto over the Coordinator's
decision: approve / modify (smaller size) / reject. Account state is
static/manual for now via environment variables.

Sprint 8: added CORS support so the dashboard (a browser-based page,
running on a different origin than this API) can call these
endpoints directly.

Sprint 9: added the dashboard itself — a single static HTML page
(app/static/index.html) served from this same FastAPI app at
/dashboard, reading every agent/coordinator/risk endpoint above.

Sprint 10: added /system/status (server time, scheduler config, last
webhook/agent-run timestamps) plus a tests/ suite for the Coordinator
and Risk Agent and an API_REFERENCE.md.

Sprint 11: added /admin/wipe-all-data — a one-time, secret-protected
reset for clearing test/synthetic data before a real trading session.

Sprint 12: the webhook handler now actually invokes Analysis on new
bars inside the Timing gate, instead of only computing the gate
decision — closing a gap where Analysis never ran automatically.

Sprint 13: after a fresh Analysis opinion saves, the webhook handler
now also auto-computes and persists a Coordinator decision, so
/coordinator/history fills in on its own instead of needing a manual
"Compute & Save" click.

Sprint 14: added outcome tracking (app/outcomes.py) — for each
directional Coordinator decision, /coordinator/history/outcomes looks
up whether price actually moved the predicted way at several time
horizons, computed on demand from bars already stored (nothing new
persisted). Intended for calibrating COORDINATOR_THRESHOLD against
real accuracy instead of guessing from the dashboard.

Sprint 15: added the Execution Agent (final agent from the roadmap) —
LLM-backed, paper-only. Turns an already-approved Risk decision into
a concrete order spec (entry/stop/targets). Never places a real order
or talks to a broker; nothing to execute (no LLM call) unless Risk
approved or modified the trade.

Sprint 16: the webhook's auto-Analysis/Coordinator run now happens in
a BackgroundTasks task instead of inline — the webhook acks
TradingView immediately instead of waiting on the LLM call, avoiding
delivery timeouts on TradingView's side.

Sprint 17: added DELETE /admin/market-state/{event_id} — surgical
removal of a single known-bad bar (e.g. a manual test webhook that
leaked into real history) without wiping everything via
/admin/wipe-all-data. Same secret-guard as the webhook.

Tier-1 safety fixes (external review): six endpoints that trigger a
paid LLM/search call or write to the database — /agents/analysis/run,
/agents/news/run, /agents/macro/run, /coordinator/decide,
/agents/risk/evaluate, /agents/execution/plan — were reachable by
anyone with the URL. Now guarded by the same X-Webhook-Secret as the
webhook and /admin/* endpoints.

Tier 2.1 (external review): added the trade-candidate lifecycle
(app/candidates.py) — one immutable row per Coordinator run that
freezes the exact bar and opinions a decision was scored from. Risk
and Execution now act on that SAME candidate row instead of each
independently querying "latest decision"/"latest opinion"/"latest
bar", which could silently disagree if a new bar or opinion landed
between two separately-timed lookups.

Tier 2.2 (external review): Risk used to size every position from
ATR as a proxy for stop distance, because it ran BEFORE Execution
(which picks the real stop) — an estimate stood in for a number that
didn't exist yet. Reordered around the candidate: Risk now runs
twice. First a free "gate" pass (position limits / drawdown room only,
no stop needed) decides whether it's worth letting Execution spend a
paid LLM call at all. Execution then proposes order geometry only —
no size, since Risk hasn't sized anything yet. Risk's second "size"
pass reads that real entry_price/stop_loss back off the candidate and
sizes the position from the actual stop distance. Same
/agents/risk/evaluate and /agents/execution/plan endpoints as before;
the endpoints now inspect the candidate to run the right stage.

Tier 2.3 (paper fill/P&L lifecycle): everything through Tier 2.2
produced an opinion about what SHOULD happen; nothing tracked what
actually happened. app/paper_trades.py closes that gap — the moment
Risk's size stage approves/modifies a candidate, a paper trade opens
(immediately for a market/ready-now order, or "pending_fill" for a
limit waiting on price). Every new bar for that symbol/timeframe —
regardless of Timing/kill-zone gating, since price doesn't pause
outside a kill zone — advances every live trade: fills a pending
limit, and closes an open trade on a stop or (nearest) target hit,
realizing P&L in dollars. Risk's gate stage now checks the LIVE open-
trade count instead of the old hand-updated CURRENT_OPEN_POSITIONS env
var. New read endpoints: GET /trades/open, /trades/history,
/trades/{trade_id}.

Tier 2.4 (outcome tracker rebuild): the original Sprint 14 outcome
tracker (app/outcomes.py) could only ever estimate whether a decision
"probably" worked out, by comparing price at fixed time horizons —
the only option available before any decision could become a real,
trackable trade. Now that Tier 2.3 gives every acted-on candidate a
real closed paper trade with real P&L, that's ground truth and should
be preferred over a guess. compute_outcome_for_candidate() checks for
a real trade first (closed -> actual win/loss/pnl_usd; still open ->
"pending") and only falls back to the old horizon estimate for
candidates that never became a trade at all — labeled
"source": "hypothetical" so a guess is never mistaken for a fact. New
endpoints: GET /candidates/history/outcomes (per-candidate) and
GET /candidates/history/outcomes/summary (aggregated win rate/total
P&L vs. hypothetical horizon accuracy, replacing manual by-hand
tallying of decision history for COORDINATOR_THRESHOLD tuning). The
older /coordinator/history/outcomes is unchanged — it reads a table
with no candidate_id, so it can never link to a real trade either way.

Tier 2.5 (replay/versioning): COORDINATOR_THRESHOLD, the four agent
WEIGHTS, and MIN_AVAILABLE_WEIGHT have all changed via env vars over
this project's lifetime, and nothing recorded which config produced
a given historical decision. coordinator.py now exposes the scoring
math as a standalone _score_opinions() function (pulled out of
compute_decision(), which now just calls it with the live env-var
config), and every CoordinatorDecision carries a new config_version
field recording exactly which weights/threshold/min_available_weight
it was scored under. app/replay.py uses this to re-score a trade
candidate's already-frozen opinions_used (Tier 2.1) under either the
current live config or an explicit hypothetical override — entirely
offline, no new data, no LLM calls, never mutating the original
candidate. New endpoints: GET /candidates/{candidate_id}/replay
(single candidate), GET /candidates/history/replay (bulk, with
only_changed filtering), and GET /candidates/history/replay/summary
(aggregated decision-transition counts) — built for config-tuning
questions like "if the threshold had been 35 this whole time, how
many of the last 100 decisions would have flipped?".

Tier 2.8 (Coordinator redesign — the Timing-neutral-weight issue
flagged back in Tier 1): Timing's direction is always "neutral" by
design, so its slot in the weighted sum always contributed 0
magnitude — but its 20% weight still counted as "available evidence"
for MIN_AVAILABLE_WEIGHT, so Analysis (40%) plus a present-but-neutral
Timing (20%, present on almost every webhook bar) cleared the 60%
minimum trivially, letting Analysis alone effectively single-handedly
trigger a trade in production. coordinator.py now excludes Timing from
available_weight/the weighted score entirely via a new
DIRECTIONAL_AGENTS constant (analysis/news/macro only) — the minimum
is now a fraction of the *directional* evidence pool, so Analysis
alone (40% of an 80%-wide directional pool = 50%) correctly stays
insufficient_data regardless of Timing. Timing keeps its designed
purpose as an actual gate instead of dead weight: still gathered and
still in opinions_used, but now read out separately as a new
CoordinatorDecision.timing_context field and applied as a score
dampener/veto — full veto (score forced to 0) on a "market_closed"
flag (weekend timestamp), half-dampened on "low_liquidity" (a weekday
bar outside every kill zone). No new endpoints — same
/coordinator/decide, visible in the response's timing_context field
and, when triggered, in conflict_flags ("timing_market_closed" /
"timing_low_liquidity_dampened").

Tier 2.9 (calendar integrity): three related gaps, all fixed via a new
app/trading_calendar.py (deterministic US holiday calendar + a
CME/Globex trading-day/timestamp consistency check):
  1. Holiday awareness: timing_agent.py only checked weekday-vs-weekend
     — a US market holiday (Thanksgiving, July 4th, Christmas, etc.) is
     a WEEKDAY the cash equity market these kill zones model is closed
     on. A bar timestamped during nominal kill-zone hours on a holiday
     used to score as a normal, full-confidence session — wasting a
     paid Analysis LLM call and letting the Coordinator treat a shut
     market as ordinary. is_holiday now folds into every in_*_session
     flag the same way is_weekday already did, and TimingOpinion gains
     an is_holiday key_data field plus the same "market_closed" flag
     weekends already use (so Tier 2.8's Coordinator veto applies here
     for free, no coordinator.py changes needed).
  2. Bar data-integrity: the webhook payload's own trading_date field
     was never checked against what its timestamp implies. New
     calendar_warning field on WebhookAck (None when consistent) flags
     a mismatch under the CME/Globex session-rollover convention (NY
     local time >= 18:00 belongs to the next day's session) — surfaced
     and logged, never rejected, since failing ingestion outright over
     a data source we don't control is worse than a flagged anomaly.
  3. Economic event awareness: News's "urgent" flag (FOMC/CPI/NFP
     imminent or breaking) only ever dampened the score INSIDE the
     Analysis/News opposing-conflict branch in coordinator.py — two
     agents that AGREED (e.g. both bullish right before an FOMC
     decision) got zero dampening despite the same flagged risk,
     silently ignoring the flag exactly the way the conflict-dampening
     design was meant to prevent. "urgent" now dampens the score
     whenever News carries it, independent of conflict status; the
     combined conflict+urgent case still reports the original single
     "analysis_news_conflict_urgent_dampened" flag, agreement+urgent
     reports the new "news_urgent_dampened".

Tier 2.10 (account-level risk controls): CURRENT_DRAWDOWN_USED has
always been a hand-updated env var that could silently drift from
reality — the same problem Tier 2.3 already fixed for open-position
count. New app/account_risk.py computes it live instead, as the
standard peak-to-trough figure over the account-wide (all symbols)
cumulative realized P&L from real closed paper trades.
evaluate_risk_gate() and size_position() both gained a
current_drawdown_used parameter following the exact same pattern
current_open_positions already used (Tier 2.3): main.py passes the
live-computed value, the env var is now only the fallback default.
Also new: DAILY_LOSS_LIMIT, a faster, time-boxed circuit breaker
distinct from the account-wide MAX_DRAWDOWN — no new trades for the
rest of the trading day once today's realized losses (bucketed by the
same CME/Globex trading-day convention Tier 2.9 established) cross
this threshold, checked at both Risk stages via a new daily_loss_used
parameter (rejects with flag "daily_loss_limit_reached"). New
read-only endpoint GET /account/risk exposes both live figures without
triggering a risk evaluation.

Tier 3.1 (causal integrity, second external review, Aug 2026): the
"immutable, atomically-bound" candidate the Tier 2.1 changelog
promised wasn't actually either. (1) create_candidate() and its
webhook-triggered caller each independently asked storage for "the
latest bar"/"the latest opinion" at different points — a second
webhook landing mid-flight could make the frozen candidate's bar,
Timing context, and Analysis opinion each describe a different
moment. Fixed: the webhook now passes its own event_id through to
_run_auto_analysis_and_coordinator(), which fetches that EXACT bar
once (get_by_event_id), bounds the Analysis window to it
(get_recent_as_of), and threads both the anchor bar and the resulting
Analysis opinion straight into create_candidate() — nothing is
re-queried as "latest" more than once per webhook. compute_decision()
and create_candidate() both gained optional bar/analysis_opinion
parameters for this; the manual /coordinator/decide path (no specific
triggering event) is unaffected, still using "latest". (2) risk_json/
execution_json were plain overwrite-in-place columns with no
protection once a paper trade had actually been committed from a
candidate — re-calling /agents/risk/evaluate or /agents/execution/plan
after that point could silently rewrite the candidate to describe a
size/geometry the committed trade never actually had. Fixed:
app/storage.py's attach_risk_result/attach_execution_result now
refuse ("locked") once get_trade_by_candidate_id() finds a committed
trade, and both endpoints check for that up front and short-circuit
to the trade's real, already-committed state instead of recomputing.
Every attach before that point now APPENDS to a new risk_history_json/
execution_history_json column instead of only keeping the latest — so
the original gate opinion is still visible after the size opinion
lands, and a retried Execution call doesn't erase the attempt before
it. Not in scope for this tier (tracked for a later one): fully
transactional position-limit reservation (the count-then-insert gate
in paper_trades.py is still two separate operations), paper fill
realism (ready_now/market fills, no order expiry, no
commissions/slippage), and event-time vs. processing-time trade
timestamps — see the second review's Priority 2/3 findings.

This backend is intentionally standalone — no dependency on any
other existing project.
"""

import json
import logging
import os
from datetime import datetime, timezone

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.account_risk import compute_current_drawdown_used, compute_daily_loss_used
from app.analysis_agent import AnalysisAgentError, run_analysis
from app.candidates import (
    CandidateError,
    CandidateLockedError,
    create_candidate,
    get_candidate_history,
    get_committed_trade,
    get_current_candidate,
    record_execution_result,
    record_risk_result,
)
from app.coordinator import compute_decision
from app.execution_agent import ExecutionAgentError, plan_execution
from app.macro_agent import MacroAgentError, run_macro
from app.models import MarketStateOut, MarketStatePayload, WebhookAck
from app.news_agent import NewsAgentError, run_news
from app.outcomes import (
    HORIZON_MINUTES_DEFAULT,
    compute_outcome_for_candidate,
    compute_outcomes_for_decision,
    summarize_outcomes,
)
from app.paper_trades import get_open_trade_count, open_trade_from_candidate, process_new_bar
from app.replay import replay_candidate, replay_candidates_for_symbol, summarize_replay
from app.risk_agent import (
    ACCOUNT_BALANCE,
    DAILY_LOSS_LIMIT,
    MAX_DRAWDOWN,
    evaluate_risk_gate,
    size_position,
)
from app.scheduler import (
    MACRO_SYMBOL,
    MACRO_TIMEFRAME,
    NEWS_SYMBOL,
    NEWS_TIMEFRAME,
    start_scheduler,
    stop_scheduler,
)
from app.storage import (
    get_all_closed_trades_chronological,
    get_by_event_id,
    get_candidate_by_id,
    get_last_opinion_timestamps,
    get_last_webhook_received,
    get_latest,
    get_latest_candidate,
    get_latest_opinion,
    get_open_or_pending_trades,
    get_recent,
    get_recent_as_of,
    get_recent_decisions,
    get_recent_opinions,
    get_recent_trades,
    get_trade_by_id,
    init_db,
    save_decision,
    save_event,
    save_opinion,
    delete_market_state_event,
    wipe_all_data,
)
from app.timing_agent import evaluate_timing, should_run_analysis
from app.trading_calendar import check_trading_date

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")


def _check_secret(x_webhook_secret: str | None) -> None:
    """Shared guard for any endpoint that costs money (LLM/search
    calls) or writes to the database. Originally only /webhook/tradingview
    and /admin/* were protected — the /agents/*/run, /coordinator/decide,
    /agents/risk/evaluate, and /agents/execution/plan endpoints were
    reachable by anyone with the URL, including Execution which fires a
    real paid LLM call on a bare GET. Same secret as the webhook, so no
    new credential to manage."""
    if not x_webhook_secret or x_webhook_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="invalid or missing secret")


app = FastAPI(title="Trading Agents Backend", version="0.1.0")

# The dashboard is a static page served from a different origin than
# this API (e.g. an artifact preview, or later a proper static host).
# Read-only GET endpoints, no cookies/session auth involved — allowing
# any origin is acceptable here. Tighten to specific origins later if
# this API ever handles anything more sensitive than read-mostly
# trading data behind its own webhook secret / API key checks.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# Dashboard: a single static page, same-origin (no CORS needed to view
# it here — CORS above is for anyone hosting/opening it elsewhere).
app.mount("/dashboard", StaticFiles(directory="app/static", html=True), name="dashboard")


@app.on_event("startup")
def _startup() -> None:
    if not WEBHOOK_SECRET:
        # Fail loudly rather than silently accepting unauthenticated
        # webhooks — this must be set before deploying anywhere public.
        raise RuntimeError(
            "WEBHOOK_SECRET environment variable is not set. "
            "Set it before starting the server."
        )
    init_db()
    start_scheduler()


@app.on_event("shutdown")
def _shutdown() -> None:
    stop_scheduler()


def _minutes_since(sqlite_datetime: str | None) -> float | None:
    """SQLite's datetime('now') stores UTC as 'YYYY-MM-DD HH:MM:SS'
    (no timezone suffix). Parse it as UTC and return minutes elapsed."""
    if not sqlite_datetime:
        return None
    dt = datetime.strptime(sqlite_datetime, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - dt
    return round(delta.total_seconds() / 60, 1)


@app.get("/system/status")
def system_status(symbol: str = Query(default=NEWS_SYMBOL)) -> dict:
    scheduler_enabled = os.environ.get("ENABLE_SCHEDULER", "false").lower() == "true"
    last_runs = get_last_opinion_timestamps(symbol=symbol)
    last_webhook = get_last_webhook_received(symbol=symbol)

    agents = {}
    for agent in ("analysis", "news", "macro", "risk"):
        last_run = last_runs.get(agent)
        agents[agent] = {
            "last_run": last_run,
            "minutes_since_last_run": _minutes_since(last_run),
        }

    return {
        "server_time_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scheduler_enabled": scheduler_enabled,
        "scheduler_intervals_minutes": {
            "news": int(os.environ.get("NEWS_INTERVAL_MINUTES", "60")),
            "macro": int(os.environ.get("MACRO_INTERVAL_MINUTES", "60")),
        },
        "last_webhook_received": last_webhook,
        "minutes_since_last_webhook": _minutes_since(last_webhook),
        "agents": agents,
    }


@app.post("/admin/wipe-all-data")
def admin_wipe_all_data(
    x_webhook_secret: str | None = Header(default=None, alias="X-Webhook-Secret"),
) -> dict:
    """Irreversible. Wipes every stored bar, agent opinion, and
    Coordinator decision. Intended for a one-time reset before a real
    trading session starts, clearing out test/synthetic data. Guarded
    by the same secret as the TradingView webhook — not a general-
    purpose admin surface."""
    if not x_webhook_secret or x_webhook_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="invalid or missing secret")
    counts = wipe_all_data()
    return {"wiped": True, "rows_deleted": counts}


@app.delete("/admin/market-state/{event_id}")
def admin_delete_market_state_event(
    event_id: str,
    x_webhook_secret: str | None = Header(default=None, alias="X-Webhook-Secret"),
) -> dict:
    """Deletes a single market_state bar by its event_id — for
    surgically removing known-bad data (e.g. manual test webhooks
    that leaked into real history) without wiping everything else.
    Scoped to market_state only: it does not cascade-delete any
    agent_opinions or coordinator_decisions that were computed from
    that bar, since those aren't tied to it by a reliable key (the
    Coordinator's own timestamp is its compute time, not the bar's).
    Guarded by the same secret as the webhook."""
    if not x_webhook_secret or x_webhook_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="invalid or missing secret")
    deleted = delete_market_state_event(event_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"no market_state row found for event_id={event_id}")
    return {"deleted": True, "event_id": event_id}


@app.get("/")
def health_check() -> dict:
    return {"status": "ok"}


def _run_auto_analysis_and_coordinator(symbol: str, timeframe: str, event_id: str) -> None:
    """Runs Analysis + Coordinator for a freshly-arrived bar. Called via
    BackgroundTasks so the webhook can ack TradingView immediately —
    the LLM call is the slow part, and TradingView's own delivery
    timeout is short enough that waiting for it inline caused
    legitimate "request took too long and timed out" failures at
    TradingView even though the work itself completed successfully
    a few seconds later.

    Tier 3.1 (causal integrity): event_id anchors this entire run to
    the EXACT bar that triggered it. Before this, the function only
    took symbol/timeframe and asked for "the latest 10 bars" whenever
    it happened to actually run — on 5m/15m/1h feeds with a slow LLM
    call and BackgroundTasks queuing, a second bar can genuinely land
    before this one finishes, and "latest" would silently mean a
    different, newer bar than the one that triggered this run. Now the
    anchor bar is fetched once by its own event_id, the Analysis
    window is bounded to bars at-or-before it (get_recent_as_of), and
    both the anchor bar and the resulting Analysis opinion are passed
    straight into create_candidate() instead of it re-querying "latest"
    a second and third time."""
    anchor_bar = get_by_event_id(event_id)
    if anchor_bar is None:
        # Shouldn't happen — save_event() just inserted this exact
        # event_id moments before this task was scheduled. Logged, not
        # raised: a background task has nothing to return an error to.
        logging.getLogger("webhook").error("auto-analysis: anchor bar not found for event_id=%s", event_id)
        return

    try:
        recent_bars = get_recent_as_of(
            symbol=symbol, timeframe=timeframe, as_of_timestamp=anchor_bar["timestamp"], limit=10
        )
        recent_bars.reverse()
        opinion = run_analysis(symbol=symbol, timeframe=timeframe, bars=recent_bars)
        save_opinion(
            agent="analysis",
            symbol=symbol,
            timeframe=timeframe,
            timestamp=opinion.timestamp,
            opinion=opinion.to_dict(),
        )

        # Auto-build a trade candidate right after a fresh Analysis
        # opinion lands — this is what actually populates candidate
        # history on its own. create_candidate() is now given the SAME
        # anchor_bar and the SAME opinion just computed above (rather
        # than independently re-fetching "latest" bar/opinion), so the
        # candidate's bar, its Timing context, and the Analysis
        # opinion it was scored from are all guaranteed to describe the
        # one triggering event, not three independently-resolved
        # "latest" reads. Still also writes to the older
        # coordinator_decisions table for backward compatibility with
        # the dashboard's existing Decision History view, until it's
        # updated to read from candidate history.
        try:
            candidate = create_candidate(
                symbol=symbol, timeframe=timeframe, bar=anchor_bar, analysis_opinion=opinion.to_dict()
            )
            save_decision(
                symbol=symbol,
                timeframe=timeframe,
                timestamp=candidate["decision"]["timestamp"],
                decision=candidate["decision"],
            )
        except Exception as e:  # noqa: BLE001 - background task, log and move on
            logging.getLogger("webhook").error("auto-candidate failed: %s", e)
    except AnalysisAgentError as e:
        logging.getLogger("webhook").error("auto-analysis failed: %s", e)


def _process_paper_trades(symbol: str, timeframe: str, bar: dict) -> None:
    """Advances every live paper trade against a freshly-arrived bar.
    Deliberately NOT gated by the Timing/kill-zone check that gates
    Analysis — a trade that's already open can hit its stop or target
    at any hour, session or not, and this only reads OHLC already
    stored, no LLM/cost involved. Runs as its own background task so
    it never waits on (or is blocked by) the separate, slower
    Analysis/Coordinator task."""
    try:
        changed = process_new_bar(symbol=symbol, timeframe=timeframe, bar=bar)
        if changed:
            logging.getLogger("webhook").info(
                "paper trades updated: %s",
                [(t["trade_id"], t["status"], t.get("exit_reason")) for t in changed],
            )
    except Exception as e:  # noqa: BLE001 - background task, log and move on
        logging.getLogger("webhook").error("paper trade processing failed: %s", e)


@app.post("/webhook/tradingview", response_model=WebhookAck)
def receive_market_state(
    payload: MarketStatePayload,
    background_tasks: BackgroundTasks,
    x_webhook_secret: str | None = Header(default=None, alias="X-Webhook-Secret"),
) -> WebhookAck:
    # TradingView alerts can't set custom headers, so the secret travels
    # inside the JSON body (payload.secret) as the script already does.
    # The X-Webhook-Secret header is accepted too, for any client that
    # *can* set headers (manual tests, curl, future callers).
    provided_secret = payload.secret or x_webhook_secret
    if not provided_secret or provided_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="invalid or missing secret")

    is_new = save_event(payload)

    timing = evaluate_timing(payload.timestamp)
    analysis_would_run = should_run_analysis(timing)

    # Tier 2.9 (calendar integrity): does the payload's own
    # trading_date agree with what its timestamp implies? A mismatch
    # is surfaced, not rejected — see app/trading_calendar.py.
    calendar_warning = check_trading_date(payload.timestamp, payload.trading_date)
    if calendar_warning:
        logging.getLogger("webhook").warning(
            "calendar integrity: event_id=%s %s", payload.event_id, calendar_warning
        )

    # Only for genuinely new bars — a retried/duplicate delivery
    # shouldn't trigger a second paid LLM call for the same bar.
    # Scheduled as a background task: the HTTP response below returns
    # to TradingView immediately, and the (slower) LLM call + Coordinator
    # run after, off the request/response path entirely.
    if is_new and analysis_would_run:
        background_tasks.add_task(
            _run_auto_analysis_and_coordinator, payload.symbol, payload.timeframe, payload.event_id
        )

    # Paper trade monitoring runs on every new bar, independent of the
    # Timing gate above — an already-open trade's stop/target doesn't
    # wait for a kill zone. Cheap (no LLM), so no reason to gate it.
    if is_new:
        background_tasks.add_task(
            _process_paper_trades,
            payload.symbol,
            payload.timeframe,
            payload.model_dump(exclude={"secret"}),
        )

    return WebhookAck(
        status="stored" if is_new else "duplicate",
        event_id=payload.event_id,
        timing=timing.to_dict(),
        analysis_would_run=analysis_would_run,
        calendar_warning=calendar_warning,
    )


@app.get("/timing/now")
def timing_now() -> dict:
    """Evaluate the Timing Agent against the current server time.
    Useful for testing the session-gating logic any day, any time —
    doesn't require a live market or a webhook."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    timing = evaluate_timing(now_iso)
    return {
        **timing.to_dict(),
        "analysis_would_run": should_run_analysis(timing),
    }


@app.get("/timing/at")
def timing_at(timestamp: str = Query(..., description="ISO-8601 UTC, e.g. 2026-08-10T09:00:00Z")) -> dict:
    """Same as /timing/now but for an arbitrary timestamp — lets you
    test Monday's London open, the overlap window, weekends, etc.
    without waiting for the clock."""
    timing = evaluate_timing(timestamp)
    return {
        **timing.to_dict(),
        "analysis_would_run": should_run_analysis(timing),
    }


@app.post("/agents/analysis/run")
def trigger_analysis(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    bars: int = Query(default=10, le=50, description="how many recent bars to feed the model"),
    ignore_timing_gate: bool = Query(
        default=False,
        description="For manual testing only. In normal operation the Timing gate decides whether this runs.",
    ),
    x_webhook_secret: str | None = Header(default=None, alias="X-Webhook-Secret"),
) -> dict:
    _check_secret(x_webhook_secret)
    latest = get_latest(symbol=symbol, timeframe=timeframe)
    if latest is None:
        raise HTTPException(status_code=404, detail="no market data yet for that symbol/timeframe")

    if not ignore_timing_gate:
        timing = evaluate_timing(latest["timestamp"])
        if not should_run_analysis(timing):
            return {
                "skipped": True,
                "reason": "outside London/NY session",
                "timing": timing.to_dict(),
            }

    recent_bars = get_recent(symbol=symbol, timeframe=timeframe, limit=bars)
    recent_bars.reverse()  # oldest -> newest, matches the prompt's expectation

    try:
        opinion = run_analysis(symbol=symbol, timeframe=timeframe, bars=recent_bars)
    except AnalysisAgentError as e:
        raise HTTPException(status_code=502, detail=str(e))

    save_opinion(
        agent="analysis",
        symbol=symbol,
        timeframe=timeframe,
        timestamp=opinion.timestamp,
        opinion=opinion.to_dict(),
    )
    return {"skipped": False, "opinion": opinion.to_dict()}


@app.get("/agents/analysis/latest")
def read_latest_analysis(
    symbol: str = Query(...),
    timeframe: str = Query(...),
) -> dict:
    opinion = get_latest_opinion(agent="analysis", symbol=symbol, timeframe=timeframe)
    if opinion is None:
        raise HTTPException(status_code=404, detail="no analysis opinion stored yet")
    return opinion


@app.get("/agents/analysis/history")
def read_analysis_history(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    limit: int = Query(default=20, le=200),
) -> list[dict]:
    """Newest-first history of Analysis opinions — read-only, for
    after-the-fact investigation (was the LLM actually re-run each
    time, did key_data/reasoning actually change, etc). /latest only
    ever shows the single most recent opinion."""
    return get_recent_opinions(agent="analysis", symbol=symbol, timeframe=timeframe, limit=limit)


@app.post("/agents/news/run")
def trigger_news(
    symbol: str = Query(default=NEWS_SYMBOL),
    x_webhook_secret: str | None = Header(default=None, alias="X-Webhook-Secret"),
) -> dict:
    _check_secret(x_webhook_secret)
    try:
        opinion = run_news(symbol=symbol)
    except NewsAgentError as e:
        raise HTTPException(status_code=502, detail=str(e))

    save_opinion(
        agent="news",
        symbol=symbol,
        timeframe=NEWS_TIMEFRAME,
        timestamp=opinion.timestamp,
        opinion=opinion.to_dict(),
    )
    return {"opinion": opinion.to_dict()}


@app.get("/agents/news/latest")
def read_latest_news(
    symbol: str = Query(default=NEWS_SYMBOL),
) -> dict:
    opinion = get_latest_opinion(agent="news", symbol=symbol, timeframe=NEWS_TIMEFRAME)
    if opinion is None:
        raise HTTPException(status_code=404, detail="no news opinion stored yet")
    return opinion


@app.post("/agents/macro/run")
def trigger_macro(
    symbol: str = Query(default=MACRO_SYMBOL),
    x_webhook_secret: str | None = Header(default=None, alias="X-Webhook-Secret"),
) -> dict:
    _check_secret(x_webhook_secret)
    try:
        opinion = run_macro(symbol=symbol)
    except MacroAgentError as e:
        raise HTTPException(status_code=502, detail=str(e))

    save_opinion(
        agent="macro",
        symbol=symbol,
        timeframe=MACRO_TIMEFRAME,
        timestamp=opinion.timestamp,
        opinion=opinion.to_dict(),
    )
    return {"opinion": opinion.to_dict()}


@app.get("/agents/macro/latest")
def read_latest_macro(
    symbol: str = Query(default=MACRO_SYMBOL),
) -> dict:
    opinion = get_latest_opinion(agent="macro", symbol=symbol, timeframe=MACRO_TIMEFRAME)
    if opinion is None:
        raise HTTPException(status_code=404, detail="no macro opinion stored yet")
    return opinion


@app.get("/coordinator/decide")
def coordinator_decide(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    persist: bool = Query(default=True, description="build and store a trade candidate"),
    x_webhook_secret: str | None = Header(default=None, alias="X-Webhook-Secret"),
) -> dict:
    _check_secret(x_webhook_secret)
    if persist:
        candidate = create_candidate(symbol=symbol, timeframe=timeframe)
        save_decision(
            symbol=symbol,
            timeframe=timeframe,
            timestamp=candidate["decision"]["timestamp"],
            decision=candidate["decision"],
        )
        return {**candidate["decision"], "candidate_id": candidate["candidate_id"]}
    # persist=false is a preview — compute without creating a candidate
    decision = compute_decision(symbol=symbol, timeframe=timeframe)
    return decision.to_dict()


@app.get("/candidates/latest")
def candidates_latest(
    symbol: str = Query(...),
    timeframe: str = Query(...),
) -> dict:
    """Read-only — the current trade candidate exactly as Risk/
    Execution would see it: the frozen bar, the exact opinions the
    decision was scored from, and whatever Risk/Execution results
    have been attached to it so far (either may still be null)."""
    candidate = get_latest_candidate(symbol=symbol, timeframe=timeframe)
    if candidate is None:
        raise HTTPException(status_code=404, detail="no trade candidate exists yet")
    return candidate


@app.get("/candidates/history")
def candidates_history(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    limit: int = Query(default=20, le=200),
) -> list[dict]:
    return get_candidate_history(symbol=symbol, timeframe=timeframe, limit=limit)


@app.get("/candidates/history/outcomes")
def candidates_history_outcomes(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    limit: int = Query(default=20, le=200),
    horizons: str = Query(
        default="15,30,60",
        description="comma-separated minutes — only used as a fallback for candidates that never became a real trade",
    ),
) -> list[dict]:
    """Tier 2.3 rebuild of outcome tracking (supersedes
    /coordinator/history/outcomes for anything going forward — that
    older endpoint is kept as-is since it reads a table with no
    candidate_id to link a real trade to). For each directional
    candidate, prefers a real closed paper trade's actual pnl_usd
    over the original hypothetical price-horizon estimate — the
    estimate is now only used as a fallback for candidates that never
    became a trade at all. no_trade/insufficient_data candidates get
    outcome=None, same "nothing to score" rule as before."""
    try:
        horizon_list = [int(h.strip()) for h in horizons.split(",") if h.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail="horizons must be comma-separated integers")

    candidates = get_candidate_history(symbol=symbol, timeframe=timeframe, limit=limit)
    results = []
    for candidate in candidates:
        outcome = compute_outcome_for_candidate(candidate, horizons=horizon_list)
        results.append({**candidate, "outcome": outcome})
    return results


@app.get("/candidates/history/outcomes/summary")
def candidates_history_outcomes_summary(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    limit: int = Query(default=100, le=500),
    horizons: str = Query(default="15,30,60"),
) -> dict:
    """Aggregated version of the above — real win rate/total P&L from
    closed trades, plus hypothetical horizon accuracy for candidates
    that never became a trade, kept as two separate sections (never
    blended into one number). Built for the same COORDINATOR_THRESHOLD
    tuning use case /coordinator/history/outcomes was originally
    built for — replaces manually pulling and tallying decision-
    history rows by hand."""
    try:
        horizon_list = [int(h.strip()) for h in horizons.split(",") if h.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail="horizons must be comma-separated integers")

    candidates = get_candidate_history(symbol=symbol, timeframe=timeframe, limit=limit)
    outcomes = [compute_outcome_for_candidate(c, horizons=horizon_list) for c in candidates]
    return summarize_outcomes(outcomes)


def _parse_replay_horizons(horizons: str) -> list[int]:
    try:
        return [int(h.strip()) for h in horizons.split(",") if h.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail="horizons must be comma-separated integers")


def _parse_replay_weights(weights: str | None) -> dict | None:
    """None means "use the live WEIGHTS config" — distinct from an
    empty dict, which would be a real (if unusual) request to zero
    out every agent's weight."""
    if weights is None:
        return None
    try:
        parsed = json.loads(weights)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=400,
            detail='weights must be a JSON object string, e.g. {"analysis":0.4,"news":0.25,"timing":0.2,"macro":0.15}',
        )
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="weights must be a JSON object")
    return parsed


# NOTE: both of these must be registered BEFORE /candidates/{candidate_id}
# (and before /candidates/{candidate_id}/replay below) — FastAPI matches
# routes in registration order, and /candidates/{candidate_id}/replay
# would otherwise swallow "/candidates/history/replay" by treating
# "history" as the candidate_id path param (hit this during Tier 2.5
# smoke testing; same class of ordering issue /candidates/history/outcomes
# above already had to avoid relative to /candidates/{candidate_id}).
@app.get("/candidates/history/replay")
def candidates_history_replay(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    limit: int = Query(default=50, le=500),
    weights: str = Query(default=None),
    threshold: float = Query(default=None),
    min_available_weight: float = Query(default=None),
    only_changed: bool = Query(default=False, description="only return candidates whose replayed decision differs from what actually happened"),
    include_outcome: bool = Query(default=False),
    horizons: str = Query(default="15,30,60"),
) -> list[dict]:
    """Bulk version of the single-candidate replay below, over recent
    candidate history — the tool for config-tuning questions like "if
    COORDINATOR_THRESHOLD had been 35 this whole time, how many of the
    last 100 decisions would have flipped?"."""
    return replay_candidates_for_symbol(
        symbol=symbol,
        timeframe=timeframe,
        weights=_parse_replay_weights(weights),
        threshold=threshold,
        min_available_weight=min_available_weight,
        limit=limit,
        only_changed=only_changed,
        include_outcome=include_outcome,
        outcome_horizons=_parse_replay_horizons(horizons) if include_outcome else None,
    )


@app.get("/candidates/history/replay/summary")
def candidates_history_replay_summary(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    limit: int = Query(default=100, le=500),
    weights: str = Query(default=None),
    threshold: float = Query(default=None),
    min_available_weight: float = Query(default=None),
) -> dict:
    """Aggregated transition counts (changed/unchanged, and
    original-decision -> replayed-decision breakdown) — the at-a-
    glance answer before reading individual replayed candidates."""
    results = replay_candidates_for_symbol(
        symbol=symbol,
        timeframe=timeframe,
        weights=_parse_replay_weights(weights),
        threshold=threshold,
        min_available_weight=min_available_weight,
        limit=limit,
    )
    return summarize_replay(results)


@app.get("/candidates/{candidate_id}")
def candidate_by_id(candidate_id: str) -> dict:
    candidate = get_candidate_by_id(candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail=f"no candidate found with id={candidate_id}")
    return candidate


@app.get("/candidates/{candidate_id}/replay")
def candidate_replay(
    candidate_id: str,
    weights: str = Query(default=None, description='JSON object, e.g. {"analysis":0.4,"news":0.25,"timing":0.2,"macro":0.15} — omit to use the current live weights'),
    threshold: float = Query(default=None, description="omit to use the current live COORDINATOR_THRESHOLD"),
    min_available_weight: float = Query(default=None, description="omit to use the current live MIN_AVAILABLE_WEIGHT"),
    include_outcome: bool = Query(default=False, description="also compute the hypothetical horizon outcome for the replayed decision"),
    horizons: str = Query(default="15,30,60"),
) -> dict:
    """Tier 2.5: re-scores ONE candidate's frozen opinions_used under a
    config — the live config by default, or an explicit hypothetical
    override for weights/threshold/min_available_weight. Never mutates
    the original candidate or opens a trade; purely a read-only
    recompute for answering "what would the Coordinator have decided
    here under a different config?"."""
    candidate = get_candidate_by_id(candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail=f"no candidate found with id={candidate_id}")

    return replay_candidate(
        candidate,
        weights=_parse_replay_weights(weights),
        threshold=threshold,
        min_available_weight=min_available_weight,
        include_outcome=include_outcome,
        outcome_horizons=_parse_replay_horizons(horizons) if include_outcome else None,
    )


@app.get("/trades/open")
def trades_open(
    symbol: str = Query(...),
    timeframe: str = Query(...),
) -> list[dict]:
    """Trades still live — status "pending_fill" (limit order waiting
    for price) or "open" (filled, position live). Read-only, no
    secret needed — same pattern as /candidates/*."""
    return get_open_or_pending_trades(symbol=symbol, timeframe=timeframe)


@app.get("/trades/history")
def trades_history(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    limit: int = Query(default=20, le=200),
) -> list[dict]:
    """Closed trades, newest first, with realized pnl_usd and
    exit_reason ("stop_hit" | "target_hit")."""
    return get_recent_trades(symbol=symbol, timeframe=timeframe, limit=limit)


@app.get("/trades/{trade_id}")
def trade_by_id(trade_id: str) -> dict:
    trade = get_trade_by_id(trade_id)
    if trade is None:
        raise HTTPException(status_code=404, detail=f"no trade found with id={trade_id}")
    return trade


@app.get("/account/risk")
def account_risk_status() -> dict:
    """Tier 2.10: the account-wide risk snapshot — live-computed from
    real closed paper trades (app/account_risk.py), the same figures
    /agents/risk/evaluate uses internally to gate/size trades, exposed
    here read-only so the dashboard (or anyone) can see current
    drawdown/daily-loss status without triggering a risk evaluation.
    Account-wide by design (not scoped to a symbol/timeframe) — the
    account's risk budget is one account-wide number regardless of how
    many symbols end up trading against it. No secret needed, same
    pattern as /trades/* and /candidates/*."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    closed_trades = get_all_closed_trades_chronological()
    current_drawdown_used = compute_current_drawdown_used(trades=closed_trades)
    daily_loss_used = compute_daily_loss_used(now_iso, trades=closed_trades)
    return {
        "as_of": now_iso,
        "account_balance": ACCOUNT_BALANCE,
        "max_drawdown": MAX_DRAWDOWN,
        "current_drawdown_used": current_drawdown_used,
        "remaining_drawdown_room": round(MAX_DRAWDOWN - current_drawdown_used, 2),
        "daily_loss_limit": DAILY_LOSS_LIMIT,
        "daily_loss_used": daily_loss_used,
        "remaining_daily_loss_room": round(DAILY_LOSS_LIMIT - daily_loss_used, 2),
        "closed_trades_considered": len(closed_trades),
    }


@app.get("/coordinator/history")
def coordinator_history(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    limit: int = Query(default=20, le=200),
) -> list[dict]:
    return get_recent_decisions(symbol=symbol, timeframe=timeframe, limit=limit)


@app.get("/coordinator/history/outcomes")
def coordinator_history_outcomes(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    limit: int = Query(default=20, le=200),
    horizons: str = Query(default="15,30,60", description="comma-separated minutes"),
) -> list[dict]:
    """Same as /coordinator/history, but for each directional decision
    (enter_long/enter_short) also attaches whether price actually moved
    the predicted way at each horizon. no_trade/insufficient_data rows
    get outcomes=None — nothing to score. Computed live from stored
    bars each call; nothing persisted, nothing recomputed in the
    background."""
    try:
        horizon_list = [int(h.strip()) for h in horizons.split(",") if h.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail="horizons must be comma-separated integers")

    decisions = get_recent_decisions(symbol=symbol, timeframe=timeframe, limit=limit)
    results = []
    for decision in decisions:
        outcomes = compute_outcomes_for_decision(
            symbol=symbol, timeframe=timeframe, decision=decision, horizons=horizon_list
        )
        results.append({**decision, "outcomes": outcomes})
    return results


@app.get("/agents/risk/evaluate")
def risk_evaluate(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    x_webhook_secret: str | None = Header(default=None, alias="X-Webhook-Secret"),
) -> dict:
    """Reworked (Tier 2.1) to act on the current trade candidate as a
    whole, instead of independently fetching 'latest decision' and
    'latest bar' — those two used to potentially come from different
    moments if a new bar arrived between the two separate queries.
    The result is written back onto the SAME candidate row.

    Tier 2.2: this is now a two-stage endpoint, and which stage runs
    depends on the candidate's current state — same URL, called twice
    across one candidate's lifecycle:
      - No Execution attached yet (or Execution didn't produce a valid
        plan) -> the "gate" stage: free, no stop price needed, checks
        position limits and drawdown room only. Result includes
        "pending_execution" when it's clear to let Execution run.
      - Execution has attached a validated (status="planned") order
        -> the "size" stage: sizes the position from Execution's
        actual entry_price/stop_loss instead of an ATR estimate.
    Either way the result overwrites this candidate's risk_json — a
    candidate carries exactly one current Risk opinion, not a gate
    opinion and a size opinion side by side.

    Tier 2.3: the gate stage's position-limit check now uses the LIVE
    count of open/pending paper trades (app/paper_trades.py) instead
    of only the static CURRENT_OPEN_POSITIONS env var. And when the
    size stage approves or modifies, a paper trade is opened right
    here as a side effect — that's the natural commit point: Risk
    deciding a real size IS the decision to actually take the trade
    (paper-only, so there's no reason to gate that behind a further
    manual step).

    Tier 3.1 (causal integrity): once a paper trade has been committed
    from this candidate, its risk_json is locked — calling this again
    (a dashboard double-click, a retry) no longer re-runs the gate/size
    math and overwrites it. It short-circuits and returns the ALREADY-
    COMMITTED risk opinion and trade unchanged, so the candidate can
    never end up describing a different size/decision than the trade
    that was actually taken from it."""
    _check_secret(x_webhook_secret)
    try:
        candidate = get_current_candidate(symbol=symbol, timeframe=timeframe)
    except CandidateError as e:
        raise HTTPException(status_code=404, detail=str(e))

    existing_trade = get_committed_trade(candidate["candidate_id"])
    if existing_trade is not None:
        return {
            "candidate_id": candidate["candidate_id"],
            "coordinator_decision": candidate["decision"],
            "risk_opinion": candidate["risk"],
            "trade": existing_trade,
            "locked": True,
        }

    # Tier 2.10: live account-level figures, computed fresh from real
    # closed paper trades for every call — same "pass the live value
    # in, env var is only the fallback" pattern Tier 2.3 already
    # established for current_open_positions.
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    closed_trades = get_all_closed_trades_chronological()
    current_drawdown_used = compute_current_drawdown_used(trades=closed_trades)
    daily_loss_used = compute_daily_loss_used(now_iso, trades=closed_trades)

    execution = candidate["execution"]
    is_size_stage = execution is not None and execution.get("status") == "planned"
    if is_size_stage:
        risk_opinion = size_position(
            symbol=symbol,
            timeframe=timeframe,
            entry_price=execution["entry_price"],
            stop_loss=execution["stop_loss"],
            current_drawdown_used=current_drawdown_used,
            daily_loss_used=daily_loss_used,
        )
    else:
        open_positions = get_open_trade_count(symbol=symbol, timeframe=timeframe)
        risk_opinion = evaluate_risk_gate(
            symbol=symbol,
            timeframe=timeframe,
            coordinator_decision=candidate["decision"],
            current_open_positions=open_positions,
            current_drawdown_used=current_drawdown_used,
            daily_loss_used=daily_loss_used,
        )

    # Tier 3.1: attach BEFORE committing a trade from it — the
    # candidate's recorded risk_json is what the trade gets built from,
    # not an in-memory value that happens to match. If another request
    # committed a trade for this same candidate in the tiny window
    # since existing_trade was checked above, attach_risk_result's
    # write-once guard catches it here and this falls back to
    # returning that trade's real state instead of a second one.
    try:
        record_risk_result(candidate["candidate_id"], risk_opinion.to_dict())
    except CandidateLockedError:
        locked_candidate = get_candidate_by_id(candidate["candidate_id"])
        return {
            "candidate_id": candidate["candidate_id"],
            "coordinator_decision": candidate["decision"],
            "risk_opinion": locked_candidate["risk"],
            "trade": get_committed_trade(candidate["candidate_id"]),
            "locked": True,
        }

    trade = None
    if is_size_stage and risk_opinion.decision in ("approve", "modify"):
        candidate_for_trade = {**candidate, "risk": risk_opinion.to_dict()}
        trade = open_trade_from_candidate(candidate_for_trade)

    # Also written to the older agent_opinions table — /system/status
    # and the dashboard's existing Risk display still read from there.
    save_opinion(
        agent="risk",
        symbol=symbol,
        timeframe=timeframe,
        timestamp=risk_opinion.timestamp,
        opinion=risk_opinion.to_dict(),
    )
    return {
        "candidate_id": candidate["candidate_id"],
        "coordinator_decision": candidate["decision"],
        "risk_opinion": risk_opinion.to_dict(),
        "trade": trade,
    }


@app.get("/agents/execution/plan")
def execution_plan(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    x_webhook_secret: str | None = Header(default=None, alias="X-Webhook-Secret"),
) -> dict:
    """Reworked (Tier 2.1): requires the CURRENT candidate to already
    have a Risk result attached — not just "some" Risk opinion from
    anywhere. This closes the original gap where Execution could
    combine a Risk approval for one decision with a different,
    newer Coordinator decision. key_levels now come from the exact
    Analysis opinion frozen inside this candidate's opinions_used,
    not a fresh independent lookup that could have moved on.

    Tier 2.2: the required Risk result is now the "gate" opinion
    (decision in "pending_execution"/"approve"/"modify" — i.e. the
    gate cleared at some point; "approve"/"modify" cover re-running
    this after a size pass already happened). A "reject" or
    "no_action" gate blocks Execution outright — no point spending an
    LLM call on a trade Risk has already ruled out. This call no
    longer needs or uses a size — it proposes geometry only; call
    /agents/risk/evaluate again afterward to size the position from
    the entry_price/stop_loss this produces.

    Tier 3.1 (causal integrity): once a paper trade has been committed
    from this candidate, its execution_json is locked. Re-calling this
    (e.g. a double-click after the trade already opened) short-circuits
    BEFORE spending a paid LLM call, returning the existing execution
    plan unchanged — never a second, different geometry for a trade
    whose real entry/stop/size are already fixed."""
    _check_secret(x_webhook_secret)
    try:
        candidate = get_current_candidate(symbol=symbol, timeframe=timeframe)
    except CandidateError as e:
        raise HTTPException(status_code=404, detail=str(e))

    existing_trade = get_committed_trade(candidate["candidate_id"])
    if existing_trade is not None:
        return {
            "candidate_id": candidate["candidate_id"],
            "execution_opinion": candidate["execution"],
            "trade": existing_trade,
            "locked": True,
        }

    risk = candidate["risk"]
    if risk is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "the current trade candidate has not cleared Risk's gate yet — "
                "call /agents/risk/evaluate first (it must be run before Execution can "
                "act on this same candidate)"
            ),
        )
    if risk.get("decision") in ("reject", "no_action"):
        raise HTTPException(
            status_code=409,
            detail=f"Risk blocks this candidate (decision='{risk.get('decision')}'): {risk.get('reasoning')}",
        )

    analysis_opinion = candidate["decision"].get("opinions_used", {}).get("analysis")
    key_levels = (analysis_opinion or {}).get("key_data", {}).get("key_levels")

    try:
        execution_opinion = plan_execution(
            symbol=symbol,
            timeframe=timeframe,
            coordinator_decision=candidate["decision"],
            latest_bar=candidate["bar"],
            analysis_key_levels=key_levels,
        )
    except ExecutionAgentError as e:
        raise HTTPException(status_code=502, detail=str(e))

    # Tier 3.1: defense-in-depth for the tiny race window between the
    # existing_trade check above and this attach — see risk_evaluate's
    # matching comment for the same pattern.
    try:
        record_execution_result(candidate["candidate_id"], execution_opinion.to_dict())
    except CandidateLockedError:
        locked_candidate = get_candidate_by_id(candidate["candidate_id"])
        return {
            "candidate_id": candidate["candidate_id"],
            "execution_opinion": locked_candidate["execution"],
            "trade": get_committed_trade(candidate["candidate_id"]),
            "locked": True,
        }
    save_opinion(
        agent="execution",
        symbol=symbol,
        timeframe=timeframe,
        timestamp=execution_opinion.timestamp,
        opinion=execution_opinion.to_dict(),
    )
    return {"candidate_id": candidate["candidate_id"], "execution_opinion": execution_opinion.to_dict()}


@app.get("/market-state/latest", response_model=MarketStateOut)
def read_latest(
    symbol: str = Query(...),
    timeframe: str = Query(...),
) -> MarketStateOut | JSONResponse:
    data = get_latest(symbol=symbol, timeframe=timeframe)
    if data is None:
        raise HTTPException(status_code=404, detail="no data yet for that symbol/timeframe")
    return MarketStateOut(**data)


@app.get("/market-state/recent", response_model=list[MarketStateOut])
def read_recent(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    limit: int = Query(default=20, le=200),
) -> list[MarketStateOut]:
    rows = get_recent(symbol=symbol, timeframe=timeframe, limit=limit)
    return [MarketStateOut(**row) for row in rows]
