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
  "auto_execute_enabled": false,
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
`auto_execute_enabled` (Tier 3.9) reflects `AUTO_EXECUTE_ENABLED` —
whether every qualifying candidate is currently being walked through
Risk → Execution → Risk automatically, without a human click. See
`AUTO_EXECUTE_ENABLED` under Environment variables and the webhook
section below.

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

**Tier 3.9 (auto-execution):** when a new bar produces a directional
candidate (`enter_long`/`enter_short`) inside the webhook's background
task, and `AUTO_EXECUTE_ENABLED=true`, that candidate is immediately
walked through the same Risk-gate → Execution → Risk-size pipeline
`GET /agents/risk/evaluate` and `GET /agents/execution/plan` drive
manually — same underlying functions, same guardrails (position
limits, drawdown/daily-loss room, write-once candidate locking), no
human click. Off by default; nothing here changes if the env var isn't
set. This exists to remove selection bias from paper-trade data
collection — see Environment variables below.

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
       produced a valid plan): checks position limits, the daily loss
       limit, and drawdown room only — no stop price needed. Returns
       `pending_execution` (clear to let Execution run), `reject`
       (hard block: `max_positions_reached` / `daily_loss_limit_reached`
       / `drawdown_exhausted`), or `no_action` (Coordinator isn't
       directional). As of Tier 3.3, the open-position count checked
       here is ACCOUNT-WIDE (every symbol/timeframe), not scoped to
       just this one — this pass stays an advisory pre-check, though;
       the real enforcement is the atomic commit in the Size stage
       below.
    2. **Size** (Execution has attached a validated `status="planned"`
       order to this same candidate): sizes the position from
       Execution's actual `entry_price`/`stop_loss` —
       `risk_per_contract = |entry - stop| × $2/pt` — never from ATR.
       Re-checks the daily loss limit and drawdown room too (Execution's
       LLM call happens in between the two stages, during which either
       could change). The trade's budget (`key_data.budget_for_this_trade_usd`)
       is, as of Tier 3.3, `min(RISK_FRACTION_PER_TRADE × remaining
       drawdown room, remaining daily-loss room)` — whichever is
       tighter right now, not drawdown room alone —
       `key_data.budget_binding_constraint` reports which one
       (`"drawdown_room"` | `"daily_loss_room"`), with flag
       `daily_loss_room_binding` set when the daily-loss side is what
       capped (or rejected) the trade. Returns `approve` / `modify` /
       `reject`.
  - Response includes `stage: "gate" | "size"` alongside `decision` so
    callers can tell which pass produced the result.
  - Returns 404 if no trade candidate exists yet, or the latest one is
    older than `CANDIDATE_MAX_AGE_MINUTES` — call `/coordinator/decide`
    first.
  - As of Tier 2.10, `key_data.current_drawdown_used` is the LIVE
    peak-to-trough drawdown computed from real closed paper trades
    (`app/account_risk.py`) — `CURRENT_DRAWDOWN_USED` is now a fallback
    only, same relationship `CURRENT_OPEN_POSITIONS` already has to the
    live open-position count. `key_data` also gains `daily_loss_limit` /
    `daily_loss_used` / `remaining_daily_loss_room`.
  - As of Tier 3.1: once a paper trade has been committed from this
    candidate, its Risk result is **locked** — calling this again
    returns `{"locked": true, "risk_opinion": <the original, unchanged
    opinion>, "trade": <the already-committed trade>}` instead of
    recomputing gate/size math and overwriting the candidate. A
    candidate's `risk_json` can never end up describing a size the
    committed trade doesn't actually have.

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
  - As of Tier 3.1: same lock as Risk above — once a trade exists for
    this candidate, this returns `{"locked": true, "execution_opinion":
    <unchanged>, "trade": <the committed trade>}` WITHOUT calling the
    LLM at all (checked before `plan_execution()` runs, so a re-call
    never spends a paid Execution call it can't use).

### Trades (paper fill/P&L lifecycle, Tier 2.3; fill realism, Tier 3.2) — read-only, no secret needed
A paper ORDER is submitted automatically the moment
`/agents/risk/evaluate`'s size stage returns `approve`/`modify` —
there's no separate "open trade" endpoint to call. As of Tier 3.2,
every order (market or limit) starts `pending_fill` — even a market
order no longer fills instantly; it fills at the NEXT bar's open
(filling into the already-closed anchor bar would be lookahead bias).
Every new webhook bar (regardless of Timing/kill-zone gating — price
doesn't pause outside a kill zone) advances every live trade: fills a
`pending_fill` order (market fills unconditionally at the bar's open;
a limit fills once price actually reaches it), and closes an `open`
trade on a stop or nearest-target hit.
- Market entries and stop-loss exits apply `SLIPPAGE_POINTS` against
  the trader (a stop is effectively a market order once triggered);
  limit and target fills stay exact, no slippage.
- A stop is gap-adjusted: if the bar's `open` already breached it, the
  realistic exit is the `open` (worse for the trader), not the stop
  price itself.
- `pnl_usd = (|exit_price − fill_price| × $2/pt × size) −
  (COMMISSION_PER_CONTRACT × size)` (sign per direction) — a flat
  round-trip commission is subtracted on every close.
- A `pending_fill` order that hasn't filled within
  `ORDER_EXPIRY_MINUTES` of EVENT time (not wall-clock) is
  auto-cancelled (`status="cancelled"`, `exit_reason="expired_unfilled"`)
  instead of resting forever, freeing its position-limit slot.
- A bar that spans both stop and target in one move is treated as the
  stop having been hit first (conservative — OHLC bars don't carry
  true intrabar order). Only the nearest target is checked; a
  multi-target plan fully closes at the first one reached, no partial
  scale-out modeling yet.
- All lifecycle timestamps (`order_submitted_at`/`opened_at`/`closed_at`)
  are the triggering bar's own EVENT time — `created_at` and the two
  new `*_processed` fields are server-processing time, operational
  data only, never used in fill/expiry/P&L logic.
- `GET /trades/open?symbol=MNQ1!&timeframe=5m` — trades still live
  (`pending_fill` or `open`).
- `GET /trades/history?symbol=MNQ1!&timeframe=5m&limit=20` — closed
  trades, newest first, with `exit_price` / `exit_reason`
  (`stop_hit` | `target_hit`) / `pnl_usd`. Cancelled/expired orders are
  not included here (nothing was ever filled) — fetch a specific one
  via `GET /trades/{trade_id}` if needed.
- `GET /trades/{trade_id}` — a single trade by id, any status.

`CURRENT_OPEN_POSITIONS` is now a fallback only — Risk's gate stage
uses the LIVE count from this table by default, so `MAX_OPEN_POSITIONS`
is enforced against reality instead of a hand-updated number. As of
Tier 3.3, that count (and the limit) is ACCOUNT-WIDE — across every
symbol/timeframe, not a separate budget per symbol — and the actual
commit is atomic (`app/storage.open_trade_if_room()`, a single
`BEGIN IMMEDIATE` transaction folding the idempotency check, the
capacity check, and the insert together), closing the earlier
count-then-insert race between two near-simultaneous candidates.

### Account Risk (Tier 2.10) — read-only, no secret needed

### `GET /account/risk`
The account-wide risk snapshot — the same live-computed figures
`/agents/risk/evaluate` uses internally to gate/size trades, exposed
here so the current drawdown/daily-loss status is visible without
triggering a risk evaluation. Deliberately account-wide, not scoped to
a symbol/timeframe — the account's risk budget is a single number
regardless of how many symbols end up trading against it.
```json
{
  "as_of": "2026-08-13T14:00:00Z",
  "account_balance": 50000.0,
  "max_drawdown": 2000.0,
  "current_drawdown_used": 340.0,
  "remaining_drawdown_room": 1660.0,
  "daily_loss_limit": 1000.0,
  "daily_loss_used": 120.0,
  "remaining_daily_loss_room": 880.0,
  "closed_trades_considered": 14
}
```
`current_drawdown_used` is the standard peak-to-trough figure over the
account-wide cumulative realized P&L curve from every closed paper
trade (not just net losses — being $500 up from a $700 peak is $200 of
drawdown, even though the account is still net positive overall).
`daily_loss_used` sums realized P&L for trades closed on the current
NY/CME trading day only (same session-rollover convention as
`app/trading_calendar.py`, Tier 2.9), floored at zero on a winning day.

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
  "pending"}` for one still live; `{"source": "actual_trade", "status":
  "cancelled", "outcome": "cancelled", "exit_reason":
  "expired_unfilled"}` (Tier 3.2) for an order that expired before it
  ever filled — a real order existed, but no position was ever taken,
  so it's neither a resolved trade nor a hypothetical guess; or
  `{"source": "hypothetical", "horizons": {...}}` (the old per-horizon
  `correct`/`incorrect`/`flat`/`pending`/`no_data` estimate) for a
  candidate that never became a trade at all. `horizons` only affects
  the hypothetical fallback.
- `GET /candidates/history/outcomes/summary?symbol=MNQ1!&timeframe=5m&limit=100`
  — aggregated: real win rate / total & average `pnl_usd` / count
  still open / count cancelled-unfilled (Tier 3.2) from closed trades,
  and hypothetical direction-accuracy per horizon for never-traded
  candidates — kept as two separate sections, never blended into one
  number. Replaces manually pulling and tallying `/coordinator/history`
  rows by hand for `COORDINATOR_THRESHOLD` tuning.
- `GET /coordinator/history/outcomes` (Sprint 14, unchanged) still
  works exactly as before — it reads the older `coordinator_decisions`
  table, which has no `candidate_id` to link a real trade to, so it's
  hypothetical-only regardless. Kept for anyone already depending on
  its shape; new analysis should use `/candidates/history/outcomes`.

### `GET /candidates/history/outcomes/by-agent?symbol=MNQ1!&timeframe=5m&limit=100&horizons=15,30,60`
Tier 3.5 (per-agent signal quality), extended in Tier 3.6 (see below).
A `COORDINATOR_THRESHOLD` sweep (`/candidates/history/replay/threshold-sweep`
above) can only ever speak to the Coordinator's *blended* decision — it
can't tell "one agent has real signal but is outweighed by noisier
ones" apart from "no individual agent beats chance either." This
endpoint answers that directly: for every recent candidate, scores
each individual Analysis/News/Macro directional opinion (Timing
excluded — always `"neutral"` by design, never a directional call to
score) against the same hypothetical horizon price-direction estimate
the rest of this section already uses, entirely independent of what
the blended decision ended up being — a bullish Analysis opinion is
scored here even if the Coordinator's overall decision was `no_trade`.
Anchored to each agent's own opinion timestamp when present, falling
back to the candidate's decision timestamp for older data recorded
before per-opinion timestamps existed.
```json
{
  "by_candidate": {
    "analysis": {"15": {"correct": 16, "incorrect": 45, "flat": 0, "pending": 0, "no_data": 0, "accuracy": 0.262}},
    "news":     {"15": {"correct": 11, "incorrect": 35, "flat": 0, "pending": 0, "no_data": 0, "accuracy": 0.239}},
    "macro":    {"15": {"correct": 5,  "incorrect": 29, "flat": 0, "pending": 0, "no_data": 0, "accuracy": 0.147}}
  },
  "by_distinct_opinion": {
    "analysis": {"15": {"correct": 12, "incorrect": 31, "flat": 0, "pending": 0, "no_data": 0, "accuracy": 0.279}},
    "news":     {"15": {"correct": 4,  "incorrect": 9,  "flat": 0, "pending": 0, "no_data": 0, "accuracy": 0.308}},
    "macro":    {"15": {"correct": 2,  "incorrect": 7,  "flat": 0, "pending": 0, "no_data": 0, "accuracy": 0.222}}
  },
  "distinct_opinion_counts": {"analysis": 43, "news": 13, "macro": 9}
}
```
Both agent-keyed sections always carry all three agent keys, even if a
given agent never issued a directional (non-neutral) opinion in the
fetched window — its counts are all zero and `accuracy` is `null`
rather than the key being absent. Entirely offline, no LLM calls, no
trade side effects; same caveat as every hypothetical estimate in this
project: this is a price-direction proxy at a fixed time horizon, not
a real backtest. 400 if `horizons` doesn't parse as comma-separated
integers.

**Tier 3.6 — why two sections.** News and Macro run on their own
schedule (`NEWS_INTERVAL_MINUTES`/`MACRO_INTERVAL_MINUTES`) and stay
eligible for reuse across every webhook bar until
`NEWS_MACRO_MAX_AGE_MINUTES` (default 90) — one LLM call can
legitimately be the frozen `opinions_used` entry for a dozen-plus
consecutive candidates. `by_candidate` tallies one data point per
candidate (matches what the Coordinator literally saw at each
decision), so a single unlucky or lucky call can dominate that
aggregate and make the sample size look far larger than the number of
independent calls actually made. `by_distinct_opinion` scores each
unique `(symbol, timeframe, opinion_timestamp, direction)` tuple
exactly once regardless of reuse — the more honest read on whether an
agent shows real signal. `distinct_opinion_counts` reports how many
truly independent calls back each agent's numbers, so a caller can see
directly how much duplication a `by_candidate` figure was resting on
(Analysis, which only stays fresh for `ANALYSIS_MAX_AGE_MINUTES`,
default 15, is reused far less than News/Macro in practice).

Running `by_distinct_opinion` against production answered the
"does this agent show signal" question concretely for the first
time: News/Macro had only 6 and 4 genuinely distinct opinions
respectively — too few to conclude anything either way — but Analysis
had 78, and its accuracy across all three default horizons (34.7% /
31.9% / 29.9%) is consistently and substantially below the 50%
coin-flip line on a real sample, not a duplication artifact.

### `GET /candidates/history/outcomes/by-agent/detail?symbol=MNQ1!&timeframe=5m&agent=analysis&limit=100&horizons=15,30,60`
Tier 3.7 (per-opinion diagnostic detail). The endpoint above answers
WHETHER an agent shows signal; this answers WHY, once there's a real
finding worth explaining (built after Analysis's ~30%-across-the-board
production result above). Returns one record per DISTINCT opinion for
the given `agent` (required — one of `analysis`/`news`/`macro`, 400
otherwise), same dedup key as `by_distinct_opinion`, sorted oldest
first:
```json
{
  "symbol": "MNQ1!", "timeframe": "5m", "agent": "analysis",
  "distinct_opinion_count": 78,
  "opinions": [
    {
      "symbol": "MNQ1!", "timeframe": "5m",
      "opinion_timestamp": "2026-08-12T14:05:11Z",
      "direction": "bullish", "confidence": 55, "flags": ["choppy"],
      "outcome_by_horizon": {"15": "incorrect", "30": "incorrect", "60": "correct"},
      "reused_by_candidate_count": 1
    }
  ]
}
```
`confidence`/`flags` are each opinion's own self-reported read, read
straight from the already-frozen `opinions_used` entry — no new
lookup. `reused_by_candidate_count` is the per-opinion version of
`distinct_opinion_counts` above — useful for spotting whether a
low-N agent's headline number rests on one dominant call.
Deliberately excludes each opinion's free-text `reasoning` and
`key_data` to stay compact and reliable for large windows (same
WebFetch-large-JSON constraint that shaped every other endpoint in
this project meant to be queried against production through an
LLM-mediated fetch) — pull `/agents/{agent}/history` for full
reasoning on a specific opinion. Entirely offline, no LLM calls, no
trade side effects.

**Tier 3.8 addition — `by_day=true`.** One agent's opinions across a
single trading day are correlated, not independent draws — a bad
regime can drag every opinion issued during it the same direction.
Add `by_day=true` to group the same records by calendar date (UTC),
returned as a sibling `"by_day"` key alongside the existing flat
`"opinions"` list (off by default, so the Tier 3.7 response shape is
unchanged unless asked for):
```json
"by_day": {
  "2026-08-12": {"15": {"correct": 4, "incorrect": 15, "flat": 0, "pending": 0, "no_data": 0, "n": 19, "accuracy": 0.211}},
  "2026-08-13": {"15": {"correct": 21, "incorrect": 32, "flat": 0, "pending": 0, "no_data": 0, "n": 53, "accuracy": 0.396}}
}
```
Deliberately the cheap first version, not real block-bootstrap/
clustered confidence intervals — good enough to make day-level
clustering visible now; escalate once there's more data across more
distinct days.

### `GET /candidates/history/outcomes/baseline-comparison?symbol=MNQ1!&timeframe=5m&limit=100&horizons=15,30,60`
Tier 3.8 (base rate + trivial-baseline comparison). 50% coin-flip is
not automatically the right null baseline for judging an agent's
accuracy — if the market moved mostly one direction during the
measurement window, any fixed directional bias (like Analysis's own
90%-bullish tendency, found via the endpoint above) looks artificially
good or bad purely as a function of which way the window happened to
move, independent of real skill. This computes the market's own base
rate alongside a couple of trivial, mostly-LLM-independent predictors,
on the exact same candidate population and horizon machinery every
other accuracy figure in this project uses:
```json
{
  "symbol": "MNQ1!", "timeframe": "5m",
  "candidates_considered": 82,
  "always_bullish": {"15": {"correct": 34, "incorrect": 48, "flat": 0, "pending": 0, "no_data": 0, "accuracy": 0.415}},
  "always_bearish": {"15": {"correct": 48, "incorrect": 34, "flat": 0, "pending": 0, "no_data": 0, "accuracy": 0.585}},
  "inverse_of_analysis": {"15": {"correct": 49, "incorrect": 26, "flat": 0, "pending": 2, "no_data": 1, "accuracy": 0.653}},
  "vwap_direction": {"15": {"correct": 40, "incorrect": 38, "flat": 0, "pending": 0, "no_data": 0, "accuracy": 0.513}},
  "sample_sizes": {"inverse_of_analysis": 78, "vwap_direction": 71}
}
```
`always_bullish`/`always_bearish` are computed for every candidate
with a resolvable anchor timestamp (`candidates_considered`) — their
`accuracy` at a given horizon literally IS this window's up-rate /
down-rate; that number, not an assumed 50%, is the real baseline an
agent's accuracy should be judged against. `inverse_of_analysis` flips
Analysis's own directional call (bullish↔bearish; neutral/missing
skipped) as a pure diagnostic for whether a systematically wrong agent
might have a reversible anti-signal — **this project has not acted on
that inversion and has no plan to without much more out-of-sample
evidence; it's here to inform, not to trigger a config change.**
`vwap_direction` predicts from the triggering bar's own
`distance_from_vwap_points` (bullish if price sits above VWAP, bearish
if below, skipped if exactly 0 or missing) — a simple technical
baseline with no LLM call behind it at all. `sample_sizes` reports how
many candidates each of the latter two baselines actually covered,
since unlike `always_bullish`/`always_bearish` they only apply where
the relevant data exists. Entirely offline, no LLM calls, no trade
side effects. 400 if `horizons` doesn't parse as comma-separated
integers.

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

As of Tier 3.1 (causal integrity), the webhook-triggered `persist=true`
path anchors the whole candidate to the exact bar that triggered it —
the market_state event's own `event_id` — instead of independently
re-querying "the latest bar" and "the latest Analysis opinion" at each
step. This closes a real gap: a second webhook landing while an
earlier one's background Analysis run was still in flight could
previously make the frozen candidate's bar, its Timing context, and
its Analysis opinion each describe a different moment. A candidate
object (as returned by `/candidates/{id}` and friends) now also
carries `risk_history`/`execution_history` — append-only lists of
every Risk/Execution result ever attached, so e.g. the original gate
opinion (`stage: "gate"`) is still visible even after the size opinion
(`stage: "size"`) becomes the current `risk` value. `risk`/`execution`
on the candidate always remain "whatever the most recent stage
produced," unchanged in shape from before this tier.

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

### `GET /candidates/history/replay/threshold-sweep?symbol=MNQ1!&timeframe=5m&thresholds=15,20,25,30,35,40&limit=100&horizons=15,30,60&weights=...&min_available_weight=...`
Tier 3.4 (`COORDINATOR_THRESHOLD` tuning) — the actual tuning tool:
sweeps `thresholds` (required, comma-separated) across every recent
candidate's frozen opinions (offline re-score, no LLM calls, same
machinery as the replay endpoints above), and for each threshold
aggregates directional decision volume plus hypothetical horizon
accuracy into a compact per-threshold summary:
```json
{
  "symbol": "MNQ1!", "timeframe": "5m", "candidates_considered": 54,
  "weights_held_fixed": {"analysis": 0.4, "news": 0.25, "timing": 0.2, "macro": 0.15},
  "min_available_weight_held_fixed": 0.6,
  "sweep": {
    "15": {"directional_candidates": 40, "by_horizon_minutes": {"15": {"correct": 12, "incorrect": 28, "flat": 0, "pending": 0, "no_data": 0, "accuracy": 0.3}}},
    "35": {"directional_candidates": 9,  "by_horizon_minutes": {"15": {"correct": 6,  "incorrect": 3,  "flat": 0, "pending": 0, "no_data": 0, "accuracy": 0.667}}}
  }
}
```
`weights`/`min_available_weight` are held FIXED across the whole
sweep — only `threshold` varies, so any accuracy shift is
attributable to threshold alone. Same caveat as `include_outcome`
above: this is the hypothetical horizon price-direction estimate, not
a real backtest — a replayed decision under a hypothetical threshold
was never actually filled/sized/executed, so there's no real P&L to
attribute to it. 400 if `thresholds` doesn't parse as comma-separated
numbers.

### `GET /candidates/history/backtest-lite?symbol=MNQ1!&timeframe=5m&limit=200&sources=analysis,coordinator,always_bullish,always_bearish,vwap,inverse_analysis&atr_stop_mult=1.5&atr_target_mult=2.5&expiry_bars=24&non_overlapping=true`
Tier 3.10 (ATR-barrier benchmark) — every accuracy number through Tier
3.9 uses the "price higher/lower N minutes later" proxy, never an
actual entry/stop/target trade simulation. This endpoint runs the
SAME fill/stop/target/slippage/commission mechanics the live
paper-trade engine uses for real trades
(`app/paper_trades.process_new_bar`), offline, against bars already in
storage, for a hypothetical trade that was never taken — nothing
written to any trade table. Entry is a market fill at the next bar's
open after the candidate's own anchor bar; stop/target are the anchor
bar's own already-stored ATR times `atr_stop_mult`/`atr_target_mult`
(deterministic, no LLM, no lookahead) — NOT Execution's proposed
levels, since this benchmarks the directional SIGNAL, not what
Execution would have picked.

`sources` runs several direction signals through the identical barrier
mechanics side by side: `analysis` (Analysis's own opinion), `coordinator`
(the blended decision), `always_bullish` / `always_bearish` / `vwap`
(trivial baselines — `vwap` is bullish when the anchor bar's own
`distance_from_vwap_points` is positive), and `inverse_analysis`
(Analysis's calls flipped — diagnostic only, never acted on, same
framing as the `inverse_of_analysis` baseline in
`baseline-comparison` above). Omit `sources` for all six.

**Tier 3.12 correction:** this is a POLICY comparison, not a paired
one — each source independently applies `non_overlapping` against its
OWN resolved directions, so different sources can end up trading
different candidate subsets (a candidate `analysis` stays flat on may
still open a trade under `coordinator`'s direction). An earlier
version of this document described this as a "same candidate
population" comparison; that was inaccurate. For a true paired
comparison — one shared entry, one shared non-overlap schedule, only
candidates every requested source can resolve — see
`GET /candidates/history/backtest-lite/paired` below.

`non_overlapping=true` (default) skips a candidate whose anchor falls
before the previous simulated trade (for that source) resolved —
avoids double-counting overlapping candidates on a fast timeframe as
independent evidence, mirroring the real `MAX_OPEN_POSITIONS=1`
constraint.

```json
{
  "symbol": "MNQ1!", "timeframe": "5m",
  "config": {"atr_stop_mult": 1.5, "atr_target_mult": 2.5, "expiry_bars": 24, "non_overlapping": true, "candidates_considered": 100},
  "by_source": {
    "analysis": {
      "trades_taken": 61, "skipped_no_direction": 22, "skipped_no_atr": 0,
      "skipped_overlapping": 17, "skipped_no_forward_data": 0,
      "wins": 27, "losses": 33, "breakeven": 1, "expired": 9,
      "total_pnl_usd": -412.0, "gross_profit_usd": 890.0, "gross_loss_usd": 1302.0,
      "win_rate": 0.45, "profit_factor": 0.6834, "avg_pnl_usd": -6.75,
      "trades": []
    },
    "always_bullish": { "...": "same shape" },
    "always_bearish": { "...": "same shape" },
    "vwap": { "...": "same shape" },
    "inverse_analysis": { "...": "same shape" },
    "coordinator": { "...": "same shape" }
  }
}
```
`trades` is always `[]` at this endpoint's compact default — per-trade
detail exists in `app/backtest.run_barrier_backtest(..., include_trades=True)`
for direct/programmatic use, but is deliberately excluded from the
HTTP response to stay compact and WebFetch-reliable at scale, same
constraint that's shaped every other endpoint in this project queried
against production through this session. `profit_factor` is `null`
when there are no losses to divide by (undefined, not a misleading
infinity), and `win_rate`/`avg_pnl_usd` are `null` when nothing
resolved to a decided win/loss yet. Entirely offline: no LLM calls,
no new data collection. 400 if `sources` contains an unrecognized
value. `COORDINATOR_THRESHOLD` and the Coordinator's own scoring are
untouched — this is read-only analysis, same as every diagnostic tier
before it.

### `GET /candidates/history/backtest-lite/champion-challenger?symbol=MNQ1!&timeframe=5m&limit=300&champion=coordinator&challengers=analysis,inverse_analysis,always_bullish,always_bearish,vwap&holdout_fraction=0.3&atr_stop_mult=1.5&atr_target_mult=2.5&expiry_bars=24&non_overlapping=true`
Tier 3.11 (champion/challenger, out-of-sample). The endpoint above's
first real production run found `inverse_analysis` as the only source
with `profit_factor > 1` — exactly the kind of finding the external
review warned about, since it was found on the same historical sample
any change would be justified against. Built on
`app/backtest.compute_champion_challenger_report()`: holds out the
most RECENT `holdout_fraction` (default `0.3`) of candidate history as
a validation window (never a random split — regimes are
time-correlated, a random split would leak the future into
calibration), then runs `champion` (the currently-live decision
source, default `coordinator`) plus every requested `challenger`
through the identical backtest-lite barrier mechanics on BOTH the
calibration window and the held-out validation window, separately.

```json
{
  "symbol": "MNQ1!", "timeframe": "5m",
  "config": {
    "atr_stop_mult": 1.5, "atr_target_mult": 2.5, "expiry_bars": 24, "non_overlapping": true,
    "holdout_fraction": 0.3, "candidates_considered": 156, "calibration_candidates": 104, "validation_candidates": 47,
    "purged_at_boundary": 5
  },
  "champion": "coordinator",
  "challengers": ["analysis", "inverse_analysis", "always_bullish", "always_bearish", "vwap"],
  "by_source": {
    "coordinator": {"calibration": { "...": "same shape as backtest-lite's per-source summary" }, "validation": { "...": "same shape" }},
    "inverse_analysis": {"calibration": { "...": "..." }, "validation": { "...": "..." }},
    "...": "one calibration/validation pair per source"
  },
  "base_rate": {
    "calibration": { "...": "same shape as GET /candidates/history/baseline-comparison" },
    "validation": { "...": "same shape" }
  }
}
```
Reads as: does a challenger's apparent edge on calibration still hold
up on data it was never fitted to? A challenger that looks good on
calibration but falls apart on validation is materially weaker
evidence than one that holds up on both — reported side by side on
purpose, not collapsed into a single number or an automatic pass/fail
(a rigid threshold would be its own kind of overfitting at this sample
size). Purely a report: never picks a winner, never flips anything.
Same standing rule as every diagnostic tier before it — any real
trading-logic change needs the user's explicit direction.
`COORDINATOR_THRESHOLD` and Coordinator scoring untouched. 400 if
`champion`/`challengers` contains an unrecognized source; 422 (FastAPI's
own query validation) if `holdout_fraction` is outside `(0, 1)`.

**Tier 3.12 additions:** `config.calibration_candidates` now reflects
an EMBARGOED calibration set — any calibration candidate whose forward
barrier walk (`expiry_bars` bars ahead) would read price bars from
inside the validation window is purged, so calibration and validation
no longer share any price data; `config.purged_at_boundary` reports
how many candidates that removed (this doesn't affect
`validation_candidates` or validation's own numbers — a validation
trade only ever looks forward from its own, later anchor). The new
`base_rate` section runs Tier 3.8's plain "price higher/lower N
minutes later" baseline comparison on both windows, as a cheap sanity
check for whether the two windows look like meaningfully different
market regimes.

### `GET /candidates/history/backtest-lite/paired?symbol=MNQ1!&timeframe=5m&limit=300&sources=analysis,coordinator,inverse_analysis&atr_stop_mult=1.5&atr_target_mult=2.5&expiry_bars=24`
Tier 3.12 (paired signal comparison). The plain `backtest-lite`
endpoint above compares sources as independent POLICIES — each one
applies its own non-overlap schedule against its own resolved
directions, so two sources can end up trading different candidate
subsets. That's a fair "what would running on this signal alone have
looked like" comparison, but not a clean "does signal A beat signal B"
one, since a difference in results can come from which candidates were
traded rather than what direction was called on the same candidate.
This endpoint, built on `app/backtest.run_paired_barrier_backtest()`,
fixes that: it keeps only candidates where EVERY requested source can
resolve a direction (the full intersection), then runs every source
through the identical entry price, ATR-derived stop/target geometry,
and forward bar walk for each accepted candidate — one shared,
direction-independent non-overlap schedule decides which candidates
are accepted at all, so no source's own resolved direction can
influence which candidates make the comparison. `sources` is required
(comma-separated, at least one recognized source).

```json
{
  "symbol": "MNQ1!", "timeframe": "5m",
  "config": {
    "atr_stop_mult": 1.5, "atr_target_mult": 2.5, "expiry_bars": 24,
    "candidates_considered": 156, "eligible_candidates": 61, "accepted_candidates": 38
  },
  "sources": ["analysis", "coordinator", "inverse_analysis"],
  "by_source": {
    "analysis": { "...": "same shape as backtest-lite's per-source summary" },
    "coordinator": { "...": "same shape" },
    "inverse_analysis": { "...": "same shape" }
  }
}
```
`eligible_candidates` is the intersection — only candidates every
requested source could resolve a direction for; `accepted_candidates`
is what's left after the shared non-overlap schedule, i.e. the actual
number of paired trades each source ran. Entirely offline: no LLM
calls, no new data collection, nothing written to any trade table. 400
if `sources` is empty or contains an unrecognized value.
`COORDINATOR_THRESHOLD` and the Coordinator's own scoring are
untouched — read-only analysis, same as every diagnostic tier before
it.

---

## Auto-execution (Tier 3.9)

Every prior tier's paper trades were opened by a human manually
calling `GET /agents/risk/evaluate` then `GET /agents/execution/plan`
then `GET /agents/risk/evaluate` again for whichever candidates they
chose to act on. An external review (2026-08-14) flagged that this
selection is itself a source of bias: which candidates get executed
ends up conflating the system's own signal quality with the
operator's judgment, availability, and mood, which makes the
resulting "real trade" data unreliable for judging the system on its
own. The fix is to make execution mechanical and non-selective.

Set `AUTO_EXECUTE_ENABLED=true` and every directional candidate
(`enter_long`/`enter_short` — i.e. one that already cleared
`COORDINATOR_THRESHOLD`) is automatically walked through Risk-gate →
Execution → Risk-size inside the webhook's background task, right
after the candidate itself is created — no manual step. This calls
the exact same functions the manual endpoints call
(`evaluate_risk_gate`, `plan_execution`, `size_position`,
`open_trade_from_candidate`), so every existing guardrail (position
limits, drawdown/daily-loss room, write-once candidate locking, the
atomic account-wide open-position check) applies identically. A
candidate the gate rejects, or one Execution declines/fails to
produce a valid geometry for, is simply left at that stage — same
outcome as if a human had stopped there manually.

Off by default. Setting it has a real, ongoing cost: Execution's LLM
call now fires on every qualifying candidate instead of only the ones
a human chose to click through, and every candidate that clears the
gate opens a real (paper) trade. The user was offered a conservative
(stay manual), a sampled, and a fully-automatic policy, and chose the
fully-automatic one — execute every qualifying candidate — to get the
most complete, least-biased dataset as fast as possible, accepting
the added cost. `COORDINATOR_THRESHOLD` and the Coordinator's scoring
were NOT touched by this tier.

---

## Environment variables

| Variable | Required | Default | Notes |
|---|---|---|---|
| `WEBHOOK_SECRET` | yes | — | must match the Pine Script's secret input |
| `ANTHROPIC_API_KEY` | yes | — | for Analysis/News/Macro |
| `DB_PATH` | no | `./data/market_state.db` | point at a Railway Volume mount for persistence |
| `ENABLE_SCHEDULER` | no | `false` | set `true` in production to auto-run News/Macro |
| `AUTO_EXECUTE_ENABLED` | no | `false` | Tier 3.9: set `true` to auto-run Risk → Execution → Risk on every qualifying candidate, no manual click — see "Auto-execution (Tier 3.9)" above |
| `NEWS_SYMBOL` | no | `MNQ1!` | |
| `NEWS_INTERVAL_MINUTES` | no | `20` | |
| `MACRO_SYMBOL` | no | (= `NEWS_SYMBOL`) | |
| `MACRO_INTERVAL_MINUTES` | no | `20` | |
| `COORDINATOR_THRESHOLD` | no | `25` | placeholder — needs tuning against real history |
| `ACCOUNT_BALANCE` | no | `50000` | static/manual, Sprint 7 |
| `MAX_DRAWDOWN` | no | `2000` | |
| `CURRENT_DRAWDOWN_USED` | no | `0` | Tier 2.10: fallback only — normally superseded by the live peak-to-trough figure computed from real closed paper trades |
| `MAX_OPEN_POSITIONS` | no | `1` | enforced against the live open-paper-trade count (Tier 2.3); ACCOUNT-WIDE across every symbol/timeframe, atomically, as of Tier 3.3 |
| `CURRENT_OPEN_POSITIONS` | no | `0` | Tier 2.3: fallback only — normally superseded by the live, account-wide paper-trade count |
| `BASE_POSITION_SIZE` | no | `1` | contracts |
| `RISK_FRACTION_PER_TRADE` | no | `0.5` | fraction of remaining drawdown room risked per trade — as of Tier 3.3, the trade's actual budget is `min(this, remaining daily-loss room)` |
| `DAILY_LOSS_LIMIT` | no | `1000` | Tier 2.10: new time-boxed circuit breaker, live-computed from trades closed on the current NY/CME trading day — no manual updating needed |
| `ORDER_EXPIRY_MINUTES` | no | `60` | Tier 3.2: cancels a `pending_fill` order after this many EVENT-time minutes unfilled |
| `SLIPPAGE_POINTS` | no | `0.25` | Tier 3.2: applied against the trader on market entries and stop-loss exits only |
| `COMMISSION_PER_CONTRACT` | no | `2.0` | Tier 3.2: flat round-trip commission, subtracted from `pnl_usd` on every closed trade |
| `BACKTEST_ATR_STOP_MULT` | no | `1.5` | Tier 3.10: ATR-barrier backtest-lite's default stop distance (multiple of the anchor bar's own ATR) — only affects the offline benchmark, never real trades or Execution's actual proposed geometry |
| `BACKTEST_ATR_TARGET_MULT` | no | `2.5` | Tier 3.10: same, target distance |
| `BACKTEST_EXPIRY_BARS` | no | `24` | Tier 3.10: how many forward bars a hypothetical barrier trade is walked before being marked "expired" (mark-to-last-seen-close, `SLIPPAGE_POINTS` applied against the trader as of Tier 3.12 — this exit is itself a market order, previously priced with no slippage, inconsistent with every other exit type) |
| `BACKTEST_HOLDOUT_FRACTION` | no | `0.3` | Tier 3.11: fraction of candidate history (chronologically most recent) held out as the champion/challenger validation window |

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
                                          -- a paper ORDER is submitted automatically here on approve/modify
                                          (Tier 3.2: starts pending_fill even for a market order)
9. GET  /trades/open?...              -> confirm the order exists (pending_fill)
   (later, on subsequent bars)
10. GET /trades/open?...              -> once a real bar fills it, status flips to open
11. GET /trades/history?...           -> once a stop/target is hit, the trade shows up here closed with pnl_usd
```

In production with `ENABLE_SCHEDULER=true`, steps 1–4 happen on
their own (webhook + background scheduler) — only 5–8 need manual
triggering (or a future automation once the pipeline is trusted).
Note step 6 is called twice across the lifecycle (steps 6 and 8) —
same endpoint, different stage depending on whether Execution has
run yet. Steps 9–10 need no manual trigger at all — trade opening and
fill/close monitoring both happen automatically as side effects of
step 8 and every subsequent webhook bar, respectively.

With `AUTO_EXECUTE_ENABLED=true` (Tier 3.9) as well, steps 5–8 also
happen on their own, immediately after step 2 — nothing left to
trigger manually for a candidate that clears the gate.
