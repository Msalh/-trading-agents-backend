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
- `GET /trades/history?symbol=MNQ1!&timeframe=5m&limit=20&exclude_provenance=manual_dashboard`
  — closed trades, newest first, with `exit_price` / `exit_reason`
  (`stop_hit` | `target_hit`) / `pnl_usd`. Cancelled/expired orders are
  not included here (nothing was ever filled) — fetch a specific one
  via `GET /trades/{trade_id}` if needed. `exclude_provenance` (Tier
  3.22) is an opt-in, comma-separated filter on the new `provenance`
  field below — omit it for the full, unfiltered history (default,
  unchanged behavior). Filtering happens AFTER `limit` is applied to
  the underlying query, so a caller who needs an exact post-filter
  count should pass a generously large `limit`.
- `GET /trades/{trade_id}` — a single trade by id, any status.

**Tier 3.22 (fifth external review — trade provenance).** A manual
dashboard pipeline test on 2026-08-18 (via `/agents/risk/evaluate`'s
"Run" buttons) produced a real closed paper trade that was
indistinguishable, in every report, from genuine autonomous execution
— flagged by the review as data contamination needing an immediate
fix. Every trade object now carries a `provenance` field:
`"auto_policy"` (opened by the `AUTO_EXECUTE_ENABLED`-gated background
task) or `"manual_dashboard"` (opened via the manual
`/agents/risk/evaluate` endpoint). `app/paper_trades.
open_trade_from_candidate()` takes `provenance` as a REQUIRED
argument — no default, so a future call site can't silently omit it.
Rows that predate this migration were backfilled to
`"manual_dashboard"`: `AUTO_EXECUTE_ENABLED` has been `false` for this
project's entire history to date (repeatedly reconfirmed live), so no
pre-migration row could possibly have come from the auto-policy path.
This is a code-verifiable split only — it cannot distinguish a
deliberate manual pipeline TEST from a deliberate manual DISCRETIONARY
trade decision, since that's a human-intent question the backend has
no way to observe; both land under `"manual_dashboard"`.

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
  "closed_trades_considered": 14,
  "closed_trades_by_provenance": { "auto_policy": 13, "manual_dashboard": 1 }
}
```
`current_drawdown_used` is the standard peak-to-trough figure over the
account-wide cumulative realized P&L curve from every closed paper
trade (not just net losses — being $500 up from a $700 peak is $200 of
drawdown, even though the account is still net positive overall).
`daily_loss_used` sums realized P&L for trades closed on the current
NY/CME trading day only (same session-rollover convention as
`app/trading_calendar.py`, Tier 2.9), floored at zero on a winning day.

`closed_trades_by_provenance` (Tier 3.22) is a visibility-only
breakdown of `closed_trades_considered` by the new `provenance` field
(see the Trades section above). Deliberately NOT used to filter
`current_drawdown_used`/`daily_loss_used` — both still count every
closed trade regardless of provenance. Whether a manual dashboard
trade should consume real paper-account risk-budget capacity is an
open design question the fifth review raised but explicitly left to
the user's own judgment (same status as the still-open
Analysis-load-bearing design question) — not decided or silently
changed here.

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

## Replay / versioning (Tier 2.5, extended Tier 3.24) — read-only, no secret needed

`COORDINATOR_THRESHOLD`, the four agent weights, `MIN_AVAILABLE_WEIGHT`,
and (Tier 3.24) `ANALYSIS_REQUIRED` have all changed or been added via
env vars over the project's life; every `CoordinatorDecision` now
carries a `config_version` field (`{"weights": {...}, "threshold": ...,
"min_available_weight": ..., "analysis_required": ...}`) recording
exactly which config produced it. Replay re-scores a trade candidate's
already-frozen `opinions_used`/`missing_agents`/`stale_agents`
(Tier 2.1) against either the current live config or an explicit
hypothetical override — entirely offline, no new market data, no LLM
calls, and it never mutates the original candidate or opens a trade.

**Tier 3.24 (`analysis_required`, project-owner design decision, not
data-driven).** Tier 3.21 proved algebraically that under the live
weights, Analysis being unavailable *always* fails the
`MIN_AVAILABLE_WEIGHT` quorum check on its own — Analysis was already
"load-bearing" as an emergent side-effect of the weights/threshold
math, not a named rule. The fifth external review flagged this as an
open question data alone couldn't resolve: is that intentional, or an
accident of tunable weights? The project owner's answer: make it
explicit. `ANALYSIS_REQUIRED` (default `true`) is a new gate in
`_score_opinions()`, checked *before* the quorum math and independent
of it — "no directional decision without a current (non-missing,
non-stale) Analysis opinion." Scoped narrowly, by the owner's explicit
choice: it checks Analysis's mere presence, not its direction — a
present-but-neutral Analysis opinion still passes, exactly as it did
before this tier. Changes no decision computed today, live or
replayed; it hardens an already-true guarantee against being silently
broken by a future weights/`min_available_weight` retune that might
otherwise let News+Macro clear quorum without Analysis ever being
consulted.

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
are plain numbers, `analysis_required` (Tier 3.24) is a plain boolean.
Any of the four left out falls back to the CURRENT live value (not the
candidate's original config) — asking "what would this decide under
today's threshold but the original weights" is a valid, distinct
question from either extreme.

### `GET /candidates/{candidate_id}/replay?weights=...&threshold=...&min_available_weight=...&analysis_required=...&include_outcome=false&horizons=15,30,60`
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

### `GET /candidates/history/replay?symbol=MNQ1!&timeframe=5m&limit=50&only_changed=false&weights=...&threshold=...&min_available_weight=...&analysis_required=...`
Bulk version over recent candidate history (same ordering as
`/candidates/history`, newest first) — a list of the objects above.
`only_changed=true` filters to candidates whose replayed decision
differs from what actually happened, the ones worth reading when
tuning a config change.

### `GET /candidates/history/replay/summary?symbol=MNQ1!&timeframe=5m&limit=100&weights=...&threshold=...&min_available_weight=...&analysis_required=...`
Aggregated transition counts:
```json
{"total_candidates": 100, "changed": 7, "unchanged": 93,
 "transitions": {"insufficient_data -> enter_long": 5, "no_trade -> enter_short": 2}}
```
The at-a-glance answer to "if `COORDINATOR_THRESHOLD` had been 35
this whole time, how many of the last 100 decisions would have
flipped?" before reading individual replayed candidates.

### `GET /candidates/history/replay/threshold-sweep?symbol=MNQ1!&timeframe=5m&thresholds=15,20,25,30,35,40&limit=100&horizons=15,30,60&weights=...&min_available_weight=...&analysis_required=...`
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
  "analysis_required_held_fixed": true,
  "sweep": {
    "15": {"directional_candidates": 40, "by_horizon_minutes": {"15": {"correct": 12, "incorrect": 28, "flat": 0, "pending": 0, "no_data": 0, "accuracy": 0.3}}},
    "35": {"directional_candidates": 9,  "by_horizon_minutes": {"15": {"correct": 6,  "incorrect": 3,  "flat": 0, "pending": 0, "no_data": 0, "accuracy": 0.667}}}
  }
}
```
`weights`/`min_available_weight`/`analysis_required` are held FIXED
across the whole sweep — only `threshold` varies, so any accuracy
shift is attributable to threshold alone. Same caveat as `include_outcome`
above: this is the hypothetical horizon price-direction estimate, not
a real backtest — a replayed decision under a hypothetical threshold
was never actually filled/sized/executed, so there's no real P&L to
attribute to it. 400 if `thresholds` doesn't parse as comma-separated
numbers.

### `GET /candidates/history/backtest-lite?symbol=MNQ1!&timeframe=5m&limit=200&sources=analysis,coordinator,always_bullish,always_bearish,vwap,inverse_analysis,analysis_risk_filtered&atr_stop_mult=1.5&atr_target_mult=2.5&expiry_bars=24&non_overlapping=true`
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
`distance_from_vwap_points` is positive), `inverse_analysis`
(Analysis's calls flipped — diagnostic only, never acted on, same
framing as the `inverse_of_analysis` baseline in
`baseline-comparison` above), and `analysis_risk_filtered` (Tier 3.30 —
same direction call as `analysis`, but the candidate is skipped
entirely if News's opinion carries the `"urgent"` flag or Macro's
opinion carries the `"risk_off"` flag; an agent that never ran for that
candidate can't veto it. Sixth external review's "Analysis alone
decides direction, News/Macro as risk filters only" shadow policy —
News/Macro can only remove a trade Analysis wanted to take, never
supply or shift its direction. See `app/backtest.py`'s module docstring
for exactly why those two flags, out of News's and Macro's full
vocabularies, were the ones confirmed for the veto). Omit `sources` for
all seven.

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
  "day_session": {
    "candidates_considered": 100, "distinct_trading_days": 14,
    "candidates_per_day": {"min": 2, "median": 6.5, "max": 19},
    "by_session_name": {"RTH": 71, "OVERNIGHT": 29},
    "by_timing_session_label": {"new_york": 38, "london": 22, "new_york_pm": 11, "london_ny_overlap": 9, "outside_sessions": 20},
    "unknown_trading_date_count": 0
  },
  "by_source": {
    "analysis": {
      "trades_taken": 61, "skipped_no_direction": 22, "skipped_no_atr": 0,
      "skipped_overlapping": 17, "skipped_no_forward_data": 0,
      "wins": 27, "losses": 33, "breakeven": 1, "expired": 9,
      "total_pnl_usd": -412.0, "gross_profit_usd": 890.0, "gross_loss_usd": 1302.0,
      "win_rate": 0.45, "win_rate_ci95_low": 0.3387, "win_rate_ci95_high": 0.5734,
      "profit_factor": 0.6834, "avg_pnl_usd": -6.75, "median_pnl_usd": -8.5, "max_drawdown_usd": 612.3,
      "trades": []
    },
    "always_bullish": { "...": "same shape" },
    "always_bearish": { "...": "same shape" },
    "vwap": { "...": "same shape" },
    "inverse_analysis": { "...": "same shape" },
    "coordinator": { "...": "same shape" },
    "analysis_risk_filtered": { "...": "same shape" }
  }
}
```
**Tier 3.18 addition:** `day_session` — see the dedicated section below
for the full field explanation — reports how many genuinely
independent trading days/sessions `candidates_considered` actually
spans, computed once over the input candidate set (not per-source,
since it's a property of the sample, not of any one direction signal).
This is the "day/session counts as a primary metric, not buried"
addition the third external review asked for; the same object is now
in every backtest-lite/paired/grid/champion-challenger response.

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

**Tier 3.13 additions:** `win_rate_ci95_low`/`win_rate_ci95_high` are a
95% Wilson score confidence interval on `wins / (wins + losses)` — at
the trade counts this project actually produces (single digits to low
double digits per source), the bare `win_rate` alone is not enough to
tell a real edge from noise; treat two sources' `win_rate` figures as
meaningfully different only if their intervals don't overlap much, not
just because the point estimates differ. `median_pnl_usd` is the
median (not mean) trade P&L, a check against one large win or loss
dominating `avg_pnl_usd`. `max_drawdown_usd` is the deepest
peak-to-trough dip in the running equity curve, in the order trades
were actually taken — "how bad did it get along the way," not just the
ending total. All three are `null`/absent-equivalent under the same
"nothing decided yet" conditions as `win_rate`/`avg_pnl_usd`.

### `GET /candidates/history/backtest-lite/champion-challenger?symbol=MNQ1!&timeframe=5m&limit=300&champion=coordinator&challengers=analysis,inverse_analysis,always_bullish,always_bearish,vwap,analysis_risk_filtered&holdout_fraction=0.3&atr_stop_mult=1.5&atr_target_mult=2.5&expiry_bars=24&non_overlapping=true`
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
  "challengers": ["analysis", "inverse_analysis", "always_bullish", "always_bearish", "vwap", "analysis_risk_filtered"],
  "by_source": {
    "coordinator": {"calibration": { "...": "same shape as backtest-lite's per-source summary" }, "validation": { "...": "same shape" }},
    "inverse_analysis": {"calibration": { "...": "..." }, "validation": { "...": "..." }},
    "...": "one calibration/validation pair per source"
  },
  "base_rate": {
    "calibration": { "...": "same shape as GET /candidates/history/baseline-comparison" },
    "validation": { "...": "same shape" }
  },
  "day_session": {
    "calibration": { "...": "same shape as the day/session section below" },
    "validation": { "...": "same shape" }
  }
}
```
**Tier 3.18 addition:** `day_session` is reported PER WINDOW here
(unlike the other backtest-lite endpoints, which report it once) —
out-of-sample validity depends directly on how many independent
trading days each window actually spans, so a validation window with
`distinct_trading_days: 2` is much weaker out-of-sample evidence than
one with `distinct_trading_days: 15`, even at the same
`validation_candidates` count.
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
  "day_session": { "...": "same shape as the day/session section below, computed over the full input candidates" },
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

### `GET /candidates/history/backtest-lite/sensitivity-grid?symbol=MNQ1!&timeframe=5m&limit=300&sources=analysis,coordinator,inverse_analysis`
Tier 3.14 (parameter sensitivity grid). Every result reported through
Tier 3.13 used one specific geometry (1.5x ATR stop, 2.5x ATR target,
24-bar expiry) — a source that only looks good under that one choice
could just be an artifact of the choice, not a real edge. This
endpoint, built on `app/backtest.run_sensitivity_grid()`, runs the
Tier 3.12 paired comparison across a small, PRE-REGISTERED grid:
default stops `{1.0, 1.5, 2.0}`x ATR, targets `{1.5, 2.0, 2.5}`x ATR,
expiry `{6, 12, 24}` bars — 27 combinations. The grid itself is fixed
at deploy time via `BACKTEST_GRID_STOP_MULTS` / `BACKTEST_GRID_TARGET_
MULTS` / `BACKTEST_GRID_EXPIRY_BARS` env vars, deliberately **not** a
query parameter on this endpoint — letting a caller choose the grid
per request would defeat the entire point of pre-registering it before
looking at results (that would just be overfitting under a different
name). `sources` is required (comma-separated, at least one recognized
source).

```json
{
  "symbol": "MNQ1!", "timeframe": "5m",
  "grid": {"stop_mults": [1.0, 1.5, 2.0], "target_mults": [1.5, 2.0, 2.5], "expiry_bars": [6, 12, 24], "total_combinations": 27},
  "day_session": { "...": "same shape as the day/session section below, computed once over the input candidates (same for every grid combination)" },
  "sources": ["analysis", "coordinator", "inverse_analysis"],
  "robustness": {
    "analysis": {
      "combinations_run": 27, "combinations_with_positive_pnl": 9, "combinations_with_profit_factor_above_1": 7,
      "median_win_rate_across_grid": 0.31, "min_total_pnl_usd": -180.4, "max_total_pnl_usd": 62.1
    },
    "coordinator": { "...": "same shape" },
    "inverse_analysis": { "...": "same shape" }
  },
  "combinations": {
    "stop1.0x_target1.5x_expiry6b": {
      "stop_mult": 1.0, "target_mult": 1.5, "expiry_bars": 6, "accepted_candidates": 12,
      "by_source": {
        "analysis": {"trades_taken": 12, "win_rate": 0.25, "win_rate_ci95_low": 0.0865, "win_rate_ci95_high": 0.5195, "profit_factor": 0.71, "total_pnl_usd": -84.2},
        "coordinator": { "...": "same shape" },
        "inverse_analysis": { "...": "same shape" }
      }
    },
    "...": "one entry per grid combination (27 by default)"
  }
}
```
`robustness` is the headline read: `combinations_with_positive_pnl` /
`combinations_with_profit_factor_above_1` out of `combinations_run`
show how often a source cleared a basic bar across the whole grid —
a real edge should hold up across most reasonable geometries, not just
the one config tested first. `combinations` has one compact entry per
grid point (full per-trade detail intentionally omitted to keep the
response a reasonable size across 27 combinations — for per-trade
detail on any single configuration, use the `paired` or `backtest-lite`
endpoints above with `include_trades=True` via direct/programmatic use
of `app/backtest`). Entirely offline: no LLM calls, no new data
collection, nothing written to any trade table. 400 if `sources` is
empty or contains an unrecognized value. `COORDINATOR_THRESHOLD` and
the Coordinator's own scoring are untouched — read-only analysis, same
as every diagnostic tier before it.

---

## Day/session reporting (Tier 3.18)

The third external review's item 5: "day/session trade counts should
be a primary reported metric everywhere, not buried." A headline
`candidates_considered: 100` can look like a decent sample while
actually spanning very few genuinely independent trading days —
candidates on a fast timeframe cluster tightly in calendar time, and
two decisions minutes apart in the same session are far closer to one
data point than two. `app/backtest.compute_day_session_breakdown()`
answers this and is now wired into the top level of every backtest-
lite/paired/grid/champion-challenger response above (champion-
challenger reports it once per calibration/validation window, since
out-of-sample validity depends directly on how many independent days
each window spans) — plus exposed standalone here for a quick check
before running anything heavier.

### `GET /candidates/history/day-session-report?symbol=MNQ1!&timeframe=5m&limit=300`

```json
{
  "symbol": "MNQ1!", "timeframe": "5m",
  "candidates_considered": 100,
  "distinct_trading_days": 14,
  "candidates_per_day": {"min": 2, "median": 6.5, "max": 19},
  "by_session_name": {"RTH": 71, "OVERNIGHT": 29},
  "by_timing_session_label": {"new_york": 38, "london": 22, "new_york_pm": 11, "london_ny_overlap": 9, "outside_sessions": 20},
  "unknown_trading_date_count": 0
}
```

`distinct_trading_days` uses each bar's own Pine-computed
`trading_date` field (the CME/Globex session-rollover-aware value
already validated at ingestion — Tier 2.9 — not a naive UTC
calendar-date split), falling back to
`app.trading_calendar.expected_trading_date()` applied to the
candidate's own anchor timestamp for the rare candidate with no stored
bar (very old data, or the manual `/coordinator/decide` path).
`candidates_per_day` (min/median/max) shows how concentrated
candidates are within a day — a `max` far above the `median` flags a
single busy day disproportionately shaping the whole sample.
`by_session_name` is the bar's own coarse `RTH`/`OVERNIGHT` split;
`by_timing_session_label` is Timing's finer classification (already
computed for every decision — `london`, `new_york`, `new_york_pm`,
`london_ny_overlap`, `outside_sessions`, plus `weekend`/`holiday` on
the rare candidate anchored then). `unknown_trading_date_count` is how
many candidates couldn't be dated at all (no stored bar and no
resolvable anchor timestamp) — flagged, never silently dropped.

Entirely offline and read-only: no LLM calls, no new data collection,
no effect on `COORDINATOR_THRESHOLD` or any trading logic.

---

## Trading-date integrity (Tier 3.19)

The fourth external review (2026-08-18): after Tier 3.18 shipped,
production showed `distinct_trading_days` stuck at 4 even as
`candidates_considered` grew by 43 over a window that included a
genuine trading weekday. The review pointed out a previously-
unverified assumption in `compute_day_session_breakdown()`: it trusts
each bar's payload `trading_date` field at face value the moment it's
present, and `unknown_trading_date_count == 0` only proves a value
existed — never that it was *correct*. Tier 2.9's `check_trading_date()`
already computes a mismatch warning (`calendar_warning`) at webhook
ingestion, but it was only ever returned/logged per event, never
persisted on the candidate or aggregated — a systematic mismatch (a
stale Pine Script value, a DST edge case, clock skew) would have been
completely invisible in every report built since.

`app/backtest.compute_trading_date_integrity_report()` is the direct
check: for every candidate with a stored bar it cross-checks THREE
independent views of what trading day it belongs to.

### `GET /candidates/history/trading-date-integrity?symbol=MNQ1!&timeframe=5m&limit=300`

```json
{
  "symbol": "MNQ1!", "timeframe": "5m",
  "candidates_considered": 275,
  "candidates_missing_bar": 0,
  "candidates_bar_missing_trading_date": 0,
  "payload_trading_dates": {"2026-08-13": 78, "2026-08-14": 76, "2026-08-17": 77, "2026-08-18": 44},
  "computed_trading_dates": {"2026-08-13": 78, "2026-08-14": 76, "2026-08-17": 77, "2026-08-18": 44},
  "utc_calendar_dates": {"2026-08-13": 78, "2026-08-14": 76, "2026-08-17": 77, "2026-08-18": 44},
  "distinct_payload_trading_days": 4,
  "distinct_computed_trading_days": 4,
  "distinct_utc_calendar_dates": 4,
  "mismatch_count": 0,
  "mismatch_examples": [],
  "mismatch_examples_truncated": false,
  "earliest_anchor_timestamp": "2026-08-13T09:05:00Z",
  "latest_anchor_timestamp": "2026-08-18T04:45:00Z"
}
```

(The numbers above are a placeholder shape, not yet a confirmed
production pull — see the fourth realignment package for status.)

- `payload_trading_dates` / `distinct_payload_trading_days`: the
  literal wire value from each bar, unmodified — what
  `day-session-report` currently trusts.
- `computed_trading_dates` / `distinct_computed_trading_days`: each
  bar's timestamp re-run through `app.trading_calendar.
  expected_trading_date()` (the same CME/Globex rollover convention
  `check_trading_date()` already applies at ingestion — just re-run
  here so the result is visible/aggregable instead of living only in a
  per-event log line).
- `utc_calendar_dates` / `distinct_utc_calendar_dates`: a THIRD, fully
  independent view — the anchor timestamp's own plain UTC calendar
  date, no NY-timezone/session-rollover adjustment at all. If this
  view shows MORE distinct dates than the other two, the rollover
  convention itself is collapsing days together, not the underlying
  data; if all three agree, a stagnant day count reflects a real
  data/ingestion fact rather than a reporting artifact.
- `mismatch_count` (never truncated) and `mismatch_examples` (capped
  at `TRADING_DATE_MISMATCH_EXAMPLE_LIMIT = 20`, with
  `mismatch_examples_truncated` flagging when more exist beyond the
  cap) — each example carries `candidate_id`, `event_id`,
  `anchor_timestamp`, `payload_trading_date`, `computed_trading_date`,
  so a genuine mismatch can be traced back to the exact webhook event
  that produced it.
- `earliest_anchor_timestamp` / `latest_anchor_timestamp`: the
  candidate set's real time span, for a quick sanity check that a
  `limit` parameter reached as far back as expected.

Deliberately a separate endpoint from `day-session-report` rather than
merged into it — this is a forensic/validation tool (its
`mismatch_examples` payload can be sizable on a long history), not a
routine summary metric. Entirely offline and read-only: no LLM calls,
no new data collection, no effect on `COORDINATOR_THRESHOLD` or any
trading logic.

---

## Experiment registry (Tier 3.20, hardened Tier 3.23)

The fourth external review (2026-08-18): every finding this project
has produced has been retrospective, and the weekly scheduled check
watching for a 15-distinct-day threshold (Tier 3.18) is watching the
SAME growing candidate pool every diagnostic tier keeps mining for
ideas — by the time that threshold fires, none of its candidates will
be a clean holdout in any normal sense, even with scoring untouched
the whole time. This is a lightweight, append-only pre-registration
mechanism: freeze a hypothesis, a stopping rule, and a snapshot of the
live scoring config, then only count candidates inserted AFTER that
moment toward it. Deliberately not the full "shadow trading engine" a
prior review gestured at — the review's own guidance was that a simple
registration + one-time resolution log is enough for now; Tier 3.23
made that honest naming explicit — see below.

**Tier 3.23 (fifth external review, 2026-08-19).** The fifth review
praised the pre-registration IDEA but found the EXECUTION incomplete —
`locked_config` was recorded but never actually used. Six fixes:

1. **Re-scoring, not trusting stored decisions.** If `direction_source`
   is `"coordinator"`, every prospective candidate is now re-scored via
   `app.replay.replay_candidate()` under the experiment's FROZEN
   weights/threshold/min_available_weight before any backtest runs —
   closing the gap where a live config change mid-experiment could
   silently blend two different scoring configs into one resolution.
   (Other `direction_source` values never depended on Coordinator
   weights, so nothing to re-score there.)
2. **Geometry locking.** `atr_stop_mult`/`atr_target_mult`/
   `expiry_bars`/`non_overlapping` are captured at registration and
   threaded through as real parameters at evaluation/resolution.
   `slippage_points`/`commission_per_contract`/`backtest_logic_version`
   aren't parametrizable in `app.backtest` yet, so those are
   drift-CHECKED instead — see `geometry_drift` below.
3. **Structured `target_metrics`.** No longer a free-text list —
   `primary_metric` (one of `win_rate`/`profit_factor`/`avg_pnl_usd`/
   `median_pnl_usd`/`total_pnl_usd`/`max_drawdown_usd`/`trades_taken`/
   `wins`/`losses`), `comparator` (one of `>=`/`<=`/`>`/`<`/`==`),
   `success_threshold` (a number), optional `secondary_metrics`
   (reported, not gated). `resolve_experiment()` now computes whether
   the primary metric actually met its bar.
4. **`registered_watermark_rowid`, not `registered_at`.** The
   no-peeking boundary is now a monotonic integer (the highest
   `trade_candidates.rowid` that existed at registration), not a
   second-precision timestamp string comparison — no same-second tie
   is possible.
5. **No silent truncation.** The prospective-candidate query is
   unbounded (every candidate past the watermark), not a "newest
   2000" query that could quietly drop the OLDEST prospective
   candidates once a long-running experiment outgrew that limit.
   `EXPERIMENT_MAX_PROSPECTIVE_CANDIDATES` (default 20000) now raises
   `ExperimentError` loudly instead.
6. **Honest naming.** This is a prospective experiment registry with
   one-time aggregate resolution, not yet a full append-only shadow
   evaluation engine (no per-candidate outcome ledger) — a real step
   toward that, not the same thing under a bigger name.

### `POST /experiments?symbol=MNQ1!&timeframe=5m&hypothesis=...&primary_metric=win_rate&comparator=%3E%3D&success_threshold=0.55&secondary_metrics=profit_factor&min_distinct_trading_days=15` (secret required)

```json
{
  "experiment_id": "b0d1...", "symbol": "MNQ1!", "timeframe": "5m",
  "hypothesis": "Coordinator's blended decision beats Analysis alone on win_rate over the next 15 independent trading days",
  "locked_config": {
    "coordinator_threshold": 25.0,
    "weights": {"analysis": 0.4, "news": 0.25, "timing": 0.2, "macro": 0.15},
    "min_available_weight": 0.6,
    "analysis_required": true,
    "backtest_geometry": {
      "atr_stop_mult": 1.5, "atr_target_mult": 2.5, "expiry_bars": 24,
      "non_overlapping": true, "slippage_points": 0.25,
      "commission_per_contract": 2.0, "backtest_logic_version": "1"
    }
  },
  "target_metrics": {
    "primary_metric": "win_rate", "comparator": ">=", "success_threshold": 0.55,
    "secondary_metrics": ["profit_factor"]
  },
  "stopping_rule": {"min_distinct_trading_days": 15},
  "direction_source": "coordinator",
  "registered_at": "2026-08-19 09:40:03",
  "registered_watermark_rowid": 374,
  "status": "active", "resolved_at": null, "resolution": null
}
```

`locked_config` snapshots the CURRENT live scoring config AND backtest
geometry at registration — read-only, never mutates them, and never
changes even if the live values are later edited.
`analysis_required` (Tier 3.24) is a fifth locked, ENFORCED scoring
knob alongside `coordinator_threshold`/`weights`/`min_available_weight`
— see the Coordinator/replay sections above for what it gates.
Experiments registered before Tier 3.24 have no `analysis_required` key
in their stored `locked_config`; re-scoring defaults a missing key to
`true` (the only value it has ever actually had live), never to
`false`.
`registered_watermark_rowid` is the real no-peeking boundary (see
point 4 above); `registered_at` is kept for display/audit only.
`stopping_rule` accepts `min_distinct_trading_days` and/or
`min_accepted_trades` (the latter computed via
`compute_backtest_comparison`'s `trades_taken` for `direction_source`
— the same non-overlapping-schedule trade count backtest-lite already
reports, run against LOCKED-config-rescored candidates as of Tier
3.23); at least one is required. 400 on an empty hypothesis, an
invalid `target_metrics` (unknown `primary_metric`/`comparator`, a
non-numeric `success_threshold`, an unknown `secondary_metrics`
entry), an empty/unrecognized `stopping_rule`, or an unknown
`direction_source`.

### `GET /experiments?symbol=MNQ1!&timeframe=5m`

Every registered experiment, newest first — append-only history, not
a "latest" view. Omit both params to list across every symbol/
timeframe.

### `GET /experiments/{experiment_id}`

The full record plus a live, read-only `stopping_rule_status` computed
against prospective (post-registration) candidates right now —
checking this as often as desired never resolves the experiment or
consumes anything:

```json
{
  "...": "...",
  "stopping_rule_status": {
    "prospective_candidates_considered": 40,
    "checks": {"min_distinct_trading_days": {"required": 15, "actual": 3, "met": false}},
    "stopping_rule_met": false,
    "geometry_drift": null
  }
}
```

`geometry_drift` (Tier 3.23) is `null` when live slippage/commission/
backtest-logic-version still match what was locked, or an object
naming exactly which of those three drifted (`{"locked": ..., "live":
...}` per key) if not — surfaced loudly rather than silently blended
into the backtest. Raises `ExperimentError` (surfaced as a 500 from
this endpoint, since it's an unusual/unexpected condition, not a
normal 4xx) if the prospective window has grown past
`EXPERIMENT_MAX_PROSPECTIVE_CANDIDATES`. 404 if `experiment_id`
doesn't exist.

### `POST /experiments/{experiment_id}/resolve` (secret required)

The one-time outcome recording. 409 if the stopping rule isn't met yet
(check `GET /experiments/{id}` first — this endpoint never forces an
early look). Once resolved, returns the SAME `resolution` on every
subsequent call — calling it again after more data accumulates never
recomputes it; `resolution` embeds a `day_session` breakdown, a full
`compute_backtest_comparison` result (Tier 3.23: run against
LOCKED-config-rescored candidates, with locked geometry parameters),
`target_metrics_result` (Tier 3.23: whether the pre-registered primary
metric actually met its comparator/threshold — `met` is `null`, not
`false`, when the metric itself is undefined, e.g. `profit_factor`
with no losses yet), and `geometry_drift` (same shape as above) —
computed ONLY from prospective candidates as of the moment the
stopping rule was first satisfied. 404 for an unknown `experiment_id`.

```json
{
  "...": "...",
  "resolution": {
    "resolved_from_candidates_considered": 22,
    "day_session": { "...": "..." },
    "backtest": { "...": "..." },
    "target_metrics_result": {
      "primary_metric": "win_rate", "comparator": ">=", "success_threshold": 0.55,
      "actual": 0.61, "met": true,
      "secondary_metrics": {"profit_factor": 1.8}
    },
    "geometry_drift": null
  }
}
```

Entirely additive: no existing endpoint's behavior changes,
`COORDINATOR_THRESHOLD`/`WEIGHTS` are only read and snapshotted, never
modified, and no LLM calls are made anywhere in this flow.

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

## Cost/usage telemetry (Tier 3.15, health counters added Tier 3.25)

Three external review cycles in a row named the same gap: this
project had no visibility into what its own LLM calls actually cost.
Every `client.messages.create()` call site in Analysis/News/Macro/
Execution is now wrapped by `app/llm_telemetry.track_llm_call()`,
which logs exactly one row — success or failure — to a new
`llm_call_log` table: agent, model, a short `trigger_context`
(symbol/timeframe or similar), latency, input/output/cache token
counts, web_search call count (News/Macro use Claude's hosted
`web_search` tool), an estimated USD cost, and (Tier 3.25) a
`pricing_version`. A telemetry write failure is swallowed, never
allowed to break or mask the actual agent call it's observing — this
is purely observational, it never changes what an agent does.

**Tier 3.25 (fifth external review, item #5 — "cost telemetry
health").** Swallowing a telemetry write failure was always correct
(it must never break a real agent call) but it also made a telemetry
outage completely invisible — `get_llm_call_summary()` would just
quietly report fewer calls than actually happened. Two additive
fixes, no agent behavior changed:

  - **`telemetry_health`** (new field on `GET /system/llm-usage`, see
    below): THIS PROCESS's in-memory `attempted`/`written`/`failed`
    counters for the telemetry write itself, since `telemetry_started_at`
    (this process's start time — resets on every restart/redeploy, so
    "0 failures" always reads as "0 failures since telemetry_started_at",
    never as "0 failures ever"). `write_success_rate` is `written /
    attempted`, `null` (not `0`) when `attempted` is `0` — "no calls
    yet" is a real third state, not silently presented as either
    extreme. Plain in-process counters, not a durable/atomic ledger —
    a rough health signal, same "estimate, not authoritative" honesty
    already applied to `estimated_cost_usd` itself.
  - **`pricing_version`** — a hand-maintained marker (same pattern as
    `app.backtest.BACKTEST_LOGIC_VERSION`, Tier 3.23) for which of the
    five `TELEMETRY_*_COST_PER_MTOK`/`*_MULTIPLIER` constants below
    produced a row's `estimated_cost_usd`. Env-configurable via
    `TELEMETRY_PRICING_VERSION` (default `"1"`), bumped by hand
    whenever those constants change materially. Stamped on every new
    row; pre-Tier-3.25 rows backfilled to `"1"` (the constants haven't
    changed since Tier 3.15's launch, so this is a real fact, not a
    guess). `get_llm_call_summary()`'s `pricing_versions_present`
    reports every distinct value in the queried window — more than one
    means the window's `total_estimated_cost_usd` blends two pricing
    regimes, surfaced loudly rather than silently averaged away.

### `GET /system/llm-usage?since=2026-08-16T00:00:00Z&recent_limit=20&recent_agent=analysis`
`since` (optional, ISO timestamp) restricts the aggregation window;
omit for all-time. `recent_limit`/`recent_agent` control the raw
recent-calls tail (default last 20 calls across all agents).

```json
{
  "since": null,
  "overall": {
    "total_calls": 214, "successful_calls": 211, "failed_calls": 3,
    "total_input_tokens": 187400, "total_output_tokens": 52100,
    "total_web_search_requests": 38, "total_estimated_cost_usd": 1.2137
  },
  "by_agent": {
    "analysis": {
      "total_calls": 120, "successful_calls": 119, "failed_calls": 1,
      "total_input_tokens": 96000, "total_output_tokens": 28000,
      "total_web_search_requests": 0, "total_estimated_cost_usd": 0.472,
      "avg_latency_ms": 1840.3
    },
    "news": { "...": "same shape" },
    "macro": { "...": "same shape" },
    "execution": { "...": "same shape" }
  },
  "pricing_versions_present": ["1"],
  "recent_calls": [
    {
      "id": 214, "called_at": "2026-08-16 12:03:44", "agent": "analysis", "model": "claude-sonnet-5",
      "trigger_context": "MNQ1!/5m", "success": 1, "error_message": null, "latency_ms": 1712.4,
      "input_tokens": 812, "output_tokens": 240, "cache_creation_input_tokens": 0,
      "cache_read_input_tokens": 0, "web_search_requests": 0, "estimated_cost_usd": 0.004024,
      "pricing_version": "1"
    }
  ],
  "telemetry_health": {
    "telemetry_started_at": "2026-08-19T10:30:00Z",
    "pricing_version": "1",
    "attempted": 214, "written": 214, "failed": 0,
    "write_success_rate": 1.0
  }
}
```

`estimated_cost_usd` throughout is exactly that — an **estimate**
computed from this project's own logged token counts against pricing
constants, not an authoritative billing figure (this project has no
Anthropic Console billing access to verify actual charges against).
Useful for relative comparison and trend-watching (which agent costs
the most, is cost trending up over time), not as an invoice. Pricing
constants are env-configurable (see below) since actual API pricing
changes over time.

Entirely read-only — does not affect any agent's behavior, prompt, or
output in any way.

---

## Coordinator/Analysis divergence + ablation (Tier 3.16, corrected Tier 3.17, reclassified Tier 3.21, threshold-crossing deep dive added Tier 3.26, News urgent-vs-directional decomposition added Tier 3.27, News urgent vs. calendar blackout added Tier 3.28, opinion-level/day-blocked re-aggregation added to all three Tier 3.29, risk-filter veto attribution added Tier 3.31)

Every backtest-lite/paired/grid result since Tier 3.10 has shown
Coordinator's own blended decision performing at or below Analysis
alone, and the third external review pushed back on investigating
that with a plain "do the two directions match?" check — it
conflates several genuinely different situations into one number and
can't say whether Coordinator's blending of News/Macro/Timing on top
of Analysis ever actually changes an outcome (versus just agreeing or
staying silent). `app/coordinator_diagnostics.py` answers this by
reusing the existing `app/replay.py` replay machinery (Tier 2.5)
rather than new scoring logic: every candidate already freezes its
full `opinions_used`/`contributions`/`conflict_flags` snapshot, so a
causal "would this specific decision have been different without
News?" question can be answered entirely offline by re-scoring that
frozen snapshot with that agent's opinion removed.

### `GET /candidates/history/coordinator-divergence?symbol=MNQ1!&timeframe=5m&limit=1000`

Real production response (197 candidates, 2026-08-16), cross-verified
via two independently-phrased fetches:

```json
{
  "symbol": "MNQ1!",
  "timeframe": "5m",
  "candidates_considered": 197,
  "cross_tab": {
    "neutral": { "no_trade": 23, "insufficient_data": 13, "enter_long": 2 },
    "directional": { "no_trade": 36, "insufficient_data": 23, "enter_long": 97, "enter_short": 2 },
    "unavailable": { "enter_long": 1 }
  },
  "named_categories": {
    "analysis_neutral_coordinator_no_trade": 23,
    "analysis_directional_coordinator_no_trade": 36,
    "analysis_directional_coordinator_insufficient_data": 23,
    "analysis_neutral_coordinator_insufficient_data": 13,
    "analysis_directional_coordinator_same_direction": 99,
    "analysis_neutral_coordinator_directional": 2,
    "analysis_unavailable_coordinator_enter_long": 1
  },
  "news_impact": {
    "present_and_directional": 123, "opposed_analysis_direction": 30,
    "avg_abs_contribution_when_present": 14.827
  },
  "macro_impact": {
    "present_and_directional": 94, "opposed_analysis_direction": 12,
    "avg_abs_contribution_when_present": 10.414
  },
  "timing_blocked_count": 0,
  "ablation": {
    "analysis_removed": {
      "candidates_considered": 197, "agent_present_count": 196,
      "decision_changed": 161, "decision_unchanged": 36,
      "decision_changed_by_category": { "to_insufficient_data": 161 },
      "conflict_flags_changed_count": 0,
      "avg_abs_score_delta_when_changed": null, "avg_abs_score_delta_when_unchanged": null,
      "transitions": { "no_trade -> insufficient_data": 59, "enter_long -> insufficient_data": 100, "enter_short -> insufficient_data": 2 }
    },
    "news_removed": {
      "candidates_considered": 197, "agent_present_count": 138,
      "decision_changed": 65, "decision_unchanged": 132,
      "decision_changed_by_category": { "threshold_crossing": 27, "to_insufficient_data": 38 },
      "conflict_flags_changed_count": 12,
      "avg_abs_score_delta_when_changed": null, "avg_abs_score_delta_when_unchanged": null,
      "transitions": { "no_trade -> enter_short": 25, "enter_long -> no_trade": 2, "enter_long -> insufficient_data": 26, "no_trade -> insufficient_data": 10, "enter_short -> insufficient_data": 2 }
    },
    "macro_removed": {
      "candidates_considered": 197, "agent_present_count": 124,
      "decision_changed": 26, "decision_unchanged": 171,
      "decision_changed_by_category": { "threshold_crossing": 2, "to_insufficient_data": 24 },
      "conflict_flags_changed_count": 0,
      "avg_abs_score_delta_when_changed": null, "avg_abs_score_delta_when_unchanged": null,
      "transitions": { "enter_long -> no_trade": 2, "enter_long -> insufficient_data": 19, "no_trade -> insufficient_data": 3, "enter_short -> insufficient_data": 2 }
    }
  }
}
```

Real, post-fix production response, re-pulled and cross-verified via
two independently-phrased fetches after Tier 3.17 (`cf442c2`) deployed
(2026-08-16). `agent_present_count` for News (138) and Macro (124) is
noticeably higher than their `present_and_directional` counts above
(123/94) — presence includes candidates where the agent had a
`neutral` opinion, not just a directional one. The `decision_changed_
by_category`/`conflict_flags_changed_count`/`avg_abs_score_delta_*`
fields are Tier 3.21 additions — the `null` averages and the
`news_removed`/`macro_removed` category splits shown in this 197-
candidate example are illustrative placeholder shapes from before the
field went live, kept here only as part of the historical Tier 3.16/
3.17 record. `analysis_removed`'s category split was never a
placeholder — it's mathematically guaranteed to be 100%
`to_insufficient_data`, explained below. See "Tier 3.21 confirmed
production numbers" further down for the real, cross-verified
post-3.21 figures on a larger (300-candidate) sample.

`cross_tab` is the complete picture — every candidate falls into
exactly one `analysis_bucket -> coordinator_decision` cell.
`named_categories` reads the reviewer's five specific categories
directly off that same cross_tab (same direction, opposite direction,
Coordinator `no_trade`/`insufficient_data` while Analysis was
directional, and Analysis neutral while Coordinator was directional
anyway). Notably, `analysis_directional_coordinator_opposite_direction`
doesn't appear at all in the production data above — 0 out of 99
directional-vs-directional candidates, meaning Coordinator's blend has
never once flipped to the opposite side of Analysis's own lean in this
history; it only ever reinforces or withholds. `news_impact`/
`macro_impact` report how often each agent was present and
directional, its average absolute weighted contribution when present,
and how often its direction opposed Analysis's own direction — a
same-direction contribution mostly just reinforces Analysis, an
opposing one is where blending could actually change the outcome.
`timing_blocked_count` counts candidates where Timing's veto
(`timing_market_closed`) or dampen (`timing_low_liquidity_dampened`)
flag actually fired (0/197 in this history so far).

`ablation` replays every candidate three times, once per directional
agent, with that agent's actual opinion removed from its frozen
snapshot (added to `missing_agents`, `app/coordinator.WEIGHTS` itself
never touched) and reports how many final decisions actually change —
a real causal measure of whether that agent's presence changes
outcomes, not just how often it happened to agree with Analysis.
`agent_present_count` is how many candidates actually had that
agent's opinion to remove; `decision_changed` can never exceed it.
`transitions` breaks changed decisions down by
`"{original} -> {replayed}"` pair.

**Tier 3.17 correction.** The first cut of this ablation (shipped as
Tier 3.16) modeled "remove agent X" by zeroing X's weight in the
`WEIGHTS` dict passed to the replay. That looked equivalent to
removing X's opinion but wasn't: the `MIN_AVAILABLE_WEIGHT` gate's
denominator (`directional_weight_total`) sums weights over ALL three
directional agents regardless of which were actually present for a
given candidate, so zeroing a weight shrank that denominator for
*every* candidate being replayed — including ones where the ablated
agent was never present to begin with, artificially pushing some of
them over the 0.6 availability bar through pure renormalization.

The pre-fix production numbers (same 197-candidate dataset above) were
`analysis_removed.decision_changed=68`, `news_removed.decision_changed=51`,
`macro_removed.decision_changed=39`. Tracing the transition breakdown
by hand: exactly 36 candidates — every one of them "Analysis alone
present" (News and Macro both absent) — flipped out of
`insufficient_data` under BOTH the News-ablation and the Macro-ablation
pass, with an identical `6/13/17` transition split each time. That
duplication across two supposedly-independent ablations, on candidates
where the "removed" agent was never present in the first place, is the
signature of the renormalization artifact, not a real finding about
either agent's influence.

The fix removes the agent's actual opinion from the frozen snapshot
instead of zeroing its weight, keeping `directional_weight_total` at
its normal live value — a candidate where the agent was never present
is now provably a no-op (see the Tier 3.17 regression test in
`tests/test_coordinator_diagnostics.py`). The confirmed post-fix
production numbers (in the response above) turned out noticeably
*higher* than a naive hand-subtraction of the 36-candidate artifact
would suggest — News is genuinely pivotal in 65/138 (47%) of the
candidates where it actually had an opinion, and Macro in 26/124
(21%), both well above the pre-fix raw percentages (26% and 20% of
all 197). Analysis is pivotal in 161/196 (82%) — and this one has a
clean structural explanation, not just "Analysis is very influential":
`WEIGHTS` gives News+Macro a combined 0.25+0.15=0.40 of the 0.80
directional total, which can never reach the 0.6 `MIN_AVAILABLE_
WEIGHT` fraction (0.40/0.80=0.50 < 0.6) even with BOTH present and
fully confident. Under the current config, Coordinator structurally
cannot reach a directional decision on News+Macro alone, regardless
of signal strength — Analysis isn't just the most-weighted agent, its
presence is a hard precondition for the system to decide anything at
all. That's a sharper, previously-invisible finding this fix exposed.

**Tier 3.21 reclassification.** The fourth external review pointed out
that even the corrected raw percentages above (82%/47%/21%) still
conflate two different effects into one "decision_changed" number: a
*quorum* effect (removing this agent alone dropped available evidence
below `MIN_AVAILABLE_WEIGHT` — says nothing about whether the agent's
DIRECTION was useful) and a genuine *directional-influence* effect
(the weighted score moved enough to cross `COORDINATOR_THRESHOLD`
among candidates that stayed data-sufficient either way). Reporting
"Macro changed 21% of decisions" without this split reads as "Macro's
direction mattered 21% of the time," which overstates what was
actually measured — most of that 21% could just be Macro completing
the quorum, not adding real signal.

`decision_changed_by_category` splits every change into exactly one
of three mutually exclusive buckets: `to_insufficient_data` (the
quorum effect — ablation is monotonic in this direction, since
removing evidence can only shrink availability, never grow it),
`direction_flipped` (both sides stayed data-sufficient and
directional, but the call reversed bullish↔bearish — the strongest
form of "this agent's direction mattered"), and `threshold_crossing`
(everything else that changed, e.g. `enter_long <-> no_trade` — the
score moved across one boundary without reversing sign).
`conflict_flags_changed_count` counts how often the ablated agent's
removal also changed which `conflict_flags` fired (mainly relevant to
News, since `analysis_news_conflict` can only exist when both Analysis
and News are present). `avg_abs_score_delta_when_changed`/`_when_
unchanged` report the raw magnitude of score movement either way —
useful even for a candidate whose decision *category* didn't change,
since a near-zero average there is itself evidence the agent isn't
moving the needle much.

A genuinely non-obvious finding surfaced while building this: under
the LIVE `weights`/`threshold`/`min_available_weight`,
`direction_flipped` turns out to be **mathematically unreachable** for
*any* single agent's ablation. Working through the renormalized-
denominator algebra for each of the three agents shows that removing
one agent's raw contribution — even at full confidence — is never
enough to both let the original decision cross `+threshold` AND flip
the post-ablation score past `-threshold`; the math only allows a
change that stops at `to_insufficient_data` or crosses at most one
boundary (`threshold_crossing`). `analysis_removed`'s category split
is consequently guaranteed to be 100% `to_insufficient_data` under the
live config, not just empirically observed to be — a structural fact
about the current weights, not a coincidence of the specific 197-
candidate sample.

Entirely offline and read-only — no LLM calls, no new candidates or
trades, no effect on `COORDINATOR_THRESHOLD` or the live scoring
config.

**Tier 3.21 confirmed production numbers.** Pulled from production
(`b57e0d6`, MNQ1!/5m, 300 candidates) via two independently-phrased
cross-verified fetches, both matching exactly:

```json
{
  "candidates_considered": 300,
  "ablation": {
    "analysis_removed": {
      "agent_present_count": 300, "decision_changed": 223, "decision_unchanged": 77,
      "decision_changed_by_category": { "to_insufficient_data": 223 },
      "avg_abs_score_delta_when_changed": 33.53
    },
    "news_removed": {
      "agent_present_count": 223, "decision_changed": 40, "decision_unchanged": 260,
      "decision_changed_by_category": { "threshold_crossing": 32, "to_insufficient_data": 8 },
      "avg_abs_score_delta_when_changed": 30.34
    },
    "macro_removed": {
      "agent_present_count": 215, "decision_changed": 2, "decision_unchanged": 298,
      "decision_changed_by_category": { "threshold_crossing": 2 },
      "avg_abs_score_delta_when_changed": 9.59
    }
  }
}
```

This confirms the reclassification empirically, on a larger sample
than the original 197-candidate dataset: `analysis_removed` is 100%
`to_insufficient_data` as proven (223/223, zero `threshold_crossing`
or `direction_flipped`) — a pure quorum effect, not evidence Analysis's
*direction* was right 74% of the time. `macro_removed` is now almost
entirely inert (2/300, both `threshold_crossing`, zero quorum effect —
Macro was present in only 215/300 candidates and rarely pivotal even
then). `news_removed` is the one case with a real mix: 32 of its 40
changes are genuine `threshold_crossing` (News moved the score across
`COORDINATOR_THRESHOLD` while both sides stayed data-sufficient) versus
only 8 quorum-only `to_insufficient_data` changes — meaning most of
News's ablation impact really is directional influence, not just
completing the availability gate, which is the opposite mix from what
the raw un-reclassified 47%-changed figure alone would suggest. As
expected, `direction_flipped` appears zero times across all three
agents, matching the proof above. `agent_present_count` for Macro
(215/300) also confirms it's the least-often-present directional
agent in this larger sample.

### `GET /candidates/history/threshold-crossing-deep-dive?symbol=MNQ1!&timeframe=5m&agent=news&limit=300&horizons=15,30,60`

Tier 3.26 (fifth external review, item #6). The `threshold_crossing`
counts above (News: 32/223-present; Macro: 2/215-present) say HOW
OFTEN removing an agent crossed the enter/no_trade line without a
quorum or direction-reversal effect involved — nothing about whether
that was good or bad for the strategy. This endpoint re-walks just one
agent's `threshold_crossing` subset and adds four dimensions per case:

```json
{
  "symbol": "MNQ1!",
  "timeframe": "5m",
  "agent": "news",
  "cases_considered": 2,
  "distinct_opinion_timestamps": 2,
  "cases": [
    {
      "candidate_id": "cand-1",
      "side": "agent_enabled_trade",
      "score_delta": -16.48,
      "agreement_with_analysis": "agree",
      "agent_flags": [],
      "agent_opinion_timestamp": "2026-08-16T14:00:00Z",
      "outcome": {"kind": "real_trade", "status": "closed", "outcome": "win", "pnl_usd": 160.0}
    },
    {
      "candidate_id": "cand-2",
      "side": "agent_prevented_trade",
      "score_delta": 4.77,
      "agreement_with_analysis": "oppose",
      "agent_flags": ["urgent"],
      "agent_opinion_timestamp": "2026-08-16T15:00:00Z",
      "outcome": {"kind": "prevented_hypothetical", "by_horizon": {"15": "prevented_win", "30": "prevented_loss", "60": "pending"}}
    }
  ],
  "summary": {
    "by_side": {"agent_enabled_trade": 1, "agent_prevented_trade": 1},
    "by_agreement_with_analysis": {"agree": 1, "oppose": 1},
    "urgent_flag_count": 1,
    "agent_enabled_trade_real_outcomes": {"win": 1},
    "agent_enabled_trade_hypothetical_outcomes_by_horizon": {"15": {}, "30": {}, "60": {}},
    "agent_prevented_trade_hypothetical_outcomes_by_horizon": {"15": {"prevented_win": 1}, "30": {"prevented_loss": 1}, "60": {"pending": 1}}
  }
}
```

(Illustrative shape from mixed fixture data, not a production pull —
this tier is diagnostic tooling delivered alongside its test suite,
not yet run against live production history.)

`agent` must be `analysis`, `news`, or `macro` (400 otherwise). Every
case is classified into exactly one `side`: `agent_enabled_trade` (the
ablated agent's presence is why a real trade was taken — without it,
the candidate would have been `no_trade`) or `agent_prevented_trade`
(the reverse). The two sides need different outcome machinery, since
only one of them ever really happened:

- `agent_enabled_trade` — the ORIGINAL candidate is a real historical
  decision, so `outcome` comes from `app.outcomes.compute_outcome_for_
  candidate()`: real closed-trade `win`/`loss`/`breakeven` with
  `pnl_usd` when a paper trade exists (`{"kind": "real_trade", ...}`),
  or the same existing hypothetical per-horizon `correct`/`incorrect`/
  `flat`/`pending`/`no_data` estimate otherwise
  (`{"kind": "hypothetical", "by_horizon": {...}}`).
- `agent_prevented_trade` — the replayed decision never became a real
  trade (it never happened), so `outcome` reuses `replay_candidate()`'s
  own hypothetical horizon estimate for the REPLAYED direction,
  relabeled: `"correct"` (the prevented trade would have won) becomes
  `"prevented_win"` — a missed opportunity — and `"incorrect"` becomes
  `"prevented_loss"` — the agent's real presence correctly avoided a
  loser. `"flat"`/`"pending"`/`"no_data"` keep their existing meaning.

`agreement_with_analysis` is whether the ablated agent's own real
opinion direction agreed or opposed Analysis's own real opinion
direction on that candidate — note this is independent of `side`: the
urgent-dampening example above shows a case where News and Analysis
*agree* on direction, yet News's presence still `agent_prevented_trade`
(coordinator.py halves the score whenever News's `"urgent"` flag is
set, regardless of agreement — see `app/coordinator.py`'s Tier 2.9
note). `agent_flags` are the agent's own self-reported flags on that
opinion — News and Macro use different vocabularies (News: `urgent`/
`low_data`/`stale_data`; Macro: `risk_off`/`conflicting_signals`/
`stale_data`), never unified into one list. `summary.urgent_flag_count`
specifically counts `"urgent"` — a flag only News's prompt defines —
so it reads `0` for `agent=macro` by construction, not because urgency
was checked and found absent. `distinct_opinion_timestamps` counts how
many of the returned cases trace back to distinct underlying LLM
opinions rather than the same slow-cadence News/Macro call being
reused across several consecutive candidates (fresh for up to
`NEWS_MACRO_MAX_AGE_MINUTES`, default 90 minutes) — a small case count
from a low-cadence agent can rest on very few independent calls.

Entirely offline for the ablation/replay step (no LLM calls,
`COORDINATOR_THRESHOLD`/`WEIGHTS` untouched, no candidate mutated);
the `agent_enabled_trade` real-outcome lookup reads trade rows the
same way every other outcome-aware endpoint in this project does.

### `GET /candidates/history/news-urgent-decomposition?symbol=MNQ1!&timeframe=5m&limit=300&horizons=15,30,60`

Tier 3.27 (sixth external review). Real Tier 3.26 production numbers
(News: 107 `threshold_crossing` cases, ~80% carrying News's `"urgent"`
flag) surfaced a real measurement gap the reviewer named directly:
`"urgent"` independently halves the blended score in
`app/coordinator.py`'s own scoring math regardless of direction or
agreement with Analysis, and the ablation behind `threshold-crossing-
deep-dive` removes News's opinion entirely — conflating that dampen
with News's genuine directional contribution into one "changed" number.
This endpoint decomposes the two without touching live scoring at all:

```json
{
  "symbol": "MNQ1!",
  "timeframe": "5m",
  "prevalence": {
    "candidate_level": {"news_present_candidates": 399, "urgent_candidates": 210, "urgent_rate": 0.526},
    "distinct_opinion_level": {"distinct_news_opinions": 48, "distinct_urgent_opinions": 9, "urgent_rate": 0.188}
  },
  "decomposition": {
    "cases_considered": 1,
    "distinct_opinion_timestamps": 1,
    "cases": [
      {
        "candidate_id": "cand-1",
        "attribution": "urgent_dampen_alone",
        "full_removal": {"changed": true, "category": "threshold_crossing", "score_delta": 5.39},
        "direction_only_removed": {"changed": false, "category": null, "score_delta": -12.5},
        "urgent_only_removed": {"changed": true, "category": "threshold_crossing", "score_delta": 21.87}
      }
    ],
    "summary": {"by_attribution": {"urgent_dampen_alone": 1}}
  }
}
```

(Illustrative shape from fixture-scale data for `decomposition`; the
`prevalence` numbers are a plausible full-history illustration, not a
production pull.)

**`prevalence`** answers the reviewer's second correction directly: the
86/107 figure from Tier 3.26 is *not* News's overall urgent rate — it's
the rate *within* a sample already pre-selected by `threshold_crossing`,
and `"urgent"` itself helps pull a candidate into that sample by
depressing its score toward the boundary. This instead reports
`"urgent"`'s unconditional share at the candidate level (every News-
present candidate) and, separately, at the distinct-opinion level (one
urgent LLM call can be reused across many candidates while fresh, per
Tier 3.6's reuse concern) — the honest base rate to compare 86/107
against.

**`decomposition`** answers the first correction: for each urgent-tagged
`threshold_crossing` case (same subset `threshold-crossing-deep-dive?
agent=news` would show), two additional partial-modification replays are
run against the frozen `opinions_used` snapshot — News present but
direction forced to `"neutral"` (zero weighted contribution;
`_DIRECTION_VALUE["neutral"]` is `0`; `"urgent"` stays in its flags, so
the dampen still applies — isolates the dampen ALONE), and News present
with its real direction/confidence but `"urgent"` stripped from its
flags (isolates the directional contribution ALONE). `attribution` is
one of: `"direction_alone"` (only the directional-only variant
reproduces the original full-removal's changed classification),
`"urgent_dampen_alone"` (only the urgent-only variant does),
`"both_independently_sufficient"` (either alone would have), or
`"only_combination_sufficient"` (a genuine interaction — neither alone
reproduces it, only removing both together does, the same thing the
existing ablation measures). Scoped to News only — Macro's flag
vocabulary (`risk_off`/`conflicting_signals`/`stale_data`) has no
`"urgent"` concept.

Entirely offline (no LLM calls, no candidate mutated,
`COORDINATOR_THRESHOLD`/`WEIGHTS`/`ANALYSIS_REQUIRED` untouched) — every
variant is a throwaway per-candidate copy used for one replay each, same
guarantee the rest of this diagnostic family gives.

---

### `GET /candidates/history/news-urgent-vs-calendar-blackout?symbol=MNQ1!&timeframe=5m&limit=300&window_hours=2.0&horizons=15,30,60`

Tier 3.28 (sixth external review, ranked backlog item #2). The
reviewer's exact ask, relayed verbatim: "قارنه بحظر بسيط مبني على تقويم
اقتصادي موثوق: امتنع قبل/بعد CPI/FOMC/NFP. إذا كان LLM لا يتفوق على
blackout ثابت، فلا يوجد سبب لدفع تكلفته أو الاعتماد على تصنيفه الحر."
(Compare News's `"urgent"` flag against a simple blackout built on a
trustworthy economic calendar: abstain before/after CPI/FOMC/NFP. If
the LLM doesn't outperform a fixed blackout, there's no reason to pay
its cost or rely on its free-text classification.)

New `app/economic_calendar.py` is a hardcoded, source-cited registry of
every real 2026 CPI/NFP/FOMC release timestamp — CPI and the Employment
Situation report (NFP) from the official BLS release schedule and the
White House's CY2026 PFEI schedule PDF, FOMC from the Federal Reserve's
own 2026 meeting calendar (full citations, DST handling, and the exact
sourcing reasoning are in that module's docstring). Its
`is_within_blackout_window()` check has zero access to News's opinion,
reasoning, or flags — it only compares a candidate's own bar timestamp
against that hardcoded registry. This endpoint tags every News-present
candidate with both signals independently and cross-tabulates them:

```json
{
  "symbol": "MNQ1!",
  "timeframe": "5m",
  "window_hours": 2.0,
  "news_present_candidates": 4,
  "data_range": {"start": "2026-08-12T11:00:00Z", "end": "2026-08-24T07:40:05Z"},
  "calendar_coverage": {
    "events_overlapping_data_range": [
      {"event": "CPI", "date": "2026-08-12", "timestamp_utc": "2026-08-12T12:30:00Z"}
    ],
    "event_count": 1
  },
  "cross_tab": {"both_flagged": 1, "news_urgent_only": 2, "neither_flagged": 1},
  "agreement_rate": 0.5,
  "outcomes_by_quadrant": {
    "news_urgent_only": {
      "real_trade": {},
      "hypothetical_by_horizon": {"15": {"correct": 2}, "30": {}, "60": {}}
    }
  },
  "cases": [
    {
      "candidate_id": "cand-1",
      "bar_timestamp": "2026-08-12T13:00:00Z",
      "decision": "no_trade",
      "quadrant": "both_flagged",
      "news_urgent": true,
      "calendar_blackout": true,
      "nearest_event": {"event": "CPI", "date": "2026-08-12", "timestamp_utc": "2026-08-12T12:30:00Z"},
      "distance_hours": 0.5,
      "outcome": null
    }
  ]
}
```

(Illustrative shape; not a production pull.)

**`cross_tab`** buckets every News-present candidate into exactly one of
four quadrants: `"both_flagged"` (News said urgent AND it's within
`window_hours` of a real event), `"news_urgent_only"` (News said urgent
but no real event is nearby — the case the reviewer's question is
really about), `"calendar_blackout_only"` (a real event is nearby but
News didn't flag urgent), or `"neither_flagged"`. `agreement_rate` is
`(both_flagged + neither_flagged) / news_present_candidates`.

**`outcomes_by_quadrant`** attaches an outcome to every candidate that
reached a directional decision (`enter_long`/`enter_short`) — real
closed-trade result when one exists (same preference as every other
outcome-aware endpoint in this project), the existing per-horizon
hypothetical estimate otherwise — bucketed per quadrant, so it's
possible to see which signal (News's judgment, or the fixed calendar)
actually correlated with worse outcomes on the cases where they
disagreed, not just how often they agreed.

**`calendar_coverage`** reports, honestly, how many real CPI/NFP/FOMC
events from the registry could even have produced an `in_blackout:
true` result somewhere in this specific `limit`-bounded history pull —
read this before treating `cross_tab` as a confident result. At this
tier's build time (2026-08-24), the live 9-trading-day production
window (`2026-08-12` through `2026-08-24`) contained exactly **one**
such event: the `2026-08-12` CPI release, which is also the very first
day of that window. No FOMC meeting fell in August 2026, and the
nearest NFP release (`2026-08-07`) predates the window entirely. This
means any single run of this endpoint against current production data
is a very thin sample — not yet a confirmatory comparison — and that
improves automatically as more weeks of data accumulate:
`2026-09-04` (NFP), `2026-09-11` (CPI), and `2026-09-15`/`16` (FOMC) are
already in the registry, waiting for the trading window to reach them.

`window_hours` defaults to `2.0`, matching News's own prompt language
about flagging events expected in "the next 2-3 hours" — tune it to see
how sensitive the comparison is to the blackout's width.

Entirely offline (no LLM calls, no candidate mutated,
`COORDINATOR_THRESHOLD`/`WEIGHTS` untouched); the outcome lookup reads
real trade rows the same way every other outcome-aware endpoint in this
project already does.

---

### `GET /candidates/history/risk-filter-veto-attribution?symbol=MNQ1!&timeframe=5m&limit=300`

Tier 3.31 (seventh external review), corrected Tier 3.32 (eighth
external review). `app/backtest.py`'s `"analysis_risk_filtered"`
direction source (Tier 3.30) bundles FOUR changes into one policy at
once — removing News from the directional vote, removing Macro from the
directional vote, removing the Coordinator's `MIN_AVAILABLE_WEIGHT`
quorum gate, and removing Timing's session/liquidity gating entirely
(that source never reads Timing at all) — so a trade-count difference
against the live Coordinator can't yet be attributed specifically to
"News/Macro became risk filters." This endpoint separates the four out
with real numbers, reusing the exact gating logic already frozen on
every stored candidate (Tier 2.1's `opinions_used`/`conflict_flags`
snapshot — the real historical Coordinator decision already encodes
whether Timing vetoed/dampened it) rather than any new replay.

**Scope correction (Tier 3.32):** the Timing finding below is proven
only for the AUTO-GENERATED webhook candidate path —
`should_run_analysis()` gates real-time Analysis runs to inside a kill
zone, so a Timing veto/dampen flag can never co-occur with a directional
Analysis opinion there. `POST /agents/analysis/run?ignore_timing_
gate=true` is a real manual-testing path that breaks that guarantee —
this is NOT a system-wide structural impossibility, just true for every
candidate this endpoint sees in normal operation (real production
history only ever contains auto-generated candidates).

For every candidate with a directional (bullish/bearish) Analysis
opinion, `summary` reports exactly one bucket per candidate:

- `news_urgent_veto` / `macro_risk_off_veto` — `analysis_risk_filtered`
  itself would skip this candidate (News's `"urgent"` flag or Macro's
  `"risk_off"` flag, checked in that priority order, matching
  `app.backtest._direction_for_source`'s own checks).
- `coordinator_agrees` — neither veto fires, and the real Coordinator
  traded the same direction — no blocking difference at all.
- `coordinator_opposite_direction` — neither veto fires, but the real
  Coordinator traded the OPPOSITE direction (structurally rare/unproven
  under live weights per Tier 3.21's `direction_flipped` proof, but not
  assumed impossible here).
- `coordinator_quorum_block` — neither veto fires, but the real
  Coordinator's decision was `insufficient_data`. In this endpoint's
  subpopulation (Analysis already confirmed present and directional)
  this can only mean News AND Macro were BOTH missing/stale — Analysis
  alone is 0.40/0.80 = 50% of `DIRECTIONAL_AGENTS`' combined weight,
  below the live 60% `MIN_AVAILABLE_WEIGHT` floor either way.
- `timing_market_closed_block` / `timing_low_liquidity_block` — neither
  veto fires, quorum was fine, but Timing's own flags forced the real
  Coordinator's score to zero or halved it below threshold
  (`app.coordinator._score_opinions`' `"timing_market_closed"`/
  `"timing_low_liquidity_dampened"` `conflict_flags`, set
  deterministically from Timing's own `market_closed`/`low_liquidity`
  flags — see `app/timing_agent.py`).
- `coordinator_score_below_threshold_other` — neither veto fires, quorum
  was fine, no Timing flag applied at all — the real Coordinator's
  blended score simply didn't cross ±threshold. **Tier 3.32 correction:**
  this is a genuine RESIDUAL/catch-all, not proof of "News/Macro
  opposition" by itself — see `score_below_threshold_breakdown` below,
  which was added specifically because the original Tier 3.31 name for
  this bucket (`news_macro_opposition_block`) claimed more than the code
  actually established.

`analysis_not_directional_excluded` counts candidates excluded before
any bucket (Analysis itself missing or neutral) — both policies skip
these identically, so they're not attributable to any veto or gate.

**`score_below_threshold_breakdown`** (Tier 3.32) splits the residual
bucket above, using only each present other agent's own stored
direction — no new replay:

- `directional_opposition` — News or Macro present with a direction that
  OPPOSES Analysis's.
- `neutral_dilution` — News or Macro present but direction `"neutral"` —
  contributes 0 to the weighted sum, diluting the renormalized average
  toward zero without actually opposing Analysis's direction.
- `agreement_low_confidence` — every present other agent agrees with
  Analysis's direction — the score fell short purely on confidence/
  weighting, not disagreement.

These three are exhaustive given at least one of News/Macro is
guaranteed present in this bucket (quorum already passed by the time a
case reaches it) — `"other"` is kept in the code as a defensive
fallback, not because it's expected to ever appear.

**`flag_prevalence`** (Tier 3.32) reports each veto flag's TRUE
independent count, not just its bucketed count — the priority order
above (News's `"urgent"` checked before Macro's `"risk_off"`) means
`summary.macro_risk_off_veto` alone UNDERSTATES Macro's real prevalence
whenever both flags fire on the same candidate (that case lands under
`news_urgent_veto` instead). `news_urgent_total` and `macro_risk_off_
total` count every case where that flag was set, regardless of which
bucket it landed in; `both_flags_overlap` counts cases where both fired
together.

```json
{
  "symbol": "MNQ1!",
  "timeframe": "5m",
  "candidates_considered": 300,
  "analysis_not_directional_excluded": 120,
  "analysis_directional_candidates": 180,
  "flag_prevalence": {
    "news_urgent_total": 25,
    "macro_risk_off_total": 8,
    "both_flags_overlap": 2
  },
  "score_below_threshold_breakdown": {
    "directional_opposition": 2,
    "neutral_dilution": 1
  },
  "summary": {
    "coordinator_agrees": 90,
    "coordinator_quorum_block": 40,
    "news_urgent_veto": 25,
    "timing_low_liquidity_block": 15,
    "macro_risk_off_veto": 6,
    "coordinator_score_below_threshold_other": 3,
    "timing_market_closed_block": 1
  },
  "cases": [
    {
      "candidate_id": "abc123",
      "bar_timestamp": "2026-08-16T14:00:00Z",
      "trading_date": "2026-08-16",
      "analysis_opinion_timestamp": "2026-08-16T14:00:00Z",
      "analysis_direction": "bullish",
      "coordinator_decision": "enter_long",
      "coordinator_direction": "bullish",
      "news_urgent": false,
      "macro_risk_off": false,
      "attribution": "coordinator_agrees",
      "score_below_threshold_reason": null
    }
  ],
  "opinion_level_day_blocked": { "...": "see below" }
}
```

(Illustrative shape; not a production pull — note `macro_risk_off_total`
8 vs `summary.macro_risk_off_veto` 6 above, an example of the overlap
`flag_prevalence` exists to surface: 2 of Macro's 8 real `risk_off`
cases co-occurred with News's `urgent` and got bucketed there instead.)
`opinion_level_day_blocked` here uses the same shared aggregator as the
other diagnostics in this family (see below), but keyed on Analysis's
own `opinion_timestamp` rather than News/Macro's — this endpoint's
subject is a whole-policy comparison per candidate, not one reused
News/Macro opinion, so Analysis's opinion identity (the one field every
case here is guaranteed to have) is the correct one to weight by.

Entirely offline (no LLM calls, no candidate mutated,
`COORDINATOR_THRESHOLD`/`WEIGHTS`/`analysis_risk_filtered`'s own veto
scope all untouched).

---

### `opinion_level_day_blocked` (Tier 3.29 — present in all four endpoints above)

Sixth external review, ranked backlog item #3. Every field documented
above pools its cases as if each were an independent CANDIDATE — which
conflates two things: how many genuinely INDEPENDENT LLM opinions
actually drove the split (News/Macro run on a slow cadence and get
reused across many consecutive candidates while fresh), and whether the
split holds across many TRADING DAYS or is one unusually active/
volatile day dominating the pool. `threshold-crossing-deep-dive`,
`news-urgent-decomposition`, `news-urgent-vs-calendar-blackout`, and
(Tier 3.31) `risk-filter-veto-attribution` each gained this same
additive key — nothing about their existing fields (`cross_tab`,
`summary`, `by_attribution`, etc.) changed shape or meaning, so any
prior consumer of these endpoints keeps working unmodified.

```json
{
  "opinion_level_day_blocked": {
    "days_considered": 2,
    "distinct_opinions_total": 3,
    "uncategorized_count": 0,
    "by_day": {
      "2026-08-12": {
        "candidates_considered": 6,
        "distinct_opinions": 1,
        "category_counts_candidate_level": {"both_flagged": 6},
        "category_counts_opinion_weighted": {"both_flagged": 1.0}
      },
      "2026-08-16": {
        "candidates_considered": 4,
        "distinct_opinions": 2,
        "category_counts_candidate_level": {"news_urgent_only": 3, "neither_flagged": 1},
        "category_counts_opinion_weighted": {"news_urgent_only": 1.5, "neither_flagged": 0.5}
      }
    },
    "candidate_level_totals": {"both_flagged": 6, "news_urgent_only": 3, "neither_flagged": 1},
    "opinion_weighted_totals": {"both_flagged": 1.0, "news_urgent_only": 1.5, "neither_flagged": 0.5}
  }
}
```

(Illustrative shape; not a production pull. The category field itself
differs per endpoint — `side` for threshold-crossing-deep-dive,
`attribution` for news-urgent-decomposition, `quadrant` for
news-urgent-vs-calendar-blackout, and (Tier 3.31) `attribution` again
for risk-filter-veto-attribution — same field name as news-urgent-
decomposition by coincidence, but a completely different bucket set and
a different opinion identity, see that endpoint's own section above.)

**`category_counts_opinion_weighted`** is the honest number to read
first: each case is weighted `1 / (how many cases share its exact
(day, opinion) pair)`, so a single News/Macro opinion reused across
several consecutive candidates in the same day always contributes
exactly **1** total — split fractionally across whichever categories
its various candidates actually landed in (if the same reused opinion
combined with different Analysis/Macro context to different effect on
different candidates, that shows up as a fractional split, not a
silent exclusion or a 6x overcount). `category_counts_candidate_level`
is the existing raw per-candidate count kept right alongside it, so the
two can be compared directly — the worked example above (Aug 12: 6
candidates but only 1 distinct opinion) is exactly the illustrative
case Tier 3.28's own production pull showed in practice: `both_flagged`
looked like a 6-candidate result but was really one LLM judgment.

**`by_day`** lets a reader see whether a pooled split is broad across
many trading days or concentrated in one. **`distinct_opinions_total`**
counts unique opinion identities across the ENTIRE case set, not the
sum of each day's own distinct count — the two can differ by a handful
when an opinion reused right at a trading-day boundary genuinely
appears in both days (correct, not a bug: it really was used on both).
**`uncategorized_count`** counts cases where the day, opinion, or
category couldn't be determined (e.g. no `trading_date` was ever stored
for that bar) — excluded from every other field here rather than
silently dropped or guessed into a bucket.

Entirely offline: pure post-processing over each diagnostic's own
already-computed `cases` list — no new replays, no LLM calls, no
mutation of anything stored.

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
| `ANALYSIS_REQUIRED` | no | `true` | Tier 3.24: explicit "no directional decision without a current Analysis opinion" gate — a project-owner design decision, not data-driven; see the Replay/versioning section above |
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
| `BACKTEST_GRID_STOP_MULTS` | no | `1.0,1.5,2.0` | Tier 3.14: comma-separated ATR stop multiples in the pre-registered sensitivity grid — deploy-time only, deliberately not a query parameter (see the sensitivity-grid endpoint above) |
| `BACKTEST_GRID_TARGET_MULTS` | no | `1.5,2.0,2.5` | Tier 3.14: same, target multiples |
| `BACKTEST_GRID_EXPIRY_BARS` | no | `6,12,24` | Tier 3.14: same, expiry bar counts |
| `TELEMETRY_INPUT_COST_PER_MTOK` | no | `2.0` | Tier 3.15: estimated USD cost per million input tokens for `GET /system/llm-usage`'s `estimated_cost_usd` figures — confirmed against Claude Sonnet 5's published pricing on 2026-08-16, adjust if pricing changes |
| `TELEMETRY_OUTPUT_COST_PER_MTOK` | no | `10.0` | Tier 3.15: same, output tokens |
| `TELEMETRY_CACHE_WRITE_MULTIPLIER` | no | `1.25` | Tier 3.15: cache-write token cost as a multiple of the base input rate (Anthropic's standard prompt-caching formula) |
| `TELEMETRY_CACHE_READ_MULTIPLIER` | no | `0.1` | Tier 3.15: same, cache-read tokens |
| `TELEMETRY_WEB_SEARCH_COST_PER_SEARCH` | no | `0.01` | Tier 3.15: estimated USD cost per `web_search` tool call (News/Macro) — $10 per 1,000 searches |
| `TELEMETRY_PRICING_VERSION` | no | `1` | Tier 3.25: hand-maintained marker stamped on every `llm_call_log` row — bump by hand whenever the five `TELEMETRY_*` pricing constants above change materially, so `GET /system/llm-usage`'s `pricing_versions_present` can flag a blended-regime window instead of silently averaging it |

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
