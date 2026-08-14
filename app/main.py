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

Tier 3.2 (fill realism, same second review, Priority 2 — items 1-5 of
the user's own recommended ordering): app/paper_trades.py reworked
substantially. Every order — market or limit — now starts
"pending_fill" and only fills against a REAL subsequent bar;
ready_now is no longer a fill trigger (Execution's own belief a limit
is "ready" doesn't prove the market traded there), and a market order
fills at the NEXT bar's open rather than instantly at candidate-
creation time (the anchor bar has already closed, so filling "into"
it would be lookahead bias). Every lifecycle timestamp
(order_submitted_at/opened_at/closed_at) is now the triggering bar's
own EVENT time, not server-processing time — new *_processed columns
keep the server timestamp too, but purely as operational data, never
read by trading logic (this also fixes daily-loss trading-day
attribution, which reads these same fields). New ORDER_EXPIRY_MINUTES
cancels a pending order that never filled instead of leaving it
resting forever. Fill/exit pricing is more realistic: market entries
and stop exits apply SLIPPAGE_POINTS against the trader (a stop is
effectively a market order once triggered); a stop is also gap-
adjusted (if the bar's open already breached it, the realistic exit
is the open, not the stop price — same "never assume the better
outcome" convention this module already used for stop-vs-target
ordering, extended to gaps). COMMISSION_PER_CONTRACT (round-trip) is
subtracted from every closed trade's pnl_usd. app/outcomes.py updated
to handle the new "cancelled" trade status distinctly from "pending".

Tier 3.3 (account-wide atomic position/risk limits, same second
review — items 6-7 of the user's own recommended ordering, the last
items in that sequence): two fixes, closing the review's "MAX_OPEN_POSITIONS
not account-wide" and "position-limit enforcement race-prone"
findings, plus the daily-loss-bounded sizing gap.
  1. MAX_OPEN_POSITIONS is now enforced account-wide, not per
     symbol+timeframe — two different symbols could previously each
     independently reach "the limit", letting the account's combined
     open-position count run well past it. New
     app/storage.open_trade_if_room() is the single atomic commit
     point: one BEGIN IMMEDIATE transaction folds the idempotency
     check (does this candidate already have a trade?), the
     account-wide capacity check, and the insert into one operation,
     closing the exact count-then-insert race the review flagged —
     previously two separate, non-atomic steps in
     app/paper_trades.open_trade_from_candidate().
  2. size_position()'s trade-size budget used to be only
     RISK_FRACTION_PER_TRADE of remaining drawdown room — a trade could
     be sized past what's actually left in TODAY's daily-loss allowance
     as long as overall drawdown room was still generous. It's now
     min(that drawdown-room budget, remaining daily-loss room),
     whichever is tighter, recorded in the risk opinion's
     key_data.budget_binding_constraint and flagged
     "daily_loss_room_binding" when the daily-loss side is what capped
     (or rejected) the trade.
Per the user's own explicit instruction, COORDINATOR_THRESHOLD itself
was NOT touched in this tier or the two before it — tuning it is
intentionally the last item in the full sequence, after paper trading
is realistic enough to learn from.

Tier 3.4 (COORDINATOR_THRESHOLD tuning, the last item on the full
sequence — now that Tier 3.1-3.3 make paper trading realistic enough
to learn from): new GET /candidates/history/replay/threshold-sweep,
built on app/replay.sweep_thresholds(). Answers "across a range of
threshold values, how does directional decision volume and
hypothetical horizon accuracy change?" by re-scoring every historical
candidate's already-frozen opinions_used (Tier 2.5's replay
machinery — entirely offline, no LLM calls) under each threshold in
turn and aggregating the hypothetical horizon outcome (same estimate
outcomes.py already uses for candidates that never became a real
trade) into a compact per-threshold summary. weights/
min_available_weight stay fixed for the whole sweep so an accuracy
shift can be attributed to threshold alone. A live check against
production data (symbol=MNQ1!, timeframe=5m) found zero real closed
paper trades to date, so this hypothetical-horizon estimate is
currently the ONLY data available to tune against — not a real
backtest, an accuracy proxy, same caveat every hypothetical estimate
in this project already carries.

Tier 3.5 (per-agent signal quality): the threshold sweep above found
no COORDINATOR_THRESHOLD value getting hypothetical accuracy anywhere
near 50% (coin-flip) — but a threshold sweep can only ever say
something about the Coordinator's BLENDED decision, since it re-scores
_score_opinions() over all agents together. It can't distinguish "one
agent has real signal but is outweighed/drowned out by noisier ones"
from "no individual agent beats chance either" — two very different
problems requiring very different fixes (reweighting vs. reworking or
dropping an agent). New GET /candidates/history/outcomes/by-agent,
built on app/outcomes.compute_per_agent_accuracy(), answers this
directly: walks each historical candidate's frozen opinions_used
(same Tier 2.1 snapshot the sweep already reads) and scores every
individual Analysis/News/Macro directional call (Timing excluded —
always neutral by design, structurally outside DIRECTIONAL_AGENTS)
against the same hypothetical horizon estimate outcomes.py already
uses elsewhere, independent of what the Coordinator ultimately
decided. Anchored to each agent's own opinion timestamp when present,
falling back to the candidate's decision timestamp for older data
predating per-opinion timestamps. Entirely offline — no LLM calls, no
new data, no trade side effects — computed from the same 79 (at last
check) historical candidates already sitting in production, no new
data collection required.

Tier 3.6 (deduplicated per-agent accuracy): running Tier 3.5 against
production surfaced results strange enough to chase down before
trusting them — most notably Macro reading exactly 0/34 correct at the
30-minute horizon. Reviewed the scoring code end to end looking for a
sign/logic bug (_score_opinions, get_bar_at_or_before/after, each
agent's own "bullish means price rises" convention) and found none —
the real issue is a measurement one. News/Macro run on a schedule
independent of individual market bars and stay eligible for reuse
across every webhook bar for up to NEWS_MACRO_MAX_AGE_MINUTES (default
90) — confirmed directly against production data that a single News or
Macro opinion legitimately backs 4+ consecutive candidates. Tier 3.5's
by-agent endpoint tallied one data point per CANDIDATE, so a single
LLM call getting reused a dozen times looked like a dozen independent
data points — inflating the apparent sample size and letting one
unlucky (or lucky) call swing the whole aggregate.
compute_per_agent_accuracy() now returns two sibling views:
"by_candidate" (the original Tier 3.5 tally, unchanged in method) and
"by_distinct_opinion" (each unique (symbol, timeframe,
opinion_timestamp, direction) tuple scored exactly once, regardless of
reuse), plus "distinct_opinion_counts" so a caller can see directly
how much duplication a given "by_candidate" figure was resting on.
This is an additive change to a same-session, not-yet-externally-
depended-on endpoint (Tier 3.5 shipped and was queried against
production in this same investigation, nothing else consumes it yet),
so the response shape changed in place rather than versioning a new
endpoint.

Running Tier 3.6's by_distinct_opinion view against production
answered the "does this agent show signal" question concretely for
the first time: News/Macro only had 6 and 4 genuinely distinct
opinions respectively — nowhere near enough to say anything either
way — but Analysis had 78, and its accuracy across all three default
horizons (34.7% / 31.9% / 29.9%) is consistently and substantially
below the 50% coin-flip line on a real sample, not a duplication
artifact.

Tier 3.7 (per-opinion diagnostic detail): with a real finding on
Analysis in hand, the next question is WHY. New GET
/candidates/history/outcomes/by-agent/detail, built on
app/outcomes.compute_agent_opinion_detail(), returns one record per
distinct opinion for a given agent — opinion_timestamp, direction,
confidence, flags, reused_by_candidate_count, and outcome_by_horizon —
so a caller can check whether Analysis's wrong calls cluster around
low self-reported confidence, a flag it already raises about its own
read (choppy/conflicting_signals/low_data), or something else visible
per-opinion that the aggregate counts in the endpoint above can't
show. Deliberately excludes each opinion's free-text reasoning/
key_data to stay compact and WebFetch-reliable at scale — same design
constraint that shaped every other endpoint in this project meant to
be queried against production through this session.

Pulling Tier 3.7 against production and finding Analysis's accuracy
sitting around 30% across the board prompted a full external review of
this project's history (methodology, not just the Analysis finding).
Two concrete methodology gaps came out of it, addressed in Tier 3.8
below; the review also explicitly recommended AGAINST touching
Coordinator scoring/weights yet (confirmed the same by-agent evidence
that would justify a change was discovered on, and would be used to
calibrate, the same 100-opinion sample — fitting to history, not
validation) and against deferring COORDINATOR_THRESHOLD tuning purely
on volume grounds. Neither the Coordinator's scoring nor
COORDINATOR_THRESHOLD have been touched as a result of this review —
Tier 3.8 is, like 3.4-3.7 before it, read-only diagnostic tooling.

Tier 3.8 (methodology fixes): (1) every accuracy figure in this module
was anchoring an opinion's outcome to opinion.get("timestamp") — LLM-
call-completion wall-clock, not market time — or decision.get
("timestamp"), the exact same category of value from coordinator.py's
now_iso. New app/outcomes._resolve_anchor_timestamp() fixes this for
Analysis specifically: Tier 3.1 (causal integrity) already pins every
webhook-triggered candidate to the exact bar that triggered it, and
that bar's own real timestamp is now preferred. News/Macro are
unaffected — not bar-triggered, no better anchor available for them.
(2) New GET /candidates/history/outcomes/baseline-comparison, built on
app/outcomes.compute_baseline_comparison(): 50% coin-flip isn't
automatically the right null baseline for judging an agent's accuracy
against — if the market moved mostly one direction during the
measurement window, any fixed directional bias looks artificially
good or bad regardless of real skill. Reports the market's own base
rate (an "always guess bullish"/"always guess bearish" predictor's
accuracy over a window literally IS that window's up-rate/down-rate)
alongside a VWAP-side baseline and Analysis's own calls inverted as a
pure diagnostic (never acted on) — all on the same candidate
population and horizon machinery every other accuracy figure here
uses. (3) New optional `by_day=true` on GET /candidates/history/
outcomes/by-agent/detail, built on app/outcomes.summarize_opinions_by_day():
one agent's opinions across a single trading day are correlated, not
independent draws — this groups by calendar date so that clustering
is visible directly, as a deliberately cheap first step ahead of real
clustered/bootstrap statistics once there's more data across more days.

Tier 3.9 (auto-execution): the same external review flagged a
methodology gap Tier 3.8 didn't touch — every real paper trade taken
so far was manually selected by a human clicking through
/agents/risk/evaluate and /agents/execution/plan for whichever
candidates they chose to act on. That selection is itself a source of
bias: which candidates get executed conflates the system's own signal
quality with the operator's judgment, availability, and mood, which
undermines using the resulting trades to judge the system on its own
merits. New env var AUTO_EXECUTE_ENABLED (off by default, same
explicit-opt-in pattern as ENABLE_SCHEDULER) makes execution
mechanical instead: when set, _auto_execute_candidate() walks every
directional candidate (enter_long/enter_short) through the exact same
evaluate_risk_gate -> plan_execution -> size_position ->
open_trade_from_candidate pipeline the manual endpoints call, inside
the webhook's background task, immediately after the candidate is
created — no duplicated logic, so every existing guardrail (position
limits, drawdown/daily-loss room, write-once candidate locking, the
atomic account-wide open-position check in
storage.open_trade_if_room()) applies identically whether a human or
this function is driving it. The user chose the most complete,
highest-cost policy available — auto-execute every qualifying
candidate rather than a sampled subset — over staying manual,
accepting the added ongoing Execution LLM cost that comes with firing
on every candidate instead of only the ones a human would have
clicked through. Never raises: a Risk/Execution failure inside it must
not affect the candidate/decision work already saved by the same
background task. As with every tier before it, COORDINATOR_THRESHOLD
and the Coordinator's own scoring were not touched.

Tier 3.10 (ATR-barrier benchmark): the same external review's other
unaddressed item — every accuracy figure through Tier 3.9 (this
module's outcomes.py-backed endpoints, replay.py's threshold sweep)
uses the "price higher/lower than the decision price N minutes later"
proxy. That's cheap and has driven every real finding so far, but it
was never a trade simulation — no entry/stop/target geometry, no
slippage, no commission, no notion of "the stop got hit before the
target did," which the review flagged as necessary before any
"Analysis beats/doesn't beat a simple baseline" comparison can be
trusted. New app/backtest.py runs the exact same fill/stop/target/
slippage/commission conventions app/paper_trades.process_new_bar()
uses for real trades, offline, against bars already in storage, for a
hypothetical trade that's never actually taken — nothing written to
any trade table. Entry is a market fill at the next bar's open after
a candidate's anchor bar; stop/target are the anchor bar's own
already-stored ATR (no lookahead) times a stop/target multiple — NOT
Execution's proposed geometry, since this benchmarks the directional
SIGNAL rather than re-litigating what Execution would have picked.
New GET /candidates/history/backtest-lite, built on
compute_backtest_comparison(), runs several direction sources
(Analysis's own opinion, the blended Coordinator decision, trivial
always-bullish/always-bearish/VWAP-side baselines, and Analysis's
calls inverted as a pure diagnostic) through the identical barrier
mechanics side by side, with non-overlapping-by-default sampling
(mirroring the real MAX_OPEN_POSITIONS=1 constraint instead of
counting correlated back-to-back candidates as independent evidence)
— the actual apples-to-apples "does Analysis have a testable edge
over cheap deterministic baselines" comparison the horizon-price proxy
alone couldn't answer honestly. Entirely offline, no LLM calls, no new
data collection, no trading-logic change — COORDINATOR_THRESHOLD and
the Coordinator's own scoring remain untouched, same as every
diagnostic tier before it.

Tier 3.11 (champion/challenger, out-of-sample): Tier 3.10's first real
run against production found inverse_analysis (Analysis's calls
flipped) as the only source with profit_factor > 1 — exactly the kind
of finding the external review warned about, since it was found on
the same historical sample it would be used to justify a change
against. New app/backtest.split_candidates_chronologically() +
compute_champion_challenger_report(), exposed at GET /candidates/
history/backtest-lite/champion-challenger, hold out the most RECENT
slice of candidate history (never a random split — regimes are
time-correlated) and run every requested source through the identical
backtest-lite barrier mechanics on BOTH the calibration window and the
held-out validation window separately, so a challenger's apparent edge
can be checked against data it was never fitted to before it's treated
as real. Reports both windows side by side; never picks a winner or
flips anything automatically — same standing rule as every diagnostic
tier before it, any real trading-logic change needs the user's
explicit direction. Entirely offline, no LLM calls, no new data
collection. COORDINATOR_THRESHOLD and Coordinator scoring untouched.

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
from app.backtest import (
    ATR_STOP_MULT,
    ATR_TARGET_MULT,
    DEFAULT_HOLDOUT_FRACTION,
    DIRECTION_SOURCES,
    EXPIRY_BARS,
    compute_backtest_comparison,
    compute_champion_challenger_report,
)
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
    compute_agent_opinion_detail,
    compute_baseline_comparison,
    compute_outcome_for_candidate,
    compute_outcomes_for_decision,
    compute_per_agent_accuracy,
    summarize_opinions_by_day,
    summarize_outcomes,
)
from app.paper_trades import get_account_open_trade_count, open_trade_from_candidate, process_new_bar
from app.replay import replay_candidate, replay_candidates_for_symbol, summarize_replay, sweep_thresholds
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

# Tier 3.9 (auto-execution): off by default, same "explicit opt-in env
# var, following the ENABLE_SCHEDULER precedent" pattern used for the
# in-process scheduler — this must not silently start opening paper
# trades just because this code shipped. See _auto_execute_candidate
# below for the full reasoning (removing selection bias from paper-
# trade data collection, per the 2026-08-14 external review and the
# user's explicit choice to auto-execute every qualifying candidate
# rather than a sampled subset).
AUTO_EXECUTE_ENABLED = os.environ.get("AUTO_EXECUTE_ENABLED", "false").lower() == "true"


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
        # Tier 3.9: surfaced here for the same reason scheduler_enabled
        # is — so it's visible from a single read-only call whether the
        # system is currently allowed to auto-open real (paper) trades
        # without a human click, not something you have to infer.
        "auto_execute_enabled": AUTO_EXECUTE_ENABLED,
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
            # Tier 3.9: opt-in, off by default (AUTO_EXECUTE_ENABLED).
            # Runs in the same try block, after the candidate is
            # already saved — a failure here must never affect the
            # candidate/decision work above, which is why
            # _auto_execute_candidate has its own internal try/except
            # and never raises.
            if AUTO_EXECUTE_ENABLED:
                _auto_execute_candidate(candidate)
        except Exception as e:  # noqa: BLE001 - background task, log and move on
            logging.getLogger("webhook").error("auto-candidate failed: %s", e)
    except AnalysisAgentError as e:
        logging.getLogger("webhook").error("auto-analysis failed: %s", e)


def _auto_execute_candidate(candidate: dict) -> None:
    """Tier 3.9 (auto-execution / removing selection bias from paper-
    trade data collection). When AUTO_EXECUTE_ENABLED is set, every
    directional candidate (enter_long/enter_short — i.e. one that
    already cleared COORDINATOR_THRESHOLD) is walked through the exact
    same Risk-gate -> Execution -> Risk-size pipeline the dashboard's
    manual buttons drive via /agents/risk/evaluate and
    /agents/execution/plan, by calling the SAME underlying functions
    (evaluate_risk_gate, plan_execution, size_position,
    open_trade_from_candidate) directly rather than duplicating any of
    that logic. Every existing guardrail therefore applies identically,
    automatically, without a human click: Tier 2.2's gate/size staging,
    Tier 2.10's live drawdown/daily-loss room, Tier 3.1's write-once
    candidate locking, and Tier 3.3's atomic account-wide position
    limit (the real enforcement is still open_trade_from_candidate's
    call into storage.open_trade_if_room() — this function does not
    duplicate that check, it just reaches the same call).

    Why this exists: a human manually choosing which candidates to
    execute as real trades conflates the system's own signal quality
    with the operator's judgment, availability, and timing of presence
    — the exact selection-bias critique raised in the 2026-08-14
    external review. Auto-execution makes every trade a mechanical,
    pre-registered, reproducible decision instead. The user was asked
    to choose between a conservative (still-manual), a sampled, and a
    fully-automatic policy, and explicitly chose the fully-automatic
    one: execute every qualifying candidate, accepting the added
    ongoing Execution LLM cost that comes with it.

    Never raises — this runs inside the webhook's background task,
    after the candidate/decision it's acting on has already been
    saved, and a Risk/Execution failure here must not be able to
    affect anything already committed."""
    candidate_id = candidate["candidate_id"]
    symbol, timeframe = candidate["symbol"], candidate["timeframe"]
    decision = candidate.get("decision") or {}

    if decision.get("decision") not in ("enter_long", "enter_short"):
        return

    try:
        if get_committed_trade(candidate_id) is not None:
            return  # already committed somehow (e.g. a manual click won a race) — leave it alone

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        closed_trades = get_all_closed_trades_chronological()
        current_drawdown_used = compute_current_drawdown_used(trades=closed_trades)
        daily_loss_used = compute_daily_loss_used(now_iso, trades=closed_trades)
        open_positions = get_account_open_trade_count()

        gate_opinion = evaluate_risk_gate(
            symbol=symbol,
            timeframe=timeframe,
            coordinator_decision=decision,
            current_open_positions=open_positions,
            current_drawdown_used=current_drawdown_used,
            daily_loss_used=daily_loss_used,
        )
        try:
            record_risk_result(candidate_id, gate_opinion.to_dict())
        except CandidateLockedError:
            return
        save_opinion(
            agent="risk",
            symbol=symbol,
            timeframe=timeframe,
            timestamp=gate_opinion.timestamp,
            opinion=gate_opinion.to_dict(),
        )

        if gate_opinion.decision != "pending_execution":
            # "reject" (position limit / daily loss / drawdown) or
            # "no_action" — nothing further to do, same as the manual
            # dashboard flow would stop here too.
            return

        analysis_opinion = decision.get("opinions_used", {}).get("analysis")
        key_levels = (analysis_opinion or {}).get("key_data", {}).get("key_levels")
        try:
            execution_opinion = plan_execution(
                symbol=symbol,
                timeframe=timeframe,
                coordinator_decision=decision,
                latest_bar=candidate["bar"],
                analysis_key_levels=key_levels,
            )
        except ExecutionAgentError as e:
            logging.getLogger("webhook").error(
                "auto-execute: execution planning failed for candidate_id=%s: %s", candidate_id, e
            )
            return
        try:
            record_execution_result(candidate_id, execution_opinion.to_dict())
        except CandidateLockedError:
            return
        save_opinion(
            agent="execution",
            symbol=symbol,
            timeframe=timeframe,
            timestamp=execution_opinion.timestamp,
            opinion=execution_opinion.to_dict(),
        )

        if execution_opinion.status != "planned":
            # no_action / error / invalid — Execution declined or its
            # proposed geometry failed validation. Nothing to size.
            return

        # Re-check drawdown/daily-loss room — the Execution LLM call
        # above took real time, during which another trade could have
        # closed and changed either figure. Same double-check pattern
        # /agents/risk/evaluate's size stage already uses.
        closed_trades = get_all_closed_trades_chronological()
        current_drawdown_used = compute_current_drawdown_used(trades=closed_trades)
        daily_loss_used = compute_daily_loss_used(now_iso, trades=closed_trades)
        size_opinion = size_position(
            symbol=symbol,
            timeframe=timeframe,
            entry_price=execution_opinion.entry_price,
            stop_loss=execution_opinion.stop_loss,
            current_drawdown_used=current_drawdown_used,
            daily_loss_used=daily_loss_used,
        )
        try:
            record_risk_result(candidate_id, size_opinion.to_dict())
        except CandidateLockedError:
            return
        save_opinion(
            agent="risk",
            symbol=symbol,
            timeframe=timeframe,
            timestamp=size_opinion.timestamp,
            opinion=size_opinion.to_dict(),
        )

        if size_opinion.decision in ("approve", "modify"):
            refreshed = get_candidate_by_id(candidate_id)
            if refreshed is not None:
                open_trade_from_candidate(refreshed)
    except Exception as e:  # noqa: BLE001 - background task, log and move on
        logging.getLogger("webhook").error("auto-execute failed for candidate_id=%s: %s", candidate_id, e)


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


@app.get("/candidates/history/outcomes/by-agent")
def candidates_history_outcomes_by_agent(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    limit: int = Query(default=100, le=500),
    horizons: str = Query(default="15,30,60"),
) -> dict:
    """Tier 3.5 (per-agent signal quality): a different cut of the same
    candidate history — instead of the Coordinator's blended decision,
    scores each individual agent's (analysis/news/macro) own
    directional call in isolation, regardless of what the blended
    decision ended up being. Built to answer a question a
    COORDINATOR_THRESHOLD sweep can't: if overall accuracy is poor, is
    any individual agent actually beating chance on its own, or is
    there no signal anywhere to weight toward? Entirely offline, same
    hypothetical horizon estimate the rest of this file already uses —
    not a real backtest.

    Tier 3.6: News/Macro opinions are reused across every candidate
    that falls within their freshness window (NEWS_MACRO_MAX_AGE_MINUTES),
    so scoring "one tally per candidate" can badly inflate the apparent
    sample size behind an accuracy figure. Response now has two
    sibling sections instead of one flat {agent: {...}} object:
    "by_candidate" (the original Tier 3.5 view) and
    "by_distinct_opinion" (each unique agent opinion scored exactly
    once, regardless of reuse) — plus "distinct_opinion_counts" so a
    caller can see how much duplication a "by_candidate" number rests
    on. See app/outcomes.compute_per_agent_accuracy()'s docstring."""
    try:
        horizon_list = [int(h.strip()) for h in horizons.split(",") if h.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail="horizons must be comma-separated integers")

    candidates = get_candidate_history(symbol=symbol, timeframe=timeframe, limit=limit)
    return compute_per_agent_accuracy(candidates, horizons=horizon_list)


@app.get("/candidates/history/outcomes/by-agent/detail")
def candidates_history_outcomes_by_agent_detail(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    agent: str = Query(...),
    limit: int = Query(default=100, le=500),
    horizons: str = Query(default="15,30,60"),
    by_day: bool = Query(default=False),
) -> dict:
    """Tier 3.7 (per-opinion diagnostic detail): the by-agent endpoint
    above answers WHETHER an agent shows signal; this answers WHY, once
    there's an answer worth explaining. Built after production data
    showed Analysis (78 distinct opinions, easily the best-populated
    of the three agents) sitting at ~30% accuracy across every default
    horizon — a real, sample-backed underperformance, not a
    duplication artifact. Explaining that needs to see whether the
    wrong calls cluster around something the aggregate can't show:
    low self-reported confidence, a flag the agent already raises
    about its own read ("choppy", "conflicting_signals", "low_data"),
    or a particular time window.

    Returns one record per DISTINCT opinion for `agent` (same dedup
    key as the by_distinct_opinion section above, sorted oldest
    first): opinion_timestamp, direction, confidence, flags,
    reused_by_candidate_count (how many candidates in this window
    reused this exact opinion — the per-opinion version of
    distinct_opinion_counts, useful for spotting whether a low-N
    agent's headline number rests on one dominant call), and
    outcome_by_horizon ({15: "correct", ...}). Deliberately excludes
    each opinion's free-text reasoning/key_data to keep the payload
    compact and reliable for large windows — see
    app/outcomes.compute_agent_opinion_detail()'s docstring for why.
    400 if `agent` isn't analysis/news/macro or `horizons` doesn't
    parse as comma-separated integers.

    Tier 3.8: `by_day=true` additionally groups these same records by
    calendar date (see app/outcomes.summarize_opinions_by_day()) — a
    single agent's opinions across one live trading day are correlated,
    not independent draws, so this makes day-level clustering visible
    directly instead of hiding it inside one aggregate number. Off by
    default to keep the existing flat response shape unchanged for
    Tier 3.7 callers."""
    try:
        horizon_list = [int(h.strip()) for h in horizons.split(",") if h.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail="horizons must be comma-separated integers")

    candidates = get_candidate_history(symbol=symbol, timeframe=timeframe, limit=limit)
    try:
        opinions = compute_agent_opinion_detail(candidates, agent=agent, horizons=horizon_list)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    result = {
        "symbol": symbol,
        "timeframe": timeframe,
        "agent": agent,
        "distinct_opinion_count": len(opinions),
        "opinions": opinions,
    }
    if by_day:
        result["by_day"] = summarize_opinions_by_day(opinions, horizons=horizon_list)
    return result


@app.get("/candidates/history/outcomes/baseline-comparison")
def candidates_history_outcomes_baseline_comparison(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    limit: int = Query(default=100, le=500),
    horizons: str = Query(default="15,30,60"),
) -> dict:
    """Tier 3.8 (base rate + trivial-baseline comparison): built after
    an external review of this project's full history correctly
    pointed out that Analysis's ~30% accuracy figure (by-agent/detail
    above) can't be judged against an assumed 50% coin-flip baseline —
    if the market moved mostly one direction during the measurement
    window, ANY fixed directional bias looks artificially good or bad
    purely as a function of which way the window happened to move,
    independent of real skill. Computes the market's own base rate
    (via "always guess bullish" / "always guess bearish" — their
    accuracy over a window literally IS that window's up-rate/
    down-rate) alongside two trivial, mostly-LLM-independent
    predictors (VWAP side, and Analysis's own calls inverted as a pure
    diagnostic — this project has NOT acted on that inversion and has
    no plan to without much more evidence) on the exact same candidate
    population and horizon machinery every other accuracy figure here
    uses. See app/outcomes.compute_baseline_comparison()'s docstring
    for the full breakdown of each field. Entirely offline, no LLM
    calls, no trade side effects. 400 if `horizons` doesn't parse as
    comma-separated integers."""
    try:
        horizon_list = [int(h.strip()) for h in horizons.split(",") if h.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail="horizons must be comma-separated integers")

    candidates = get_candidate_history(symbol=symbol, timeframe=timeframe, limit=limit)
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        **compute_baseline_comparison(candidates, horizons=horizon_list),
    }


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


@app.get("/candidates/history/replay/threshold-sweep")
def candidates_history_replay_threshold_sweep(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    thresholds: str = Query(..., description="comma-separated threshold values to test, e.g. 15,20,25,30,35,40"),
    limit: int = Query(default=100, le=500),
    weights: str = Query(default=None, description="held fixed across the whole sweep — omit to use the current live weights"),
    min_available_weight: float = Query(default=None, description="held fixed across the whole sweep — omit to use the current live value"),
    horizons: str = Query(default="15,30,60"),
) -> dict:
    """Tier 3.4 (COORDINATOR_THRESHOLD tuning): the tool this whole
    replay/outcome pair of features was ultimately built toward —
    "across a range of threshold values, how does directional decision
    volume and hypothetical horizon accuracy change?" Compact,
    pre-aggregated per-threshold summary (not a raw per-candidate
    list) so the answer is cheap to fetch and read at a glance. weights/
    min_available_weight stay fixed for the whole sweep — only
    threshold varies, so an accuracy shift can be attributed to the
    threshold alone. Same "hypothetical, not a real backtest" caveat
    as everywhere else this project uses the horizon estimate: a
    replayed decision under a hypothetical threshold was never
    actually traded, so there's no real P&L to attribute to it."""
    try:
        threshold_list = [float(t.strip()) for t in thresholds.split(",") if t.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail="thresholds must be comma-separated numbers")
    if not threshold_list:
        raise HTTPException(status_code=400, detail="thresholds must contain at least one value")

    return sweep_thresholds(
        symbol=symbol,
        timeframe=timeframe,
        thresholds=threshold_list,
        limit=limit,
        weights=_parse_replay_weights(weights),
        min_available_weight=min_available_weight,
        horizons=_parse_replay_horizons(horizons),
    )


@app.get("/candidates/history/backtest-lite")
def candidates_history_backtest_lite(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    limit: int = Query(default=200, le=1000),
    sources: str = Query(
        default=None,
        description="comma-separated direction sources to compare, e.g. analysis,coordinator,always_bullish,always_bearish,vwap,inverse_analysis — omit for all",
    ),
    atr_stop_mult: float = Query(default=ATR_STOP_MULT),
    atr_target_mult: float = Query(default=ATR_TARGET_MULT),
    expiry_bars: int = Query(default=EXPIRY_BARS, le=200),
    non_overlapping: bool = Query(default=True),
) -> dict:
    """Tier 3.10 (ATR-barrier benchmark): every accuracy figure this
    project has produced through Tier 3.9 uses the "price higher/lower
    N minutes later" proxy — never an entry/stop/target simulation, no
    slippage, no commission, no notion of "the stop got hit before the
    target." Built on app/backtest.compute_backtest_comparison(),
    which runs app/paper_trades.process_new_bar()'s exact fill/stop/
    target/slippage/commission conventions OFFLINE against bars
    already in storage, for a hypothetical trade that was never
    actually taken — same mechanics as a real paper trade, just not
    persisted anywhere. Entry/stop/target geometry is ATR-based
    (the anchor bar's own already-stored ATR, no lookahead), NOT
    Execution's LLM-proposed levels — this benchmarks the directional
    SIGNAL, not what Execution would have picked.

    `sources` lets several direction signals run through the identical
    barrier mechanics side by side for direct comparison: "analysis"
    (Analysis's own opinion), "coordinator" (the actual blended
    decision), "always_bullish"/"always_bearish"/"vwap" (trivial,
    LLM-independent baselines), and "inverse_analysis" (Analysis's
    calls flipped — diagnostic only, never acted on, matches Tier
    3.8's framing for the same baseline in the horizon-proxy endpoint).
    Same candidate population and ATR geometry held fixed across every
    source in one call, so a difference in results is attributable to
    the direction signal alone — this is the actual "does Analysis
    beat simple baselines" comparison the external review asked for,
    which the horizon-price proxy alone couldn't answer honestly.

    `non_overlapping` (default true) skips a candidate whose anchor
    falls before the previous simulated trade (for that source)
    resolved — avoids counting overlapping candidates from a fast
    timeframe as independent evidence, mirroring the real
    MAX_OPEN_POSITIONS=1 constraint instead of pretending unlimited
    concurrent hypothetical positions.

    Entirely offline: no LLM calls, no new data collection, nothing
    written to any trade table. COORDINATOR_THRESHOLD and the
    Coordinator's own scoring are untouched — this is read-only
    analysis, same as every diagnostic tier before it."""
    source_list = [s.strip() for s in sources.split(",") if s.strip()] if sources else None
    if source_list:
        unknown = [s for s in source_list if s not in DIRECTION_SOURCES]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"unknown source(s) {unknown} — must be one of {list(DIRECTION_SOURCES)}",
            )

    candidates = get_candidate_history(symbol=symbol, timeframe=timeframe, limit=limit)
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        **compute_backtest_comparison(
            candidates,
            sources=source_list,
            stop_mult=atr_stop_mult,
            target_mult=atr_target_mult,
            expiry_bars=expiry_bars,
            non_overlapping=non_overlapping,
        ),
    }


@app.get("/candidates/history/backtest-lite/champion-challenger")
def candidates_history_backtest_lite_champion_challenger(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    limit: int = Query(default=300, le=1000),
    champion: str = Query(default="coordinator"),
    challengers: str = Query(
        default=None,
        description="comma-separated challenger direction sources, e.g. analysis,inverse_analysis,always_bullish — omit for every source except the champion",
    ),
    holdout_fraction: float = Query(default=DEFAULT_HOLDOUT_FRACTION, gt=0.0, lt=1.0),
    atr_stop_mult: float = Query(default=ATR_STOP_MULT),
    atr_target_mult: float = Query(default=ATR_TARGET_MULT),
    expiry_bars: int = Query(default=EXPIRY_BARS, le=200),
    non_overlapping: bool = Query(default=True),
) -> dict:
    """Tier 3.11 (champion/challenger, out-of-sample): the backtest-
    lite endpoint above found inverse_analysis as the only source with
    profit_factor > 1 — exactly the kind of finding the external review
    warned about, since it was found on the same historical sample any
    change would be justified against. Built on
    app/backtest.compute_champion_challenger_report(): holds out the
    most RECENT `holdout_fraction` of candidate history as a validation
    window (never a random split — regimes are time-correlated, a
    random split would leak the future into calibration), and runs
    `champion` (the currently-live decision source, default
    "coordinator" — the real system) plus every requested `challenger`
    through the SAME backtest-lite barrier mechanics on BOTH the
    calibration window and the held-out validation window separately.

    Reads as: does a challenger's apparent edge on the calibration
    window still hold up on data it was never fitted to? A challenger
    that looks good on calibration but falls apart on validation is a
    materially different (weaker) result than one that holds up on
    both — this endpoint reports both windows side by side specifically
    so that distinction is visible, rather than collapsing it into a
    single number or an automatic pass/fail verdict (a rigid threshold
    would be its own kind of overfitting at this sample size).

    Purely a report: never picks a winner, never flips anything. Same
    standing rule as every diagnostic tier before it — any real
    trading-logic change needs the user's explicit direction.
    COORDINATOR_THRESHOLD and Coordinator scoring untouched. 400 if
    `champion`/`challengers` contains an unrecognized source, or if
    `holdout_fraction` is outside (0, 1)."""
    challenger_list = [c.strip() for c in challengers.split(",") if c.strip()] if challengers else None

    candidates = get_candidate_history(symbol=symbol, timeframe=timeframe, limit=limit)
    try:
        report = compute_champion_challenger_report(
            candidates,
            champion=champion,
            challengers=challenger_list,
            holdout_fraction=holdout_fraction,
            stop_mult=atr_stop_mult,
            target_mult=atr_target_mult,
            expiry_bars=expiry_bars,
            non_overlapping=non_overlapping,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"symbol": symbol, "timeframe": timeframe, **report}


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
    size stage approves or modifies, a paper ORDER is submitted right
    here as a side effect — that's the natural commit point: Risk
    deciding a real size IS the decision to actually take the trade
    (paper-only, so there's no reason to gate that behind a further
    manual step). As of Tier 3.2, this only ever creates a
    status="pending_fill" order, even for a market order — it fills
    against a real subsequent bar (see app/paper_trades.py), never
    instantly at candidate-creation time.

    Tier 3.3: that live count is now ACCOUNT-WIDE
    (get_account_open_trade_count(), every symbol/timeframe) instead of
    scoped to this one symbol+timeframe — MAX_OPEN_POSITIONS is a
    single account-wide budget. This gate-stage check stays advisory
    (Execution's LLM call happens after it, during which another
    candidate could commit); the real, atomic enforcement is
    open_trade_from_candidate()'s call into
    storage.open_trade_if_room() below.

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
        open_positions = get_account_open_trade_count()
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
