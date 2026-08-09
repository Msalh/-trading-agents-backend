"""
Sprint 1: receive market_state events from the TradingView Pine
Script broadcaster, validate them, store them, and expose them for
the Analysis Agent (built next) and the dashboard (built later).

Sprint 2: added the Timing/Session Agent — pure local logic (no LLM)
that runs on every webhook and decides whether the bar falls inside
a session worth analyzing. This is the gate described in the roadmap:
before any expensive LLM agent runs, Timing decides go/no-go.

This backend is intentionally standalone — no dependency on any
other existing project.
"""

import os
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse

from app.models import MarketStateOut, MarketStatePayload, WebhookAck
from app.storage import get_latest, get_recent, init_db, save_event
from app.timing_agent import evaluate_timing, should_run_analysis

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

app = FastAPI(title="Trading Agents Backend", version="0.1.0")


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
    # Sprint 2 scope: compute and surface the gate decision. The
    # Analysis Agent itself doesn't exist yet, so nothing is actually
    # skipped — this just proves the gate logic is correct against
    # real bar timestamps before anything expensive depends on it.

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
