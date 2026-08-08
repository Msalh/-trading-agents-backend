"""
Sprint 1 backend: receive market_state events from the TradingView
Pine Script broadcaster, validate them, store them, and expose them
for the Analysis Agent (built next) and the dashboard (built later).

This backend is intentionally standalone — no dependency on any
other existing project.
"""

import os

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse

from app.models import MarketStateOut, MarketStatePayload, WebhookAck
from app.storage import get_latest, get_recent, init_db, save_event

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
    return WebhookAck(
        status="stored" if is_new else "duplicate",
        event_id=payload.event_id,
    )


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
