# Trading Agents Backend — Sprint 1

Standalone FastAPI backend for the multi-agent MNQ trading system.
Sprint 1 scope only: receive `market_state` events from the
TradingView Pine Script broadcaster, validate them, store them, and
expose them for the next agent to read. No agents yet — this is just
the foundation.

## What's here

- `app/models.py` — Pydantic models matching the exact JSON payload
  the Pine Script sends
- `app/storage.py` — SQLite storage, idempotent on `event_id`
- `app/main.py` — the FastAPI app and routes

## Endpoints

- `GET /` — health check
- `POST /webhook/tradingview` — receives the market_state payload
- `GET /market-state/latest?symbol=MNQU6&timeframe=5m` — latest stored
  event for that symbol/timeframe
- `GET /market-state/recent?symbol=MNQU6&timeframe=5m&limit=20` —
  most recent N events

## Run locally

```bash
python -m venv venv
source venv/bin/activate       # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# edit .env and set WEBHOOK_SECRET to match the value in the
# Pine Script's "Market State Webhook Secret" input

export $(cat .env | xargs)     # or use a tool like python-dotenv/direnv
uvicorn app.main:app --reload
```

Test it without TradingView first:

```bash
curl -X POST http://localhost:8000/webhook/tradingview \
  -H "Content-Type: application/json" \
  -d '{
    "schema_version": "1.0",
    "event_id": "MNQU6:5m:2026-08-08T14:35:00Z",
    "symbol": "MNQU6",
    "source": "tradingview",
    "timeframe": "5m",
    "timestamp": "2026-08-08T14:35:00Z",
    "bar_status": "closed",
    "event_type": "bar_closed",
    "secret": "change-me-to-a-long-random-value",
    "open": 19850.25, "high": 19855.5, "low": 19848.0, "close": 19852.75,
    "volume": 1234,
    "session_name": "RTH", "is_rth": true, "trading_date": "2026-08-08",
    "rth_open": 19840.0, "previous_day_high": 19900.0, "previous_day_low": 19800.0,
    "overnight_high": 19870.0, "overnight_low": 19820.0,
    "vwap": 19851.0, "distance_from_vwap_points": 1.75, "atr": 25.5,
    "volume_ratio": 1.1, "nearest_liquidity_level": 19850.0,
    "nearest_liquidity_type": "rth_open", "distance_to_liquidity_ticks": 11,
    "overnight_high_status": null, "overnight_low_status": null,
    "previous_day_high_status": null, "previous_day_low_status": null,
    "trend_1m": "up", "trend_5m": "up", "trend_15m": "flat", "trend_1h": "down",
    "liquidity_sweep": false, "reclaim": false, "rejection": false,
    "displacement": false, "volume_spike": false
  }'
```

Then confirm it stored:

```bash
curl "http://localhost:8000/market-state/latest?symbol=MNQU6&timeframe=5m"
```

## Deploy to Railway

1. Push this folder to a GitHub repo (or `railway up` directly from
   this directory with the Railway CLI)
2. In the Railway project: **New Project → Deploy from GitHub repo**
   (or accept the CLI upload)
3. Railway auto-detects Python via `requirements.txt` and uses the
   `Procfile` to start the server — no extra config needed
4. In the Railway service **Variables** tab, add:
   - `WEBHOOK_SECRET` = the same value set in the Pine Script's
     "Market State Webhook Secret" input on TradingView
5. Once deployed, Railway gives you a public URL like
   `https://your-service.up.railway.app`
6. On TradingView, edit the alert for the indicator and set the
   webhook URL to:
   `https://your-service.up.railway.app/webhook/tradingview`
7. Confirm events are arriving:
   `https://your-service.up.railway.app/market-state/latest?symbol=MNQU6&timeframe=5m`

## Notes

- SQLite file is stored at `data/market_state.db`. On Railway, unless
  you attach a persistent volume, the filesystem resets on every
  redeploy — fine for now while testing, but before relying on this
  for real history, attach a Railway volume mounted at `/app/data`
  (or move to a managed Postgres, which Railway also offers).
- The `secret` field is never persisted or echoed back in any
  response — it's checked and dropped.
