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

This backend is intentionally standalone — no dependency on any
other existing project.
"""

import logging
import os
from datetime import datetime, timezone

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.analysis_agent import AnalysisAgentError, run_analysis
from app.coordinator import compute_decision
from app.execution_agent import ExecutionAgentError, plan_execution
from app.macro_agent import MacroAgentError, run_macro
from app.models import MarketStateOut, MarketStatePayload, WebhookAck
from app.news_agent import NewsAgentError, run_news
from app.outcomes import HORIZON_MINUTES_DEFAULT, compute_outcomes_for_decision
from app.risk_agent import evaluate_risk
from app.scheduler import (
    MACRO_SYMBOL,
    MACRO_TIMEFRAME,
    NEWS_SYMBOL,
    NEWS_TIMEFRAME,
    start_scheduler,
    stop_scheduler,
)
from app.storage import (
    get_last_opinion_timestamps,
    get_last_webhook_received,
    get_latest,
    get_latest_opinion,
    get_recent,
    get_recent_decisions,
    get_recent_opinions,
    init_db,
    save_decision,
    save_event,
    save_opinion,
    delete_market_state_event,
    wipe_all_data,
)
from app.timing_agent import evaluate_timing, should_run_analysis

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

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
            "news": int(os.environ.get("NEWS_INTERVAL_MINUTES", "20")),
            "macro": int(os.environ.get("MACRO_INTERVAL_MINUTES", "20")),
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


def _run_auto_analysis_and_coordinator(symbol: str, timeframe: str) -> None:
    """Runs Analysis + Coordinator for a freshly-arrived bar. Called via
    BackgroundTasks so the webhook can ack TradingView immediately —
    the LLM call is the slow part, and TradingView's own delivery
    timeout is short enough that waiting for it inline caused
    legitimate "request took too long and timed out" failures at
    TradingView even though the work itself completed successfully
    a few seconds later."""
    try:
        recent_bars = get_recent(symbol=symbol, timeframe=timeframe, limit=10)
        recent_bars.reverse()
        opinion = run_analysis(symbol=symbol, timeframe=timeframe, bars=recent_bars)
        save_opinion(
            agent="analysis",
            symbol=symbol,
            timeframe=timeframe,
            timestamp=opinion.timestamp,
            opinion=opinion.to_dict(),
        )

        # Auto-compute and persist a Coordinator decision right after a
        # fresh Analysis opinion lands — this is what actually
        # populates /coordinator/history on its own. Wrapped separately
        # so a Coordinator failure doesn't erase the Analysis opinion
        # just saved above.
        try:
            decision = compute_decision(symbol=symbol, timeframe=timeframe)
            save_decision(
                symbol=symbol,
                timeframe=timeframe,
                timestamp=decision.timestamp,
                decision=decision.to_dict(),
            )
        except Exception as e:  # noqa: BLE001 - background task, log and move on
            logging.getLogger("webhook").error("auto-coordinator failed: %s", e)
    except AnalysisAgentError as e:
        logging.getLogger("webhook").error("auto-analysis failed: %s", e)


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

    # Only for genuinely new bars — a retried/duplicate delivery
    # shouldn't trigger a second paid LLM call for the same bar.
    # Scheduled as a background task: the HTTP response below returns
    # to TradingView immediately, and the (slower) LLM call + Coordinator
    # run after, off the request/response path entirely.
    if is_new and analysis_would_run:
        background_tasks.add_task(
            _run_auto_analysis_and_coordinator, payload.symbol, payload.timeframe
        )

    return WebhookAck(
        status="stored" if is_new else "duplicate",
        event_id=payload.event_id,
        timing=timing.to_dict(),
        analysis_would_run=analysis_would_run,
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
) -> dict:
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
) -> dict:
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
) -> dict:
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
    persist: bool = Query(default=True, description="store this decision in the history log"),
) -> dict:
    decision = compute_decision(symbol=symbol, timeframe=timeframe)
    if persist:
        save_decision(
            symbol=symbol,
            timeframe=timeframe,
            timestamp=decision.timestamp,
            decision=decision.to_dict(),
        )
    return decision.to_dict()


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
) -> dict:
    recent_decisions = get_recent_decisions(symbol=symbol, timeframe=timeframe, limit=1)
    if not recent_decisions:
        raise HTTPException(
            status_code=404,
            detail="no Coordinator decision stored yet — call /coordinator/decide first",
        )
    latest_decision = recent_decisions[0]
    latest_bar = get_latest(symbol=symbol, timeframe=timeframe)

    risk_opinion = evaluate_risk(
        symbol=symbol,
        timeframe=timeframe,
        coordinator_decision=latest_decision,
        latest_bar=latest_bar,
    )
    save_opinion(
        agent="risk",
        symbol=symbol,
        timeframe=timeframe,
        timestamp=risk_opinion.timestamp,
        opinion=risk_opinion.to_dict(),
    )
    return {
        "coordinator_decision": latest_decision,
        "risk_opinion": risk_opinion.to_dict(),
    }


@app.get("/agents/execution/plan")
def execution_plan(
    symbol: str = Query(...),
    timeframe: str = Query(...),
) -> dict:
    """Reads the most recently STORED Risk opinion and Coordinator
    decision (does not recompute them) plus the latest bar and
    Analysis's key_levels, and asks Execution to turn an approved
    trade into a concrete paper order. No LLM call at all if Risk
    didn't approve/modify anything — see plan_execution's no_action
    short-circuit."""
    risk_opinion = get_latest_opinion(agent="risk", symbol=symbol, timeframe=timeframe)
    if risk_opinion is None:
        raise HTTPException(
            status_code=404,
            detail="no Risk opinion stored yet — call /agents/risk/evaluate first",
        )

    recent_decisions = get_recent_decisions(symbol=symbol, timeframe=timeframe, limit=1)
    if not recent_decisions:
        raise HTTPException(status_code=404, detail="no Coordinator decision stored yet")
    latest_decision = recent_decisions[0]

    latest_bar = get_latest(symbol=symbol, timeframe=timeframe)
    analysis_opinion = get_latest_opinion(agent="analysis", symbol=symbol, timeframe=timeframe)
    key_levels = (analysis_opinion or {}).get("key_data", {}).get("key_levels")

    try:
        execution_opinion = plan_execution(
            symbol=symbol,
            timeframe=timeframe,
            risk_evaluation=risk_opinion,
            coordinator_decision=latest_decision,
            latest_bar=latest_bar,
            analysis_key_levels=key_levels,
        )
    except ExecutionAgentError as e:
        raise HTTPException(status_code=502, detail=str(e))

    save_opinion(
        agent="execution",
        symbol=symbol,
        timeframe=timeframe,
        timestamp=execution_opinion.timestamp,
        opinion=execution_opinion.to_dict(),
    )
    return {"execution_opinion": execution_opinion.to_dict()}


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
