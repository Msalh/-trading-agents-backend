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

This backend is intentionally standalone — no dependency on any
other existing project.
"""

import logging
import os
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.analysis_agent import AnalysisAgentError, run_analysis
from app.coordinator import compute_decision
from app.macro_agent import MacroAgentError, run_macro
from app.models import MarketStateOut, MarketStatePayload, WebhookAck
from app.news_agent import NewsAgentError, run_news
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


@app.get("/")
def health_check() -> dict:
    return {"status": "ok"}


@app.post("/webhook/tradingview", response_model=WebhookAck)
def receive_market_state(
    payload: MarketStatePayload,
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

    # This used to only COMPUTE the gate decision without acting on it
    # (a leftover from Sprint 2, before Analysis existed) — meaning
    # Analysis never actually ran automatically, only ever via a
    # manual /agents/analysis/run call or the dashboard's Run button.
    # Now it actually runs Analysis here, matching the roadmap's
    # original design (event-driven, fires on the webhook). Only for
    # genuinely new bars — a retried/duplicate delivery shouldn't
    # trigger a second paid LLM call for the same bar.
    if is_new and analysis_would_run:
        try:
            recent_bars = get_recent(symbol=payload.symbol, timeframe=payload.timeframe, limit=10)
            recent_bars.reverse()
            opinion = run_analysis(symbol=payload.symbol, timeframe=payload.timeframe, bars=recent_bars)
            save_opinion(
                agent="analysis",
                symbol=payload.symbol,
                timeframe=payload.timeframe,
                timestamp=opinion.timestamp,
                opinion=opinion.to_dict(),
            )

            # Auto-compute and persist a Coordinator decision right
            # after a fresh Analysis opinion lands — this is what
            # actually populates /coordinator/history on its own,
            # instead of requiring a manual "Compute & Save" click on
            # the dashboard. Every 5-minute bar during an active
            # session now produces one decision-history row, using
            # whatever the latest News/Macro/Timing opinions are at
            # that moment. Wrapped separately so a Coordinator failure
            # (e.g. a storage hiccup) doesn't also erase the Analysis
            # opinion we just successfully saved above.
            try:
                decision = compute_decision(symbol=payload.symbol, timeframe=payload.timeframe)
                save_decision(
                    symbol=payload.symbol,
                    timeframe=payload.timeframe,
                    timestamp=decision.timestamp,
                    decision=decision.to_dict(),
                )
            except Exception as e:  # noqa: BLE001 - never let this break the webhook ack
                logging.getLogger("webhook").error("auto-coordinator failed: %s", e)
        except AnalysisAgentError as e:
            # Don't let an Analysis failure break the webhook ack —
            # TradingView still needs a clean response either way.
            logging.getLogger("webhook").error("auto-analysis failed: %s", e)

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
