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
  "analysis_would_run": true,
  "calendar_warning": null       // Tier 2.9 — see below
}
```

`calendar_warning` (Tier 2.9, calendar integrity) is `null` when the
payload's `trading_date` field agrees with what its own `timestamp`
implies under the CME/Globex session-rollover convention (NY local
time at/after 18:00 belongs to the next day's session), or a short
string describing the mismatch otherwise. A mismatch is logged and
surfaced here but never rejected — the bar is still stored either way
(`status` is unaffected).

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

As of Tier 2.9, `key_data.is_holiday` is `true` and `session_label` is
`"holiday"` on a US market holiday (New Year's, MLK Day, Presidents
Day, Good Friday, Memorial Day, Juneteenth, July 4th, Labor Day,
Thanksgiving, Christmas — see `app/trading_calendar.py`), same
treatment as a weekend: every `in_*_session` flag is `false` (so
Analysis won't auto-run even during nominal kill-zone clock hours) and
`flags` includes `"market_closed"`.

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

### Outcomes (Tier 2.4 rebuild) — read-only, no secret needed
Prefers a real closed paper trade's actual P&L over the original
Sprint 14 hypothetical price-horizon estimate — the estimate is now
only a fallback for candidates that never became a trade at all
(rejected by Risk, never manually run, Execution failed, etc.).
- `GET /candidates/history/outcomes?symbol=MNQ1!&timeframe=5m&limit=20&horizons=15,30,60`
  — per-candidate outcome. Each result adds an `outcome` field:
  `null` for `no_trade`/`insufficient_data` candidates (nothing to
  score); `{"source": "actual_trade", "status": "closed", "outcome":
  "win"|"loss"|"breakeven", "pnl_usd": ..., "exit_reason": ...}` for a
  candidate that became a real, resolved trade; `{"source":
  "actual_trade", "status": "open"|"pending_fill", "outcome":
  "pending"}` for one still live; or `{"source": "hypothetical",
  "horizons": {...}}` (the old per-horizon `correct`/`incorrect`/
  `flat`/`pending`/`no_data` estimate) for a candidate that never
  became a trade. `horizons` only affects the hypothetical fallback.
- `GET /candidates/history/outcomes/summary?symbol=MNQ1!&timeframe=5m&limit=100`
  — aggregated: real win rate / total & average `pnl_usd` / count
  still open from closed trades, and hypothetical direction-accuracy
  per horizon for never-traded candidates — kept as two separate
  sections, never blended into one number. Replaces manually pulling
  and tallying `/coordinator/history` rows by hand for
  `COORDINATOR_THRESHOLD` tuning.
- `GET /coordinator/history/outcomes` (Sprint 14, unchanged) still
  works exactly as before — it reads the older `coordinator_decisions`
  table, which has no `candidate_id` to link a real trade to, so it's
  hypothetical-only regardless. Kept for anyone already depending on
  its shape; new analysis should use `/candidates/history/outcomes`.

---

## Coordinator

### `GET /coordinator/decide?symbol=MNQ1!&timeframe=5m&persist=true`
Aggregates the latest Analysis/News/Macro/Timing opinions with fixed
weights (Analysis 40% / News 25% / Timing 20% / Macro 15%) into a
score and a decision (`enter_long` / `enter_short` / `no_trade` /
`insufficient_data`). Set `persist=false` to compute without writing
to history (e.g. for a "what-if" check).

As of Tier 2.8, Timing does **not** count toward the directional
score or `MIN_AVAILABLE_WEIGHT` — its direction is always `"neutral"`
by design, so it never carried real directional evidence; the minimum
is now checked against the Analysis/News/Macro (directional) weight
pool only. Timing is still gathered and still appears in
`opinions_used`, but its actual effect is now a separate gate,
visible in the response's `timing_context` field
(`{"confidence", "session_label", "flags"}`, or `null` if no market
bar was available to evaluate it against): a `"market_closed"` flag
(weekend timestamp, or — as of Tier 2.9 — a US market holiday) forces
`score` to `0` outright; a `"low_liquidity"` flag (a weekday bar
outside every ICT kill zone) halves the score. Either shows up in
`conflict_flags` too (`"timing_market_closed"` /
`"timing_low_liquidity_dampened"`) next to the
`analysis_news_conflict*` flags.

As of Tier 2.9, News's `"urgent"` flag (set when a major scheduled
economic event — FOMC, CPI, NFP — is imminent or already breaking)
halves `score` whenever it's present, independent of whether Analysis
and News actually agree — previously this only applied inside an
Analysis/News direction conflict, so two agents that agreed right
before a flagged event got no dampening at all. `conflict_flags`
reports `"analysis_news_conflict_urgent_dampened"` when both a
conflict AND `"urgent"` apply (same single flag/single dampen as
before), or the new `"news_urgent_dampened"` when `"urgent"` applies
on its own.

### `GET /coordinator/history?symbol=MNQ1!&timeframe=5m&limit=20`
Most recent N persisted decisions, newest first.

---

## Replay / versioning (Tier 2.5) — read-only, no secret needed

`COORDINATOR_THRESHOLD`, the four agent weights, and
`MIN_AVAILABLE_WEIGHT` have all changed via env vars over the
project's life; every `CoordinatorDecision` now carries a
`config_version` field (`{"weights": {...}, "threshold": ...,
"min_available_weight": ...}`) recording exactly which config
produced it. Replay re-scores a trade candidate's already-frozen
`opinions_used`/`missing_agents`/`stale_agents` (Tier 2.1) against
either the current live config or an explicit hypothetical override —
entirely offline, no new market data, no LLM calls, and it never
mutates the original candidate or opens a trade.

Since Tier 2.8, replaying a candidate uses the *current* scoring
engine (Timing excluded from `MIN_AVAILABLE_WEIGHT`/the weighted
score, kept as a separate gate — see the Coordinator section above),
regardless of which engine produced the original decision — so
replaying a pre-Tier-2.8 candidate can legitimately show `changed:
true` even under otherwise-identical weights/threshold, if the
original decision benefited from the old Timing-counts-as-evidence
bug. That's the intended use, not a bug in replay: it's exactly how
you'd audit how many historical decisions the fix would have changed.

`weights` (when provided) must be a JSON object string, e.g.
`{"analysis":0.5,"news":0.2,"timing":0.2,"macro":0.1}` — an agent
omitted from it is scored as weight 0, a valid way to ask "what if
this agent didn't count at all." `threshold` / `min_available_weight`
are plain numbers. Any of the three left out falls back to the
CURRENT live value (not the candidate's original config) — asking
"what would this decide under today's threshold but the original
weights" is a valid, distinct question from either extreme.

### `GET /candidates/{candidate_id}/replay?weights=...&threshold=...&min_available_weight=...&include_outcome=false&horizons=15,30,60`
Single-candidate replay. Returns:
```json
{
  "candidate_id": "...",
  "symbol": "MNQ1!", "timeframe": "5m",
  "original_decision_timestamp": "...",
  "original": {"decision": "...", "direction": "...", "score": ..., "threshold": ..., "config_version": {...} },
  "replayed": { "...": "full CoordinatorDecision.to_dict(), including its own config_version" },
  "changed": true,
  "replayed_hypothetical_outcome": { "15": {...}, "30": {...} }
}
```
`original.config_version` is `null` for a candidate created before
this tier ever recorded one — never an empty object pretending to be
a real (if empty) config. `replayed_hypothetical_outcome` is only
present when `include_outcome=true` AND the replayed decision is
directional (nothing to evaluate for `no_trade`/`insufficient_data`)
— it's the Sprint 14 horizon price-direction estimate, never a real
trade; replay never opens one. 404 if `candidate_id` doesn't exist.

### `GET /candidates/history/replay?symbol=MNQ1!&timeframe=5m&limit=50&only_changed=false&weights=...&threshold=...&min_available_weight=...`
Bulk version over recent candidate history (same ordering as
`/candidates/history`, newest first) — a list of the objects above.
`only_changed=true` filters to candidates whose replayed decision
differs from what actually happened, the ones worth reading when
tuning a config change.

### `GET /candidates/history/replay/summary?symbol=MNQ1!&timeframe=5m&limit=100&weights=...&threshold=...&min_available_weight=...`
Aggregated transition counts:
```json
{"total_candidates": 100, "changed": 7, "unchanged": 93,
 "transitions": {"insufficient_data -> enter_long": 5, "no_trade -> enter_short": 2}}
```
The at-a-glance answer to "if `COORDINATOR_THRESHOLD` had been 35
this whole time, how many of the last 100 decisions would have
flipped?" before reading individual replayed candidates.

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
