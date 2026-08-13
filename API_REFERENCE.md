# API Reference — Trading Agents Backend

All endpoints are on the deployed base URL:
`https://web-production-c67aa.up.railway.app`

None of these require auth except `/webhook/tradingview`, which
checks the `secret` field in the payload against `WEBHOOK_SECRET`.

---

## Health

### `GET /`
Health check. Returns `{"status": "ok"}`.

### `GET /system/status?symbol=MNQ1!`
Operational snapshot: is the scheduler running, when each agent last
ran, when the last webhook was received, and how many minutes ago
each of those was (`null` if it's never happened yet).

```json
{
  "server_time_utc": "2026-08-09T13:22:25Z",
  "scheduler_enabled": true,
  "scheduler_intervals_minutes": {"news": 20, "macro": 20},
  "last_webhook_received": "2026-08-09 13:22:25",
  "minutes_since_last_webhook": 4.2,
  "agents": {
    "analysis": {"last_run": "...", "minutes_since_last_run": 4.2},
    "news":     {"last_run": "...", "minutes_since_last_run": 12.8},
    "macro":    {"last_run": "...", "minutes_since_last_run": 6.1},
    "risk":     {"last_run": null,  "minutes_since_last_run": null}
  }
}
```

---

## Ingestion

### `POST /webhook/tradingview`
Receives one `market_state` bar-close event from the Pine Script
broadcaster. Body must match the schema in `app/models.py`
(`MarketStatePayload`) — see the Pine Script's `f_buildPayload()`
for the exact wire format. `secret` in the body is checked against
`WEBHOOK_SECRET` and never stored or echoed back.

Response includes the Timing Agent's live evaluation of the bar's
timestamp:

```json
{
  "status": "stored",            // or "duplicate" if event_id seen before
  "event_id": "MNQ1!:5m:2026-08-11T10:30:00Z",
  "timing": { "...": "TimingOpinion fields" },
  "analysis_would_run": true
}
```

---

## Market data

### `GET /market-state/latest?symbol=MNQ1!&timeframe=5m`
Most recent stored bar for that symbol/timeframe. 404 if none yet.

### `GET /market-state/recent?symbol=MNQ1!&timeframe=5m&limit=20`
Most recent N bars (max 200), newest first.

---

## Agents

Each agent has a `POST /agents/<name>/run` (triggers a fresh
run — costs money for LLM-backed agents) and a `GET
/agents/<name>/latest` (reads the last stored opinion, free).

### Analysis (LLM, bar-dependent)
- `POST /agents/analysis/run?symbol=MNQ1!&timeframe=5m&bars=10&ignore_timing_gate=false`
  - `bars`: how many recent bars to feed the model (max 50)
  - `ignore_timing_gate`: for manual testing outside London/NY hours only — normal operation respects the Timing gate and returns `{"skipped": true, ...}` outside sessions
- `GET /agents/analysis/latest?symbol=MNQ1!&timeframe=5m`

### News (LLM + hosted web search, not bar-dependent)
- `POST /agents/news/run?symbol=MNQ1!` (symbol optional, defaults to `NEWS_SYMBOL`)
- `GET /agents/news/latest?symbol=MNQ1!`
- Stored under `timeframe="global"` internally — not tied to any chart timeframe.

### Macro (LLM + hosted web search, not bar-dependent)
- `POST /agents/macro/run?symbol=MNQ1!`
- `GET /agents/macro/latest?symbol=MNQ1!`
- Same `timeframe="global"` pattern as News.

### Timing (pure logic, no LLM, no stored opinion)
Not exposed as its own agent endpoint — it's computed live wherever
needed (inside the webhook response, and inside the Coordinator).
Standalone test endpoints:
- `GET /timing/now` — evaluates the current server time
- `GET /timing/at?timestamp=2026-08-10T09:00:00Z` — evaluates any timestamp you give it

### Risk (deterministic logic, no LLM) — two-stage as of Tier 2.2
- `GET /agents/risk/evaluate?symbol=MNQ1!&timeframe=5m`
  - Acts on the current trade candidate (see Candidates below), not
    an independent "latest decision" lookup. Same URL runs one of two
    stages depending on the candidate's state — call it twice across
    one candidate's lifecycle:
    1. **Gate** (no Execution attached yet, or Execution hasn't
       produced a valid plan): checks position limits and drawdown
       room only — no stop price needed. Returns `pending_execution`
       (clear to let Execution run), `reject` (hard block:
       `max_positions_reached` / `drawdown_exhausted`), or `no_action`
       (Coordinator isn't directional).
    2. **Size** (Execution has attached a validated `status="planned"`
       order to this same candidate): sizes the position from
       Execution's actual `entry_price`/`stop_loss` —
       `risk_per_contract = |entry - stop| × $2/pt` — never from ATR.
       Returns `approve` / `modify` / `reject`.
  - Response includes `stage: "gate" | "size"` alongside `decision` so
    callers can tell which pass produced the result.
  - Returns 404 if no trade candidate exists yet, or the latest one is
    older than `CANDIDATE_MAX_AGE_MINUTES` — call `/coordinator/decide`
    first.

### Execution (LLM, geometry only — no size)
- `GET /agents/execution/plan?symbol=MNQ1!&timeframe=5m`
  - Requires the current candidate's Risk result to have cleared the
    gate (`pending_execution`/`approve`/`modify`) — 409 if Risk hasn't
    run yet, or if Risk's decision is `reject`/`no_action`.
  - Proposes `order_type` / `entry_price` / `stop_loss` / `targets` /
    `ready_now` for the Coordinator's direction. Does **not** take or
    return a contract size — Risk sizes the trade afterward (call
    `/agents/risk/evaluate` again once this returns) from the real
    stop distance this produces.
  - A deterministic geometry check runs after parsing (stop on the
    losing side of entry, targets on the profitable side, minimum
    reward:risk). A proposal that fails returns `status="invalid"`
    with `validation_error` set instead of being treated as a normal
    plan.
  - Paper-only — never places a real order.

### Trades (paper fill/P&L lifecycle, Tier 2.3) — read-only, no secret needed
A paper trade opens automatically the moment `/agents/risk/evaluate`'s
size stage returns `approve`/`modify` — there's no separate "open
trade" endpoint to call. Every new webhook bar (regardless of Timing/
kill-zone gating — price doesn't pause outside a kill zone) advances
every live trade: fills a `pending_fill` limit order once price
reaches it, and closes an `open` trade on a stop or nearest-target
hit, computing `pnl_usd = |exit − fill| × $2/pt × size` (sign per
direction). A bar that spans both stop and target in one move is
treated as the stop having been hit first (conservative — OHLC bars
don't carry true intrabar order). Only the nearest target is checked;
a multi-target plan fully closes at the first one reached, no partial
scale-out modeling yet.
- `GET /trades/open?symbol=MNQ1!&timeframe=5m` — trades still live
  (`pending_fill` or `open`).
- `GET /trades/history?symbol=MNQ1!&timeframe=5m&limit=20` — closed
  trades, newest first, with `exit_price` / `exit_reason`
  (`stop_hit` | `target_hit`) / `pnl_usd`.
- `GET /trades/{trade_id}` — a single trade by id.

`CURRENT_OPEN_POSITIONS` is now a fallback only — Risk's gate stage
uses the LIVE count from this table (`get_open_trade_count`) by
default, so `MAX_OPEN_POSITIONS` is enforced against reality instead
of a hand-updated number.

---

## Coordinator

### `GET /coordinator/decide?symbol=MNQ1!&timeframe=5m&persist=true`
Aggregates the latest Analysis/News/Macro/Timing opinions with fixed
weights (Analysis 40% / News 25% / Timing 20% / Macro 15%) into a
score and a decision (`enter_long` / `enter_short` / `no_trade` /
`insufficient_data`). Set `persist=false` to compute without writing
to history (e.g. for a "what-if" check).

### `GET /coordinator/history?symbol=MNQ1!&timeframe=5m&limit=20`
Most recent N persisted decisions, newest first.

---

## Environment variables

| Variable | Required | Default | Notes |
|---|---|---|---|
| `WEBHOOK_SECRET` | yes | — | must match the Pine Script's secret input |
| `ANTHROPIC_API_KEY` | yes | — | for Analysis/News/Macro |
| `DB_PATH` | no | `./data/market_state.db` | point at a Railway Volume mount for persistence |
| `ENABLE_SCHEDULER` | no | `false` | set `true` in production to auto-run News/Macro |
| `NEWS_SYMBOL` | no | `MNQ1!` | |
| `NEWS_INTERVAL_MINUTES` | no | `20` | |
| `MACRO_SYMBOL` | no | (= `NEWS_SYMBOL`) | |
| `MACRO_INTERVAL_MINUTES` | no | `20` | |
| `COORDINATOR_THRESHOLD` | no | `25` | placeholder — needs tuning against real history |
| `ACCOUNT_BALANCE` | no | `50000` | static/manual, Sprint 7 |
| `MAX_DRAWDOWN` | no | `2000` | |
| `CURRENT_DRAWDOWN_USED` | no | `0` | update by hand as the real account changes |
| `MAX_OPEN_POSITIONS` | no | `1` | enforced against the live open-paper-trade count as of Tier 2.3 |
| `CURRENT_OPEN_POSITIONS` | no | `0` | Tier 2.3: fallback only — normally superseded by the live paper-trade count |
| `BASE_POSITION_SIZE` | no | `1` | contracts |
| `RISK_FRACTION_PER_TRADE` | no | `0.5` | fraction of remaining drawdown room risked per trade |

---

## Typical end-to-end call sequence (manual testing)

```
1. POST /webhook/tradingview          (or wait for a live TradingView bar)
2. POST /agents/analysis/run?...      (ignore_timing_gate=true outside sessions)
3. POST /agents/news/run
4. POST /agents/macro/run
5. GET  /coordinator/decide?...       -> enter_long / enter_short / no_trade
6. GET  /agents/risk/evaluate?...     -> gate stage: pending_execution / reject / no_action
7. GET  /agents/execution/plan?...    -> order_type / entry_price / stop_loss / targets
8. GET  /agents/risk/evaluate?...     -> size stage: approve / modify / reject
                                          -- a paper trade opens automatically here on approve/modify
9. GET  /trades/open?...              -> confirm the position opened
   (later, on subsequent bars)
10. GET /trades/history?...           -> once a stop/target is hit, the trade shows up here closed with pnl_usd
```

In production with `ENABLE_SCHEDULER=true`, steps 1–4 happen on
their own (webhook + background scheduler) — only 5–8 need manual
triggering (or a future automation once the pipeline is trusted).
Note step 6 is called twice across the lifecycle (steps 6 and 8) —
same endpoint, different stage depending on whether Execution has
run yet. Steps 9–10 need no manual trigger at all — trade opening and
fill/close monitoring both happen automatically as side effects of
step 8 and every subsequent webhook bar, respectively.
