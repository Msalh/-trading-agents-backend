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

Tier 3.12 (backtest-lite methodology corrections): a second external
review of the Tier 3.10/3.11 results flagged three issues, addressed
here. First, Tier 3.10's backtest-lite endpoint compares sources as
independent POLICIES — each source applies the non-overlap schedule
against its own resolved directions, so different sources can end up
trading different candidate subsets; the endpoint's docstring
previously (inaccurately) described this as a "same candidate
population" comparison. New app/backtest.run_paired_barrier_backtest(),
exposed at GET /candidates/history/backtest-lite/paired, fixes this
properly: it keeps only candidates every requested source can resolve
a direction for, and uses ONE shared entry price plus ONE shared,
direction-independent non-overlap schedule, so any difference between
sources is attributable to the direction call alone. Second, a
calibration-window candidate near the validation boundary could have
its forward barrier walk read price bars that fall inside the
validation window, which is a boundary-leakage risk for calibration's
own numbers (it does not affect validation's numbers, since a
validation trade only ever looks forward from its own anchor).
split_candidates_chronologically() gained an optional `expiry_bars`
parameter that embargoes/purges any calibration candidate whose
forward window would cross into validation;
compute_champion_challenger_report() now uses this embargo and reports
the purged count via config.purged_at_boundary. Third,
simulate_barrier_trade()'s "expired" exit (mark-to-last-seen-close)
previously applied no slippage, inconsistent with every other exit
type in the same function — now fixed to apply the same
against-the-trader slippage as a stop-out. compute_champion_challenger_
report() also gained a base_rate section (calibration vs. validation)
reusing Tier 3.8's compute_baseline_comparison(), as a cheap regime
sanity-check alongside the existing per-source results. All three
fixes are purely methodological — no trading-logic change, no
COORDINATOR_THRESHOLD change, entirely offline.

Tier 3.13 (small-sample statistics): win_rate and profit_factor are
exactly the two statistics most volatile at the trade counts this
project actually produces — Tier 3.12's own paired endpoint returned
just 7 accepted trades on its first production run, where a 5/7 vs
2/7 split LOOKS like a large difference but is well within noise.
Every backtest-lite/champion-challenger/paired summary now also
reports win_rate_ci95_low/win_rate_ci95_high (a Wilson score interval
on wins/decided — the standard small-N correction, since the plain
normal-approximation interval misbehaves badly below roughly 30
trades), median_pnl_usd (a robustness check against one large win or
loss dominating the mean), and max_drawdown_usd (the deepest
peak-to-trough dip in the running equity curve, in the order trades
were actually taken). Purely additional read-only reporting on
results already computed — no new trades simulated, no trading-logic
change, COORDINATOR_THRESHOLD untouched.

Tier 3.14 (parameter sensitivity grid): every result reported through
Tier 3.13 used one specific geometry (1.5x ATR stop, 2.5x ATR target,
24-bar expiry) — a source that only looks good under that one choice
could be an artifact of the choice, not a real edge. New
app/backtest.run_sensitivity_grid(), exposed at GET /candidates/
history/backtest-lite/sensitivity-grid, runs the Tier 3.12 paired
comparison across a small, PRE-REGISTERED grid (default 3 stop
multiples x 3 target multiples x 3 expiry values = 27 combinations,
fixed via env vars BACKTEST_GRID_STOP_MULTS/BACKTEST_GRID_TARGET_MULTS/
BACKTEST_GRID_EXPIRY_BARS at deploy time — deliberately not a query
parameter, since a caller-chosen grid would defeat the point of
pre-registering it before looking at results). Reports a compact
per-combination result per source plus a robustness summary (how many
combinations were net positive / had profit_factor > 1, the range of
total_pnl_usd across the grid) — a real edge should hold up across
most reasonable geometries, not just the one tested first. Entirely
offline, no LLM calls, no new data collection. COORDINATOR_THRESHOLD
untouched.

Tier 3.15 (LLM call cost/usage telemetry): three external review
cycles in a row named the same gap — this project had no visibility
into what its own LLM calls actually cost, and it kept getting
deferred in favor of whatever looked more urgent at the time. New
app/llm_telemetry.track_llm_call() wraps every client.messages.create()
call site in Analysis/News/Macro/Execution and logs exactly one row
per call, success or failure, to the new llm_call_log table (agent,
model, a short trigger_context, latency, input/output/cache token
counts, web_search call count, and an estimated USD cost — pricing
constants are env-configurable since this project has no Anthropic
Console billing access to verify actual charges). New GET /system/
llm-usage reports aggregated totals per agent plus a small recent-
calls tail. A telemetry write failure is swallowed, never allowed to
break or mask the actual agent call it's observing. Purely additive
and read-only from the trading system's perspective — no agent's
behavior, prompt, or output changes. COORDINATOR_THRESHOLD untouched.

Tier 3.16 (Coordinator/Analysis divergence + ablation): the third
external review's second priority item, after Tier 3.15's cost
telemetry. Every backtest-lite/paired/grid result since Tier 3.10 has
shown Coordinator performing at or below Analysis alone, but a plain
"do the two directions match?" check conflates several genuinely
different situations into one number and can't say whether
Coordinator's blending of News/Macro/Timing on top of Analysis ever
actually changes an outcome. New app/coordinator_diagnostics.py and
GET /candidates/history/coordinator-divergence answer this with a
five-way named breakdown (same direction, opposite direction,
Coordinator no_trade while Analysis was directional, Coordinator
insufficient_data, Analysis neutral while Coordinator was directional
anyway), News/Macro presence-and-opposition impact stats, a Timing-
block count, and a real causal ablation per directional agent — reuse
app/replay.py's existing replay_candidate() to re-score every
candidate's frozen opinions snapshot with one agent's weight zeroed
and report how often the final decision actually changes, not just
how often that agent happened to agree with Analysis. Entirely
offline, read-only, no LLM calls, no new candidates or trades — a
zeroed weight only ever applies to a single replay pass, never to the
live WEIGHTS config. COORDINATOR_THRESHOLD untouched.

Tier 3.17 (ablation methodology correction): pulling Tier 3.16's real
numbers from production surfaced a genuine bug in its own ablation
mechanism. Modeling "remove agent X" by zeroing X's weight in the
WEIGHTS dict also shrinks directional_weight_total — the availability
gate's (MIN_AVAILABLE_WEIGHT) denominator — for every candidate being
replayed, including ones where X was never present at all. On the
197-candidate production history, this falsely flipped the same 36
"Analysis alone present" candidates out of insufficient_data under
BOTH the News-ablation and Macro-ablation passes, with identical
transition splits — a renormalization artifact, not a real finding
about either agent's influence. Fixed by having app/coordinator_
diagnostics._ablate_agent() remove the agent's actual opinion from
the frozen snapshot (added to missing_agents) instead of zeroing a
weight, keeping directional_weight_total at its normal live value so
a candidate where the agent was never present is now correctly a
no-op. Each ablation entry also now reports agent_present_count, a
built-in sanity bound (decision_changed can never exceed it). Purely
a bugfix to Tier 3.16's own diagnostic — no scoring, weights, or
threshold used by real decisions changed.

Tier 3.18 (day/session reporting): the third external review's item
5 — day/session trade counts should be a primary reported metric
everywhere, not buried behind a raw candidate count that can look
like a decent sample while spanning very few genuinely independent
trading days. New app/backtest.compute_day_session_breakdown() reports
distinct trading days (using each bar's own Pine-computed trading_date
field, the CME/Globex-aware value from Tier 2.9 — not a naive UTC
split), candidates-per-day min/median/max, and a session breakdown
(the bar's own coarse RTH/OVERNIGHT session_name plus Timing's finer
London/NY/NY-PM/overlap/outside-sessions/weekend/holiday
classification). Wired into every existing backtest-lite/paired/grid/
champion-challenger report's top level (champion-challenger reports it
separately per calibration/validation window, since out-of-sample
validity depends directly on how many independent days each window
spans) — the "not buried" part of the review's complaint — plus a new
standalone GET /candidates/history/day-session-report for a quick
check before running anything heavier. Purely descriptive, read-only
reporting on candidates already fetched: no new data collection, no
change to which trades are simulated or how, COORDINATOR_THRESHOLD
untouched.

Tier 3.19 (trading-date integrity, fourth external review, 2026-08-18):
Tier 3.18's distinct_trading_days stayed at 4 in production even after
candidates_considered grew by 43 over a window spanning a genuine
trading weekday. The review pointed out that compute_day_session_
breakdown() trusts a bar's payload trading_date field at face value —
unknown_trading_date_count==0 only means a value was PRESENT, not that
it's correct, and Tier 2.9's own mismatch check (check_trading_date(),
run at webhook ingestion) was never persisted or aggregated anywhere,
only returned/logged per-event as calendar_warning. New app/backtest.
compute_trading_date_integrity_report() cross-checks, per candidate,
three independent views of its trading day: the literal payload value,
a freshly recomputed one (same convention check_trading_date() already
applies), and a third, fully independent plain-UTC-calendar-date split
with no NY-timezone/rollover logic at all. Reports per-view distinct-
date counts, the total mismatch count (uncapped) plus a capped list of
concrete mismatch examples (candidate_id/event_id/timestamp/both
dates), and the candidate set's earliest/latest anchor timestamp. New
standalone GET /candidates/history/trading-date-integrity — kept
separate from day-session-report since this is a forensic/validation
tool, not a summary metric. Entirely offline/read-only: no new data,
no scoring change, COORDINATOR_THRESHOLD untouched.

Tier 3.20 (experiment registry, fourth external review, 2026-08-18):
every finding this project has produced has been retrospective — run a
report against whatever candidates already exist. The scheduled weekly
check watching for a 15-distinct-day threshold (Tier 3.18) is watching
the SAME growing pool every diagnostic tier keeps mining for ideas —
by the time that threshold fires, none of its candidates will be a
clean holdout in any normal sense, even with scoring untouched the
whole time. New app/experiments.py adds a lightweight, append-only
pre-registration mechanism: register_experiment() freezes a hypothesis
statement, target_metrics, a stopping_rule (min_distinct_trading_days
and/or min_accepted_trades), and a snapshot of the live coordinator_
threshold/weights/min_available_weight, and marks registered_at as a
hard boundary — only candidates created AT OR AFTER that moment ever
count toward the experiment. evaluate_stopping_rule() is read-only and
callable any number of times to watch progress. resolve_experiment()
is the one-time action: refuses early, and once resolved returns the
SAME resolution forever, never recomputed — reuses app.backtest.
compute_backtest_comparison/compute_day_session_breakdown rather than
any new scoring logic. New POST /experiments (register, secret-
protected), GET /experiments (list, append-only history), GET
/experiments/{id} (detail plus a live, non-consuming stopping-rule
check), POST /experiments/{id}/resolve (secret-protected, one-time).
New `experiments` table. Entirely additive: no existing endpoint's
behavior changes, COORDINATOR_THRESHOLD/WEIGHTS only read and
snapshotted, never modified, no LLM calls.

Tier 3.21 (ablation reclassification, fourth external review,
2026-08-18): Tier 3.17's fixed raw ablation percentages (82%/47%/21%
for analysis/news/macro) still conflate a "quorum" effect — removing
an agent alone dropping available evidence below MIN_AVAILABLE_WEIGHT,
which says nothing about whether that agent's DIRECTION mattered —
with a genuine directional-influence effect. app/coordinator_
diagnostics._classify_ablation_change() now splits every ablation-
caused change into to_insufficient_data / direction_flipped /
threshold_crossing, and each ablation entry additionally reports
conflict_flags_changed_count and avg_abs_score_delta_when_changed/
_when_unchanged. transitions (raw {original}->{replayed} pairs) is
unchanged since Tier 3.16 — this is additive detail, not a
redefinition. Notable finding surfaced while building this: under the
LIVE weights/threshold/min_available_weight, direction_flipped turns
out to be mathematically unreachable for any single agent's ablation
— removing one agent's raw contribution alone is never enough to both
let the original cross +threshold and flip the post-ablation score
past -threshold, worked through the renormalized-denominator algebra
for each of the three agents. Read-only, offline, no LLM calls,
COORDINATOR_THRESHOLD/WEIGHTS untouched.

Tier 3.22 (trade provenance, fifth external review, 2026-08-19): the
2026-08-18 manual dashboard pipeline test (opened via /agents/risk/
evaluate's "Run" buttons) produced a real closed paper trade that was
indistinguishable, in every report, from genuine autonomous execution
— the review flagged this as data contamination requiring an
immediate fix, not something to defer. app/paper_trades.
open_trade_from_candidate() now takes a REQUIRED `provenance` argument
("auto_policy" for the AUTO_EXECUTE_ENABLED-gated background task,
"manual_dashboard" for this manual endpoint) — no default, so a future
call site can't silently omit it. New `provenance` column on
paper_trades (migrated + backfilled: any pre-Tier-3.22 row can only
have come from the manual endpoint, since AUTO_EXECUTE_ENABLED has
been false for this project's entire history to date). GET
/trades/history gained an opt-in `exclude_provenance` filter (default
unchanged — full history). GET /account/risk gained
`closed_trades_by_provenance` for visibility. Deliberately NOT
changed: `current_drawdown_used`/`daily_loss_used` still count every
closed trade regardless of provenance — whether a manual paper trade
should consume real risk-budget capacity is an open design question,
flagged to the user rather than decided here.

Tier 3.23 (experiment registry hardening, fifth external review,
2026-08-19): the fifth review found six concrete gaps in Tier 3.20's
experiment registry — praised the pre-registration IDEA, flagged the
EXECUTION as incomplete. locked_config was recorded but never actually
enforced: (a) evaluate_stopping_rule()/resolve_experiment() now
re-score every prospective candidate via app.replay.replay_candidate()
under the experiment's frozen weights/threshold/min_available_weight
before running any backtest, instead of trusting each candidate's own
stored decision (which may have been computed under a since-changed
live config). (b) Backtest geometry (ATR stop/target mult, expiry_bars,
non_overlapping) is now locked and threaded through as real parameters;
slippage/commission/backtest-logic-version aren't parametrizable in
app.backtest yet, so those are drift-CHECKED instead — a loud
`geometry_drift` field appears whenever live no longer matches locked,
rather than silently blending the two. (c) target_metrics is now a
structured, validated commitment (primary_metric/comparator/
success_threshold/secondary_metrics) — resolve_experiment() computes
and reports whether the primary metric actually met its pre-registered
bar (`target_metrics_result`), not just that some numbers were
recorded. (d) The no-peeking boundary is now
registered_watermark_rowid (a monotonic integer captured at
registration) instead of a second-precision registered_at string
comparison. (e) The prospective-candidate query is now unbounded
(fetches every candidate past the watermark, oldest first) instead of
a "newest 2000" query that could silently drop the OLDEST prospective
candidates once a long-running experiment's window grew past that
limit — EXPERIMENT_MAX_PROSPECTIVE_CANDIDATES now raises loudly
instead of truncating silently. (f) Documented honestly: this is a
prospective experiment registry with one-time aggregate resolution,
not yet a full append-only shadow evaluation engine (no per-candidate
outcome ledger) — a real step toward that, not the same thing under a
bigger name. See app/experiments.py's module docstring for full detail.

Tier 3.24 (analysis_required explicit gate, project-owner design
decision, 2026-08-19): the fifth review's own open question — is
Analysis being "load-bearing" (Tier 3.21 proved algebraically that its
absence always fails the live quorum check on its own) an intentional
design choice or just an accident of tunable weights? — was explicitly
NOT something data could answer, and was left to the project owner.
The decision: make it explicit. coordinator.ANALYSIS_REQUIRED (default
True) is a new gate in _score_opinions(), checked BEFORE the quorum
math and independent of it — "no directional decision without a
current (non-missing, non-stale) Analysis opinion," scoped to
Analysis's mere presence, not its direction (a present-but-neutral
Analysis opinion still passes, matching today's behavior exactly, and
matching the project owner's own explicit choice of this narrower
scope over a broader "must be directional" gate). Changes NO decision
computed today, live or replayed — it hardens an already-true
guarantee against being silently broken by a future weights/
min_available_weight retune, rather than changing anything now.
Threaded everywhere weights/threshold/min_available_weight already are:
CoordinatorDecision.config_version, app.replay's replay_candidate()/
replay_candidates_for_symbol()/sweep_thresholds() (a fourth optional
override, "None means use the live value"), the /candidates/*/replay*
endpoints below, and app.experiments._current_locked_config() (a fifth
locked, ENFORCED scoring knob — pre-Tier-3.24 experiments default to
True on read, the only value it has ever actually had live).

Tier 3.25 (cost telemetry health, fifth external review item #5,
2026-08-19): app.llm_telemetry's write to llm_call_log has always
swallowed a failure on purpose (a logging problem must never break a
real agent call) — but that also made a telemetry outage invisible.
Three additive fixes, no agent behavior changed: (1) in-process
attempted/written/failed counters plus TELEMETRY_STARTED_AT (process
start), exposed via GET /system/llm-usage's new `telemetry_health`
field — a degraded write rate is now visible instead of silently
under-counting calls; (2) PRICING_VERSION, a hand-maintained marker
(mirrors app.backtest.BACKTEST_LOGIC_VERSION) stamped onto every
llm_call_log row as `pricing_version` and surfaced back as
`pricing_versions_present` in the summary, so a future change to the
five TELEMETRY_*_COST_PER_MTOK/*_MULTIPLIER constants is visible in
the data rather than silently blending two pricing regimes into one
cost total; (3) pre-migration rows backfilled to pricing_version="1"
(a real fact — these constants haven't changed since Tier 3.15, not a
guess). See app/llm_telemetry.py's module docstring for full detail.

Tier 3.26 (News/Macro threshold-crossing deep dive, fifth external
review item #6, 2026-08-19): Tier 3.21's ablation.*.decision_changed_
by_category.threshold_crossing count said HOW OFTEN removing an
agent's opinion crossed the enter/no_trade line without a quorum or
direction-reversal effect involved, but nothing about whether that
crossing was good or bad for the strategy. New app/coordinator_
diagnostics.compute_threshold_crossing_deep_dive() and GET
/candidates/history/threshold-crossing-deep-dive re-walk one agent's
threshold_crossing subset and add: which side each case is on
(agent_enabled_trade — its presence is why a real trade was taken —
vs. agent_prevented_trade, the reverse); the real outcome for
agent_enabled_trade cases (actual closed-trade P&L when one exists,
same hypothetical fallback as elsewhere otherwise) or the replayed
decision's hypothetical outcome — relabeled prevented_win/prevented_
loss — for agent_prevented_trade cases, since those never became a
real trade; whether the agent's own direction agreed or opposed
Analysis's on that candidate; the agent's own self-reported flags
(News and Macro use different vocabularies — not unified, and
urgent_flag_count is always 0 for macro by construction, since only
News's prompt defines "urgent"); and a distinct-opinion-timestamp
count so a small case count from a slow-cadence agent isn't mistaken
for that many independent LLM calls. Entirely offline for the
ablation/replay step (no LLM calls, COORDINATOR_THRESHOLD/WEIGHTS
untouched, no candidate mutated); the agent_enabled_trade real-outcome
lookup reads trade rows the same way every other outcome-aware
endpoint in this project already does.

Tier 3.27 (News urgent-vs-directional decomposition, sixth external
review, 2026-08-23): real Tier 3.26 production numbers (News: 107
threshold_crossing cases, ~80% carrying News's "urgent" flag) surfaced
a real measurement gap — "urgent" independently halves the blended
score in app/coordinator.py's own scoring math regardless of direction
or agreement with Analysis, and Tier 3.26's ablation removes News's
opinion entirely, conflating that dampen with News's genuine
directional contribution into one "changed" number. New app/
coordinator_diagnostics.compute_news_urgent_analysis() and GET
/candidates/history/news-urgent-decomposition, without touching live
scoring at all: `prevalence` reports urgent's honest unconditional rate
(candidate-level and distinct-opinion-level) instead of the rate within
the pre-filtered threshold_crossing sample; `decomposition` replays two
partial-modification variants per urgent-tagged threshold_crossing case
(direction forced to neutral with urgent's flag kept, vs. urgent
stripped with the real direction/confidence kept) and attributes each
case to direction_alone / urgent_dampen_alone / both_independently_
sufficient / only_combination_sufficient. Scoped to News only — Macro's
flag vocabulary has no "urgent" concept. Entirely offline, no LLM
calls, no candidate mutated, COORDINATOR_THRESHOLD/WEIGHTS/
ANALYSIS_REQUIRED untouched.

Tier 3.28 (News urgent vs. deterministic economic-calendar blackout,
sixth external review, ranked backlog item #2, 2026-08-24): the
reviewer's exact ask — compare News's "urgent" flag against a simple
blackout built on a trustworthy economic calendar (abstain before/after
CPI/FOMC/NFP); if News doesn't outperform a fixed blackout, there is no
reason to pay its cost or trust its free-text judgment. New app/
economic_calendar.py is a hardcoded, source-cited registry of every
real 2026 CPI/NFP/FOMC release timestamp (BLS + White House PFEI
schedule for CPI/NFP, the Federal Reserve's own calendar for FOMC —
full citations and DST handling in that module's docstring) with a
deterministic is_within_blackout_window() check that has zero access to
News's opinion or reasoning. New app/coordinator_diagnostics.
compute_news_urgent_vs_calendar_blackout() and GET /candidates/history/
news-urgent-vs-calendar-blackout tag every News-present candidate with
both signals independently and cross-tabulate them, with outcomes
(real trade result preferred, hypothetical fallback otherwise) bucketed
per quadrant. Built and cross-checked against live production data
before shipping: the current 9-trading-day window (2026-08-12 through
2026-08-24) contains exactly ONE registry event (the 2026-08-12 CPI
release, the very first day of that window) — no FOMC meeting fell in
August 2026, and the nearest NFP release (2026-08-07) predates the
window. calendar_coverage reports this honestly on every call rather
than letting a thin sample read as a confident result; the comparison
gains real statistical power automatically as the data window reaches
2026-09-04 (NFP), 2026-09-11 (CPI), and 2026-09-15/16 (FOMC), already
in the registry. Entirely offline, no LLM calls, no candidate mutated,
COORDINATOR_THRESHOLD/WEIGHTS untouched.

Tier 3.29 (opinion-level, day-blocked re-aggregation, sixth external
review, ranked backlog item #3, 2026-08-24): Tiers 3.26/3.27/3.28 each
report one clean categorical split (side / attribution / quadrant)
pooled across every CANDIDATE in their subset — which conflates how
many genuinely INDEPENDENT LLM opinions actually drove the split
(News/Macro's slow cadence means one opinion is often reused across
many consecutive candidates while fresh) with whether the split holds
across many TRADING DAYS or is one volatile day dominating the pool.
New app.coordinator_diagnostics._opinion_level_day_blocked_summary(),
a single shared aggregator wired into all three existing endpoints
(threshold-crossing-deep-dive, news-urgent-decomposition, news-urgent-
vs-calendar-blackout) as a new additive `opinion_level_day_blocked` key
— reports each day's own candidate-level AND opinion-weighted counts
(a reused opinion's weight always sums to exactly 1 for that day, split
fractionally across whichever categories its candidates actually
landed in), plus the same pooled-but-opinion-weighted totals alongside
the existing raw candidate-level totals for direct comparison. No new
endpoints, no change to any existing field's meaning — every prior
consumer of these three endpoints' response shape keeps working
unmodified. Entirely offline, pure post-processing over each
diagnostic's own already-computed cases list, no new replays, no LLM
calls, no mutation of anything stored.

Tier 3.30 ("analysis_risk_filtered" shadow policy, sixth external
review, ranked backlog item #4, 2026-08-24): the reviewer's last ranked
item — a parallel policy where Analysis alone decides direction and
News/Macro act only as risk filters, run alongside the live system
without touching it. New app.backtest.DIRECTION_SOURCES entry
"analysis_risk_filtered": same direction call as the existing
"analysis" source, but the candidate is skipped entirely if News's
opinion carries the "urgent" flag or Macro's opinion carries the
"risk_off" flag (project-owner-confirmed veto scope, out of each
agent's full flag vocabulary) — News/Macro can only veto a trade
Analysis wanted to take, never supply or shift its direction. Because
DIRECTION_SOURCES is consumed generically throughout app/backtest.py,
every existing backtest-lite/paired/grid/champion-challenger endpoint
picks this up automatically with the full existing win_rate/profit_
factor/CI95/median_pnl/max_drawdown/day-session reporting machinery
applied for free — no new endpoint, no new simulation logic. Same
pattern as every diagnostic tier before it: entirely offline, re-walks
already-stored candidate history, no LLM calls, no live-running
process, COORDINATOR_THRESHOLD/WEIGHTS/AUTO_EXECUTE_ENABLED untouched.
This closes out the sixth external review's full ranked backlog
(items #1-4); what remains is time/data accumulation toward the
15-day/50-trade interim checkpoint.

Tier 3.31 (risk-filter veto attribution, seventh external review,
2026-08-25): the reviewer's core objection to Tier 3.30 — "analysis_
risk_filtered" bundles FOUR changes into one policy (removing News from
the directional vote, removing Macro from the directional vote,
removing the quorum gate, and removing Timing's session/liquidity
gating entirely, since that source never reads Timing at all), so a
trade-count difference against the live Coordinator can't be
attributed specifically to "News/Macro became risk filters." New
app.coordinator_diagnostics.compute_risk_filter_veto_attribution() and
GET /candidates/history/risk-filter-veto-attribution separate the four
out with real numbers, reusing the exact gating logic already frozen on
every stored candidate (Tier 2.1's opinions_used/conflict_flags
snapshot — the real Coordinator decision already encodes whether Timing
vetoed/dampened it) rather than any new replay. Same turn: registered
"analysis_risk_filtered" as its own prospective, watermark-locked
experiment (separate from the existing coordinator experiment
bc977800) per the review's request to start this policy's genuine
out-of-sample clock now, since a champion-challenger holdout computed
after a policy's definition was already informed by that same history
isn't a clean epistemic OOS test — only candidates created after
registration are eligible toward this new experiment's resolution;
everything analysis_risk_filtered has produced before it (including the
Package #7 champion-challenger/paired/sensitivity-grid numbers) stays
exploratory only. Entirely offline, no LLM calls, no candidate mutated,
COORDINATOR_THRESHOLD/WEIGHTS/analysis_risk_filtered's own veto scope
all untouched.

Tier 3.32 (risk-filter veto attribution corrections, eighth external
review, 2026-08-25): two real problems the reviewer found in Tier
3.31's first shipment, both fixed without touching any live behavior.
(1) The Timing finding was overstated as a general structural
impossibility — it's proven only for the auto-generated webhook
candidate path (should_run_analysis() gates real-time Analysis to
inside a kill zone); POST /agents/analysis/run?ignore_timing_gate=true
is a real manual-testing path that breaks that guarantee, so the module
comment and endpoint docstring are now explicit about the scope. (2)
The renamed `news_macro_opposition_block` -> `coordinator_score_below_
threshold_other` bucket only ever proved "the blended score didn't
cross threshold," not "News/Macro opposed Analysis" — new
`score_below_threshold_breakdown` splits it into `directional_
opposition` / `neutral_dilution` / `agreement_low_confidence` using
each present agent's own stored direction, no new replay. Also new:
`flag_prevalence` reports News urgent's and Macro risk_off's TRUE
independent counts plus their overlap, since the bucket priority order
alone understates Macro's prevalence whenever both flags co-occur on
one candidate. Entirely offline, no LLM calls, no candidate mutated,
COORDINATOR_THRESHOLD/WEIGHTS/analysis_risk_filtered's own veto scope
all untouched.

Tier 3.33 (exploratory 4-way factorial, eighth external review,
2026-08-25): Tier 3.31/3.32's veto-attribution findings showed
"analysis_risk_filtered"'s extra trades versus the real historical
"coordinator" decision come roughly 90% from quorum-blocked candidates
(insufficient_data — Analysis directional but News/Macro not both
available enough to clear MIN_AVAILABLE_WEIGHT), ~10% from the urgent/
risk_off veto actively firing, and ~0% from Timing — meaning
analysis_risk_filtered's single trade-count delta conflates two
separate structural changes (bypassing the quorum floor vs. bypassing
the veto) into one number. Two new app.backtest.DIRECTION_SOURCES
entries isolate each alone, exploratory only, before any of the four
resulting comparisons are committed to a confirmatory 15-day/50-trade
experiment (the reviewer's explicit caution against registering all
four at once): "coordinator_veto_filtered" (the REAL historical
Coordinator decision — decision.decision, already reflecting live
quorum/weights/threshold/Timing exactly as they ran — with the same
urgent/risk_off veto layered on top post-hoc, no re-scoring) and
"coordinator_quorum_bypass" (re-scores the candidate's frozen
opinions_used via the existing app.replay.replay_candidate() with
min_available_weight=0.0 as a one-off hypothetical override; live
WEIGHTS/DECISION_THRESHOLD/ANALYSIS_REQUIRED and Timing's in-scoring
veto/dampen all still apply exactly as they do for the real
Coordinator — only the 60% availability floor is lifted, and
app.coordinator.MIN_AVAILABLE_WEIGHT itself is never touched). Both
consumed generically wherever DIRECTION_SOURCES already is — every
existing backtest-lite/paired/grid/champion-challenger endpoint picks
them up automatically, no new endpoint. Entirely offline, no LLM
calls, no candidate mutated, COORDINATOR_THRESHOLD/WEIGHTS/
MIN_AVAILABLE_WEIGHT/AUTO_EXECUTE_ENABLED all untouched.

Tier 3.34 (decision-level veto transitions, ninth external review,
2026-08-25): the reviewer found that neither Tier 3.31 nor Tier 3.33
directly answers "how many of the real Coordinator's own trade
decisions would the urgent/risk_off veto have killed." Tier 3.31's
veto buckets are checked before looking at what the real Coordinator
decision actually was (never cross-tabulated against it); Tier 3.33's
coordinator_veto_filtered trade-count delta against plain coordinator
additionally mixes in non_overlapping's path-dependent scheduling
(removing an early veto'd trade can free schedule capacity for a later
candidate the original schedule skipped as overlapping), so neither
number is a clean decision-level veto count. New
app.coordinator_diagnostics.compute_veto_decision_transitions() and GET
/candidates/history/veto-decision-transitions read only already-frozen
candidate.decision fields — no replay, no barrier simulation, no
non_overlapping scheduling — and report a direct 2x2 transition between
the real historical Coordinator decision (traded vs. not) and the
hypothetical post-hoc veto (would-skip vs. wouldn't), split explicitly
by which flag was responsible (news_urgent_only/macro_risk_off_only/
both/neither), on the SAME analysis-directional population Tier 3.31
uses for direct candidate-for-candidate comparability. Also corrected a
real mislabeling this same review caught: Tier 3.31's "10%" residual-
bucket figure was described in an earlier ChatGPT package as "the
veto's share of the gap" — it never was; it's the share of analysis_
risk_filtered's extra trades attributable to something other than
quorum-bypass, an entirely different quantity from this tier's veto
kill-count. Entirely offline, no LLM calls, no candidate mutated,
COORDINATOR_THRESHOLD/WEIGHTS/MIN_AVAILABLE_WEIGHT/analysis_risk_
filtered's own veto scope all untouched.

Tier 3.35 (tenth external review, 2026-08-25): the tenth reviewer
approved Tier 3.34's method but corrected how its 56.5% headline number
(166/294 in one production pull) was described — those are Coordinator's
own directional DECISIONS, not confirmed executed real paper trades
(AUTO_EXECUTE_ENABLED is false project-wide) — and asked for it broken
down further before drawing conclusions. compute_veto_decision_
transitions() gained three purely additive pieces, no existing field's
shape or meaning changed: (1) each case now carries coordinator_
direction, news_opinion_timestamp, macro_opinion_timestamp, and
session_name; (2) direction_flag_basis_by_transition cross-tabs
transition -> coordinator_direction -> flag_basis -> count, answering
the reviewer's sharpest question directly (how many bearish/short cases
did macro_risk_off kill, versus bullish/long) without the caller
re-deriving it from raw cases; (3) news_opinion_level_day_blocked and
macro_opinion_level_day_blocked re-run the existing shared aggregator
keyed on EACH flag's own opinion identity (cases where that agent didn't
run auto-excluded), distinct from the existing Analysis-keyed
opinion_level_day_blocked. Also documented two interpretation cautions
the reviewer raised: News's urgent flag already soft-dampens the real
Coordinator's score by 0.5x independent of this diagnostic, so a
news_urgent_only kill measures the MARGINAL move from soft-dampen to
hard-block, not urgent's raw effect from zero — Macro's risk_off has no
such existing live-scoring effect, so the two flags' kill counts aren't
quite apples-to-apples. Same turn: added explicit structural invariant
tests for Tier 3.33's coordinator_veto_filtered/coordinator_quorum_
bypass sources (the reviewer's item 5) — veto_filtered never trades a
candidate the real Coordinator didn't, matches it exactly with no flags
present; quorum_bypass is a monotonic superset of real Coordinator
trades under matching config (any extra trade traces back to
insufficient_data, never a genuine no_trade) — documented as holding
only when replay's config matches the live config the fixtures are
scored under, per the reviewer's own config-drift caveat. Entirely
offline, no LLM calls, no candidate mutated, no live behavior changed —
COORDINATOR_THRESHOLD/WEIGHTS/MIN_AVAILABLE_WEIGHT/AUTO_EXECUTE_ENABLED
all untouched. Not addressed this tier (deferred as separate, larger
follow-ups per the reviewer's own priority ordering): the shared-
watermark-950 paired prospective comparison design, incremental P&L for
the killed decisions, and the risk_off semantic redesign.

Tier 3.36 (eleventh external review, 2026-08-26, items #2/#3): a fresh
production pull through Tier 3.35's direction_flag_basis_by_transition
found risk_off-implicated kills skewed ~40:1 short over long (128 vs 38
in one pull). The eleventh reviewer correctly noted the raw skew alone
can't separate "risk_off is direction-agnostic but structurally
correlated with Macro's own directional opinion, which feeds the same
score that produced the short decision in the first place" from
"risk_off is functionally anti-correlated with Macro's own bearish
reads" — a real structural-endogeneity concern, not just a wording one.
compute_veto_decision_transitions() gained purely additive pieces
answering items #2/#3 of the reviewer's priority order: (1) each case
now also carries news_direction/macro_direction (that agent's own
directional opinion, None when it didn't run); (2)
macro_risk_off_direction_crosstab cross-tabs macro_direction ->
coordinator_direction -> count, scoped to macro_risk_off cases only,
letting a caller see directly whether the short-skew traces back to
Macro itself reading bearish (the more expected, endogenous case) or
persists even when Macro read neutral/bullish (the more surprising
case); (3) macro_opinion_diversity/news_opinion_diversity (transition ->
coordinator_direction -> {candidates, distinct_opinions,
distinct_trading_days}, scoped to that flag's True cases) answer how
many DISTINCT opinions — not just candidates — produced the kill count,
since one opinion commonly anchors several candidates, and separately
track distinct trading days per the reviewer's own "day is the most
conservative unit of independence" note. No existing field's shape or
meaning changed. Entirely offline, no replay, no LLM calls, no live
behavior touched — COORDINATOR_THRESHOLD/WEIGHTS/MIN_AVAILABLE_WEIGHT/
AUTO_EXECUTE_ENABLED all untouched. Not addressed this tier (still
deferred, per the reviewer's own priority ordering): items #1
(denominators/kill-rates, already partly answered by the corrected
direction-normalized analysis in Package #11's discussion), the paired-
watermark-950 comparison, incremental P&L, and the risk_off/Macro
3-axis schema redesign.

Tier 3.37 (twelfth external review, 2026-08-26, item #1): the twelfth
reviewer confirmed Tier 3.36's 83%/17% Macro-endogeneity split (98
bearish-Macro / 20 neutral-Macro among the 118 risk_off-killed shorts)
is arithmetically sound but does NOT resolve risk_off's semantics
either way — 83% "explains" the skew mechanically without proving
risk_off is working as intended, and 17% doesn't condemn it either. The
reviewer's sharper point: a more basic gap was still open from Package
#11 — the raw 128 short / 38 long kill counts were never normalized
against how many directional decisions Coordinator produces in each
direction in the first place, so "128 killed" conflates the DISTRIBUTION
within the killed population with the PROPENSITY to kill, which needs
the full population as its denominator. compute_veto_decision_
transitions() gained one small additive field answering item #1 exactly:
direction_kill_rate_summary (coordinator_direction -> total_directional_
decisions/urgent_implicated_kills/risk_off_implicated_kills/both_
implicated_kills/survived/urgent_kill_rate/risk_off_kill_rate) — computed
directly from the existing per-case data, no new population, no replay,
no live behavior touched, rates rounded to 3 decimals following this
module's existing rate-field convention (compute_news_urgent_
prevalence's urgent_rate). Deliberately NOT done this tier, per the
reviewer's own priority ordering, which puts this denominator step
first specifically because it's small and should close the last clear
descriptive gap before anything bigger is attempted: incremental P&L
(decision-level AND portfolio-level, per the reviewer's explicit request
for both views plus a "first candidate per distinct Macro opinion"
conservative view), the paired-watermark-950 comparison, and the
risk_off/Macro 3-axis schema redesign (to be built as a separate
shadow/versioned Macro output, never silently overwriting the live
prompt or conflated with the existing 1ba9ad78 experiment's frozen
definition, per the reviewer's explicit caution).

Tier 3.38 (thirteenth external review, 2026-08-26, data-pull
methodology item): the thirteenth reviewer accepted Tier 3.37's kill-rate numbers
(73.3%/2.2%) as correctly computed but flagged that needing a manual
derivation workaround to get them (direction_kill_rate_summary itself
kept failing to pull at any large limit via WebFetch, worse than Tier
3.36's fields) is a data-observability defect in its own right, not
just a one-off inconvenience — and asked for a fix before the factorial
P&L diagnostic, not another workaround. GET /candidates/history/
veto-decision-transitions gained one new optional query param,
summary_only (default false, fully backward compatible): when true, the
response is identical in every respect except the large per-candidate
cases array is omitted entirely, leaving every summary/crosstab/rate
field (transition_summary through direction_kill_rate_summary and the
three opinion_level_day_blocked variants) intact and reachable at full
population without a multi-hundred-candidate array crowding them out of
whatever's pulling the response. Existing consumers (the dashboard,
saved queries) are unaffected since the default stays false. This is
the reviewer's own suggested fix (Package #12/#13's methodology
sections), built exactly as scoped — a query-param trim on an existing
endpoint, no new population, no replay, no live behavior touched.
Deliberately not bundled with the factorial P&L diagnostic this tier,
per the reviewer's own recommendation to fix the pull mechanism first.

Tier 3.39 (thirteenth external review, 2026-08-27, factorial incremental
P&L): with the data-observability fix shipped (summary_only) and
technically reviewed as correctly implemented, the reviewer approved
proceeding to the P&L diagnostic itself — but specified it as a
FACTORIAL 4-policy design (none / urgent_only / risk_off_only / both)
rather than a simple before/after comparison, precisely to avoid
double-counting the 63 candidates where urgent and risk_off overlap
(the reviewer's own correction to Tier 3.37's headline numbers: the
73.3%/2.2% figures measure each flag's presence among kill reasons,
not its solo/confirmed effect, which is 34.2%/2.2% once overlap is
attributed correctly). New GET /candidates/history/veto-incremental-pnl
endpoint runs the SAME barrier-backtest engine used by /backtest-lite
(identical stop/target/expiry defaults, identical slippage/commission,
same run_barrier_backtest non-overlap scheduling) across all four
policies, at both decision-level (every directional candidate) and
portfolio-level (each policy's own independent non_overlapping
schedule), further split short vs long. A dedicated attribution
section reports risk_off-solo-excluded, urgent-solo-excluded,
both-excluded (the overlap), and any-excluded (the union) as four
non-double-counted candidate sets, so solo_urgent + solo_risk_off +
overlap == union exactly by construction (covered directly by
test_veto_pnl_attribution_solo_plus_overlap_equals_union). Also adds a
macro_direction_breakdown (splits risk_off-implicated candidates by
bearish vs neutral Macro direction), a day_session_breakdown (distinct
trading days/opinions and per-session P&L for both excluded sets), and
a conservative_opinion_level view implementing the reviewer's own
naming clarification — "first candidate per (trading_date,
agent_opinion_timestamp)" as the primary dedup, plus a stricter
"first candidate per agent_opinion_timestamp across the whole history"
global variant — for both the risk_off and urgent populations. This is
a read-only diagnostic: no live policy, weight, threshold, or
execution behavior is touched; DIRECTION_SOURCES is unchanged (all
four policies reuse direction_source="coordinator" against
pre-filtered candidate subsets, not new synthetic direction sources),
and Coordinator scoring/weights remain exactly as configured. Per the
reviewer's own framing, this is the step that actually answers whether
the veto protects the account or deletes profitable short trades —
this tier only ships the measurement; interpreting the resulting
numbers and any policy recommendation is deferred to the next review
round once real production data is pulled through this endpoint.

Tier 3.40 (fourteenth external review, 2026-08-27, overlap diagnostic
extension): the first real production pull through Tier 3.39's endpoint
(Package #14) mislabeled the "both" counterfactual policy as "(live
policy)" — the reviewer caught this and it was independently verified
against source before accepting: the live Coordinator applies News'
"urgent" as a soft 0.5x score dampener (app/coordinator.py, unconditional
whenever "urgent" is set), never a hard veto, and never applies Macro's
"risk_off" at all — it is referenced nowhere in live coordinator/
execution code, only in macro_agent.py's schema and this diagnostic
family. None of Tier 3.39's four policies have ever been live; every
docstring/comment describing them now says so explicitly. The reviewer
also raised a new, explicitly NOT-yet-adopted hypothesis (urgent+
risk_off agreement may mark genuinely tradeable moves rather than pure
risk) and asked for the day/opinion diversity behind it before trusting
it — specifically a joint (News opinion, Macro opinion) PAIR count for
the 63-candidate overlap subset, since "16 opinions per flag" counted
separately doesn't reveal how many distinct combinations those opinions
actually form. Every summary dict anywhere in compute_veto_incremental_
pnl's response (policies, attribution, macro_direction_breakdown,
day_session_breakdown, conservative_opinion_level) now additionally
reports distinct_trading_days/distinct_news_opinions/distinct_macro_
opinions/distinct_joint_news_macro_opinions alongside every P&L figure
— purely additive, no existing field's shape or meaning changed, no
live policy/weight/threshold touched, DIRECTION_SOURCES unchanged.
max_drawdown_usd was already computed since Tier 3.39 and is now called
out explicitly rather than left to be independently rediscovered.

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
    compute_day_session_breakdown,
    compute_trading_date_integrity_report,
    compute_veto_incremental_pnl,
    run_paired_barrier_backtest,
    run_sensitivity_grid,
)
from app.experiments import (
    VALID_COMPARATORS,
    VALID_TARGET_METRIC_KEYS,
    ExperimentError,
    evaluate_stopping_rule,
    list_experiments,
)
from app.experiments import register_experiment as register_new_experiment
from app.experiments import resolve_experiment as resolve_existing_experiment
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
from app.coordinator_diagnostics import (
    compute_coordinator_divergence_report,
    compute_news_urgent_analysis,
    compute_news_urgent_vs_calendar_blackout,
    compute_risk_filter_veto_attribution,
    compute_threshold_crossing_deep_dive,
    compute_veto_decision_transitions,
)
from app.llm_telemetry import get_telemetry_health
from app.paper_trades import (
    PROVENANCE_AUTO_POLICY,
    PROVENANCE_MANUAL_DASHBOARD,
    get_account_open_trade_count,
    open_trade_from_candidate,
    process_new_bar,
)
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
    get_experiment_by_id,
    get_last_opinion_timestamps,
    get_last_webhook_received,
    get_latest,
    get_latest_candidate,
    get_latest_opinion,
    get_llm_call_summary,
    get_open_or_pending_trades,
    get_recent,
    get_recent_as_of,
    get_recent_decisions,
    get_recent_llm_calls,
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


@app.get("/system/llm-usage")
def system_llm_usage(
    since: str | None = Query(default=None, description="ISO timestamp — restrict to calls at or after this time; omit for all-time"),
    recent_limit: int = Query(default=20, le=200, description="how many raw recent calls to include, most recent first"),
    recent_agent: str | None = Query(default=None, description="restrict the recent-calls list to one agent (analysis/news/macro/execution)"),
) -> dict:
    """Tier 3.15 (LLM call cost/usage telemetry): closes a gap named by
    three external review cycles in a row — this project had no
    visibility into what its own LLM calls actually cost. Every
    `client.messages.create()` call site in Analysis/News/Macro/
    Execution is now wrapped (see app/llm_telemetry.track_llm_call())
    and logs exactly one row per call, success or failure, to the new
    llm_call_log table: agent, model, a short trigger_context (symbol/
    timeframe), latency, input/output/cache token counts, web_search
    call count (News/Macro use Claude's hosted web_search tool), and
    an estimated USD cost.

    `overall`/`by_agent` report call counts (total/success/failure),
    token totals, total web_search calls, total estimated cost, and
    average latency — `since` restricts the window (omit for all-
    time). `recent_calls` is a small raw tail (default 20, optionally
    filtered to one agent) for spot-checking an individual call, e.g.
    a recent failure's error message.

    `estimated_cost_usd` throughout is exactly that — an ESTIMATE
    computed from this project's own token counts against pricing
    constants that are env-configurable (TELEMETRY_INPUT_COST_PER_MTOK
    etc., see API_REFERENCE.md) since this project has no Anthropic
    Console billing access to verify actual charges. Useful for
    relative comparison and trend-watching (which agent costs the
    most, is cost trending up), not as an authoritative invoice.

    Tier 3.25 (fifth external review — "cost telemetry health", item
    #5): a telemetry write failure has always been deliberately
    swallowed (correct — a logging problem must never break a real
    agent call) but was previously invisible. `telemetry_health` now
    reports THIS PROCESS's attempted/written/failed counters since
    `telemetry_started_at` (process start — resets on redeploy), so a
    degraded write rate is visible instead of just silently under-
    counting calls. `pricing_versions_present` in the summary above
    reports which pricing regime(s) (see PRICING_VERSION in
    app/llm_telemetry.py) the queried window's estimated_cost_usd
    figures were computed under — more than one value means the window
    spans a pricing change and the cost total blends two regimes.

    Entirely read-only — does not affect any agent's behavior."""
    summary = get_llm_call_summary(since=since)
    recent = get_recent_llm_calls(limit=recent_limit, agent=recent_agent)
    return {**summary, "recent_calls": recent, "telemetry_health": get_telemetry_health()}


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
                open_trade_from_candidate(refreshed, provenance=PROVENANCE_AUTO_POLICY)
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
    analysis_required: bool = Query(default=None, description="Tier 3.24 — omit to use the current live ANALYSIS_REQUIRED"),
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
        analysis_required=analysis_required,
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
    analysis_required: bool = Query(default=None, description="Tier 3.24 — omit to use the current live ANALYSIS_REQUIRED"),
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
        analysis_required=analysis_required,
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
    analysis_required: bool = Query(default=None, description="Tier 3.24 — held fixed across the whole sweep, omit to use the current live ANALYSIS_REQUIRED"),
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
        analysis_required=analysis_required,
        horizons=_parse_replay_horizons(horizons),
    )


@app.get("/candidates/history/coordinator-divergence")
def candidates_history_coordinator_divergence(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    limit: int = Query(default=300, le=1000),
) -> dict:
    """Tier 3.16 (Coordinator/Analysis divergence + ablation): backtest-
    lite has shown Coordinator's blended decision performing at or
    below Analysis alone since Tier 3.10, and the Tier 3.12/3.14 paired
    and grid comparisons went further — on the small accepted-candidate
    sets tested so far, Coordinator and Analysis produced byte-for-byte
    IDENTICAL trade outcomes. The third external review endorsed
    investigating this directly, but specifically NOT as a shallow
    "are the directions identical?" check — that conflates several
    different situations (agreement, abstention, active override,
    inability to decide) into one number and misses whether Coordinator's
    blending ever causally changes an outcome.

    Built on app/coordinator_diagnostics.compute_coordinator_divergence_report(),
    which walks candidate history using ONLY already-existing tooling:
    every candidate already freezes its opinions_used/contributions/
    conflict_flags snapshot (Tier 2.1), and app/replay.py's
    replay_candidate() can already re-score that frozen snapshot under
    a hypothetical weights dict, entirely offline.

    `cross_tab` is the complete picture: analysis_bucket ("directional"/
    "neutral"/"unavailable") -> coordinator_decision -> count.
    `named_categories` reads the reviewer's five specific categories
    directly off it. `news_impact`/`macro_impact` report how often each
    agent was present with a real directional opinion, its average
    |contribution| to the weighted score when present, and how often
    its direction actively opposed Analysis's own direction (a same-
    direction contribution mostly just reinforces Analysis; an opposing
    one is where blending could actually change the outcome).
    `timing_blocked_count` is how many candidates had Timing's veto
    (market closed) or dampen (low liquidity) flag actually fire.
    `ablation` replays every candidate three times, each time with one
    directional agent's actual OPINION removed from its frozen
    snapshot (added to missing_agents, as if that agent's input was
    genuinely unavailable for this decision) — not a zeroed weight in
    the WEIGHTS config. Each entry reports agent_present_count (how
    many candidates actually had that agent's opinion to remove) plus
    how many final decisions actually changed — a causal answer
    ("would this decision have been different without News?"), not
    just a correlational one; decision_changed can never exceed
    agent_present_count.

    Tier 3.17 correction: the original Tier 3.16 ablation zeroed the
    agent's weight in the WEIGHTS dict instead of removing its
    opinion. That looked equivalent but wasn't — the MIN_AVAILABLE_
    WEIGHT gate's denominator (directional_weight_total) is computed
    over ALL three directional agents regardless of which were
    actually present, so zeroing a weight shrank that denominator for
    every candidate being replayed, including ones where the ablated
    agent was never present to begin with. On real production data
    this flipped 36/197 candidates (all of them "Analysis alone
    present") out of insufficient_data under BOTH the News-ablation
    AND the Macro-ablation pass, with identical transition splits —
    an artifact of the availability math, not a real finding about
    either agent's influence. Removing the agent's opinion instead of
    zeroing its weight keeps directional_weight_total at its normal
    live value, so a candidate where the agent was never present is
    now correctly a no-op.

    Tier 3.21 (ablation reclassification): the surviving raw
    decision_changed percentages still conflate a "quorum" effect
    (removing this agent alone dropped available evidence below
    MIN_AVAILABLE_WEIGHT — says nothing about whether the agent's
    DIRECTION mattered) with a real directional-influence effect
    (the weighted score moved enough to cross the threshold among
    candidates that stayed data-sufficient either way). Each entry's
    `decision_changed_by_category` splits every change into exactly
    one of `to_insufficient_data`, `direction_flipped` (the call
    reversed bullish<->bearish), or `threshold_crossing` (moved across
    one boundary without reversing). `conflict_flags_changed_count`
    and `avg_abs_score_delta_when_changed`/`_when_unchanged` add the
    raw magnitude of an agent's influence even where the category
    didn't change. `transitions` (raw {original}->{replayed} decision
    pairs) is unchanged since Tier 3.16.

    Entirely offline: no LLM calls, no new candidates, no trades.
    COORDINATOR_THRESHOLD and the live WEIGHTS config are untouched —
    each ablation pass builds a throwaway modified copy of one
    candidate's frozen snapshot for a single offline replay, never
    persisted and never touching a stored candidate or the live
    config."""
    candidates = get_candidate_history(symbol=symbol, timeframe=timeframe, limit=limit)
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        **compute_coordinator_divergence_report(candidates),
    }


@app.get("/candidates/history/threshold-crossing-deep-dive")
def candidates_history_threshold_crossing_deep_dive(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    agent: str = Query(..., description="which directional agent to ablate: analysis, news, or macro"),
    limit: int = Query(default=300, le=1000),
    horizons: str = Query(default="15,30,60"),
) -> dict:
    """Tier 3.26 (News/Macro threshold-crossing deep dive, fifth
    external review, item #6): the coordinator-divergence endpoint's
    ablation.*.decision_changed_by_category.threshold_crossing count
    (32/223-present for News, 2/215-present for Macro, confirmed
    2026-08 production) says HOW OFTEN this agent's presence crossed
    the enter/no_trade line without a quorum or direction-reversal
    effect involved — not whether that was good or bad for the
    strategy. This re-walks just that one agent's threshold_crossing
    subset and adds:

    `side` — "agent_enabled_trade" (this agent's presence is why a
    real trade was taken; without it, the candidate would have been
    no_trade) vs. "agent_prevented_trade" (the reverse: without this
    agent, a trade would have been taken that wasn't).

    `outcome` — for agent_enabled_trade, the REAL outcome
    (app.outcomes.compute_outcome_for_candidate(): real closed-trade
    win/loss/breakeven/P&L when a trade exists, the existing
    hypothetical per-horizon estimate otherwise) of the candidate that
    actually happened. For agent_prevented_trade, there is no real
    trade to look up (it never happened) — outcome is the REPLAYED
    decision's own hypothetical per-horizon estimate, relabeled
    "prevented_win" (the hypothetical direction would have been
    correct — a missed opportunity) / "prevented_loss" (it would have
    been wrong — the agent's real presence correctly avoided it).

    `agreement_with_analysis` — whether this agent's own real opinion
    direction agreed or opposed Analysis's own real opinion direction
    on that candidate ("agree" / "oppose" / "analysis_not_directional_
    or_absent").

    `agent_flags` — this agent's own self-reported flags on that
    opinion (News: urgent/low_data/stale_data; Macro: risk_off/
    conflicting_signals/stale_data — different vocabularies, not
    unified). `summary.urgent_flag_count` specifically counts "urgent"
    — a flag only News's prompt defines, so it is always 0 for
    agent=macro by construction, not a sign urgency never applies to
    Macro.

    `distinct_opinion_timestamps` — how many of the returned cases
    trace back to distinct underlying LLM opinions vs. the same
    slow-cadence News/Macro call being reused (fresh for up to
    NEWS_MACRO_MAX_AGE_MINUTES, default 90 minutes) across several
    consecutive candidates — the same duplication concern Tier 3.6
    raised for per-agent accuracy, surfaced here too so a small case
    count isn't mistaken for that many independent data points.

    `opinion_level_day_blocked` (Tier 3.29, sixth external review item
    #3): the fields above pool every case as if it were an independent
    candidate. This re-tabulates `side` two ways per trading day — the
    existing raw candidate count, and an opinion-weighted count where a
    reused agent opinion's total weight always sums to exactly 1 for
    that day, split fractionally if it landed in more than one side —
    so a reader can see whether the split reflects many independent LLM
    opinions across many days, or a handful reused heavily within one
    or two days. See app.coordinator_diagnostics._opinion_level_day_
    blocked_summary()'s own docstring for the full field shape.

    Entirely offline for the ablation/replay step (no LLM calls, no
    mutation of any stored candidate, COORDINATOR_THRESHOLD/WEIGHTS
    untouched); the agent_enabled_trade side does read real trade rows
    the same way every other outcome-aware endpoint in this project
    does."""
    if agent not in ("analysis", "news", "macro"):
        raise HTTPException(status_code=400, detail="agent must be one of: analysis, news, macro")
    candidates = get_candidate_history(symbol=symbol, timeframe=timeframe, limit=limit)
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        **compute_threshold_crossing_deep_dive(
            candidates,
            agent=agent,
            horizons=_parse_replay_horizons(horizons),
        ),
    }


@app.get("/candidates/history/news-urgent-decomposition")
def candidates_history_news_urgent_decomposition(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    limit: int = Query(default=300, le=1000),
    horizons: str = Query(default="15,30,60"),
) -> dict:
    """Tier 3.27 (sixth external review): pulling real Tier 3.26 numbers
    (News: 107 threshold_crossing cases, ~80% carrying News's "urgent"
    flag) surfaced a real measurement gap the reviewer named directly —
    News's "urgent" flag independently halves the blended score in
    app/coordinator.py's own scoring math, regardless of direction or
    agreement with Analysis, and Tier 3.26's ablation removes News's
    opinion (and therefore BOTH the directional contribution AND the
    urgent dampen) in one step. A threshold_crossing case caused entirely
    by the 0.5x dampen — no real directional disagreement at all — looked
    identical to one caused by News's own bullish/bearish read, so
    reporting the count as "News's directional influence" overstated what
    was actually measured.

    Two sections, neither touching live scoring:

    `prevalence` — the reviewer's second correction: 86/107 is NOT News's
    overall urgent rate, it's the rate WITHIN a sample already pre-
    selected by threshold_crossing (and urgent itself helps pull a
    candidate into that sample by depressing its score). Reports urgent's
    unconditional share across every News-present candidate, and
    separately across every DISTINCT News opinion (one urgent LLM call
    can be reused across many candidates while fresh) — the honest base
    rate to compare 86/107 against.

    `decomposition` — for each urgent-tagged threshold_crossing case
    (same subset GET .../threshold-crossing-deep-dive?agent=news would
    show), additionally replays two partial-modification variants of the
    frozen opinions snapshot: News present but direction forced to
    "neutral" (zero weighted contribution, urgent's dampen still applies
    — isolates the dampen ALONE) and News present with its real
    direction/confidence but "urgent" stripped from its flags (isolates
    the directional contribution ALONE). Each case's `attribution` is
    `"direction_alone"`, `"urgent_dampen_alone"`, `"both_independently_
    sufficient"`, or `"only_combination_sufficient"` (a genuine
    interaction — neither alone reproduces the original full-removal's
    changed classification, only the combination does).

    `opinion_level_day_blocked` (Tier 3.29, sixth external review item
    #3): re-tabulates `attribution` opinion-weighted and day-blocked,
    same shared aggregator threshold-crossing-deep-dive's own
    opinion_level_day_blocked uses — see that endpoint's docstring or
    app.coordinator_diagnostics._opinion_level_day_blocked_summary()'s
    own docstring for the full field shape.

    Entirely offline (no LLM calls, no candidate mutated,
    COORDINATOR_THRESHOLD/WEIGHTS/ANALYSIS_REQUIRED untouched) — every
    variant is a throwaway per-candidate copy used for one replay each,
    same guarantee the rest of this diagnostic family gives. Scoped to
    News only: Macro's flag vocabulary has no "urgent" concept."""
    candidates = get_candidate_history(symbol=symbol, timeframe=timeframe, limit=limit)
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        **compute_news_urgent_analysis(candidates, horizons=_parse_replay_horizons(horizons)),
    }


@app.get("/candidates/history/news-urgent-vs-calendar-blackout")
def candidates_history_news_urgent_vs_calendar_blackout(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    limit: int = Query(default=300, le=1000),
    window_hours: float = Query(
        default=2.0,
        description="hours before/after a real CPI/NFP/FOMC release counted as a deterministic blackout window",
    ),
    horizons: str = Query(default="15,30,60"),
) -> dict:
    """Tier 3.28 (sixth external review, ranked backlog item #2): tags
    every News-present candidate with two INDEPENDENTLY-computed
    signals — News's own self-reported "urgent" flag, and a
    deterministic calendar_blackout flag (app.economic_calendar,
    hardcoded/source-cited real 2026 CPI/NFP/FOMC release timestamps —
    has zero access to News's opinion or reasoning) — and
    cross-tabulates the two into both_flagged / news_urgent_only /
    calendar_blackout_only / neither_flagged, with `agreement_rate`
    (both_flagged + neither_flagged, as a share of all News-present
    candidates).

    For candidates that reached a directional decision, also attaches
    an outcome (real closed-trade result when one exists, the existing
    hypothetical per-horizon estimate otherwise), bucketed per quadrant
    in `outcomes_by_quadrant` — so the two signals can be compared not
    just on how often they agree, but on which one actually correlated
    with worse outcomes when they disagreed.

    `calendar_coverage` reports, honestly, how many real CPI/NFP/FOMC
    events from the registry could even have produced an
    in_blackout=True result somewhere in this specific `limit`-bounded
    history pull — read this before treating `cross_tab` as a
    confident result. At this tier's build time (2026-08-24), the live
    9-trading-day production window contained exactly ONE such event
    (the 2026-08-12 CPI release, the very first day of that window) —
    see app/economic_calendar.py's module docstring for the full
    reasoning and for why this improves automatically as more weeks of
    data accumulate (2026-09-04 NFP, 2026-09-11 CPI, and 2026-09-15/16
    FOMC are already in the registry, waiting for the trading window to
    reach them).

    `window_hours` defaults to 2.0, matching News's own prompt language
    about flagging events expected in "the next 2-3 hours" — tune it to
    see how sensitive the comparison is to the blackout's width.

    `opinion_level_day_blocked` (Tier 3.29, sixth external review item
    #3): re-tabulates `quadrant` opinion-weighted and day-blocked, same
    shared aggregator the other two diagnostics in this family use —
    see app.coordinator_diagnostics._opinion_level_day_blocked_
    summary()'s own docstring for the full field shape. Particularly
    relevant here since calendar_coverage already flags this endpoint's
    current sample as thin (one real event) — opinion_level_day_blocked
    shows directly whether even that thin sample is one distinct News
    opinion reused many times or several genuinely independent ones.

    Entirely offline (no LLM calls, no candidate mutated,
    COORDINATOR_THRESHOLD/WEIGHTS untouched); the outcome lookup reads
    real trade rows the same way every other outcome-aware endpoint in
    this project already does."""
    candidates = get_candidate_history(symbol=symbol, timeframe=timeframe, limit=limit)
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        **compute_news_urgent_vs_calendar_blackout(
            candidates,
            window_hours=window_hours,
            horizons=_parse_replay_horizons(horizons),
        ),
    }


@app.get("/candidates/history/risk-filter-veto-attribution")
def candidates_history_risk_filter_veto_attribution(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    limit: int = Query(default=300, le=1000),
) -> dict:
    """Tier 3.31 (seventh external review), corrected Tier 3.32 (eighth
    external review): app/backtest.py's "analysis_risk_filtered"
    direction source (Tier 3.30) bundles FOUR changes into one policy —
    removing News from the directional vote, removing Macro from the
    directional vote, removing the Coordinator's MIN_AVAILABLE_WEIGHT
    quorum gate, and removing Timing's session/liquidity gating entirely
    (that source never reads Timing at all) — so a trade-count
    difference against the live Coordinator can't yet be attributed
    specifically to "News/Macro became risk filters." This endpoint
    separates the four out with real numbers. NOTE (Tier 3.32): the
    Timing-gating scope is proven only for the AUTO-GENERATED webhook
    candidate path — should_run_analysis() gates real-time Analysis runs
    to inside a kill zone, so a Timing veto/dampen flag can never
    co-occur with a directional Analysis opinion THERE, but POST
    /agents/analysis/run?ignore_timing_gate=true is a real manual-
    testing path that can produce a candidate with both — not a
    system-wide impossibility, just true for every candidate this
    endpoint actually sees in normal operation.

    For every candidate with a directional (bullish/bearish) Analysis
    opinion, `summary` reports exactly one bucket per candidate:
    `news_urgent_veto` / `macro_risk_off_veto` (analysis_risk_filtered
    itself would skip this candidate — checked in that priority order,
    matching app.backtest._direction_for_source), `coordinator_agrees`
    (no veto fires and the real Coordinator traded the same direction —
    no blocking difference), `coordinator_opposite_direction` (rare/
    structurally unproven under live weights, see Tier 3.21),
    `coordinator_quorum_block` (News AND Macro both missing/stale),
    `timing_market_closed_block` / `timing_low_liquidity_block` (the
    real Coordinator decision's own conflict_flags show Timing vetoed
    or dampened it), or `coordinator_score_below_threshold_other`
    (quorum was fine, no Timing flag applied, the blended score simply
    didn't cross +-threshold — a genuine residual, NOT proof of
    directional opposition by itself; see `score_below_threshold_
    breakdown` below). `analysis_not_directional_excluded` counts
    candidates excluded before any bucket (Analysis itself missing/
    neutral — both policies skip these identically).

    `score_below_threshold_breakdown` (Tier 3.32) splits the residual
    bucket above into `directional_opposition` (News or Macro present
    with a direction that OPPOSES Analysis's), `neutral_dilution` (News
    or Macro present but direction "neutral" — dilutes the renormalized
    average without opposing anything), or `agreement_low_confidence`
    (every present other agent agrees with Analysis's direction — the
    score fell short on confidence/weighting alone, not disagreement) —
    exhaustive given at least one of News/Macro is guaranteed present in
    this bucket (quorum already passed).

    `flag_prevalence` (Tier 3.32) reports each veto flag's TRUE
    independent count (`news_urgent_total`, `macro_risk_off_total`) plus
    `both_flags_overlap` — the bucket priority order above (News checked
    before Macro) means `summary.macro_risk_off_veto` alone understates
    Macro's real prevalence whenever both flags co-occur on the same
    candidate.

    Reads every field from each candidate's already-frozen decision
    snapshot (Tier 2.1) — no new replay, no LLM calls, no candidate
    mutated. `opinion_level_day_blocked` (same shared aggregator as the
    other diagnostics in this family) re-tabulates by Analysis's own
    opinion identity, day-blocked, since this endpoint's subject is a
    whole-policy comparison rather than one reused News/Macro opinion.

    Entirely offline. COORDINATOR_THRESHOLD/WEIGHTS/analysis_risk_
    filtered's own veto scope are all untouched."""
    candidates = get_candidate_history(symbol=symbol, timeframe=timeframe, limit=limit)
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        **compute_risk_filter_veto_attribution(candidates),
    }


@app.get("/candidates/history/veto-decision-transitions")
def candidates_history_veto_decision_transitions(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    limit: int = Query(default=300, le=1000),
    summary_only: bool = Query(default=False),
) -> dict:
    """Tier 3.34 (ninth external review), extended Tier 3.35 (tenth
    external review). Corrects a real gap the ninth reviewer found:
    neither Tier 3.31 nor Tier 3.33 directly counts "how many of the
    real Coordinator's own directional DECISIONS would the urgent/
    risk_off veto have killed." (WORDING NOTE, Tier 3.35: every "trade"/
    "traded" below means the real historical Coordinator's own
    enter_long/enter_short decision, NOT a confirmed executed real paper
    trade — AUTO_EXECUTE_ENABLED is false project-wide and the real
    executed-trade count is far smaller than this endpoint's counts;
    the tenth review flagged an earlier internal write-up for blurring
    that distinction.) Tier 3.31's `news_urgent_veto`/`macro_risk_off_
    veto` buckets are checked BEFORE looking at what the real Coordinator
    decision actually was, so those candidates were never cross-tabulated
    against whether Coordinator traded them. Tier 3.33's `coordinator_
    veto_filtered` trade-count delta against plain `coordinator` (e.g.
    14 -> 5 in one production pull) additionally mixes in
    `non_overlapping`'s path-dependent scheduling (removing an early
    veto'd trade can free schedule capacity for a later candidate the
    original schedule would have skipped as overlapping) — so that delta
    isn't a clean decision-level veto count either.

    This endpoint reads only already-frozen candidate.decision fields —
    no replay, no barrier-backtest simulation, no non_overlapping
    scheduling — and reports a direct 2x2 transition between the real
    historical Coordinator decision (traded vs. not) and the
    hypothetical post-hoc veto (would-skip vs. wouldn't), on the SAME
    analysis-directional population `risk-filter-veto-attribution` (Tier
    3.31) uses, for direct candidate-for-candidate comparability:

    `transition_summary` reports exactly one of four labels per
    candidate: `coordinator_trade_veto_would_skip` (the veto's true
    direct decision-level kill count — real Coordinator traded, but a
    veto flag was present), `coordinator_trade_veto_survives` (real
    Coordinator traded, no veto flag — what `coordinator_veto_filtered`'s
    decision-level trade count should equal before any barrier-sim/
    non_overlapping path effects reshape it), `coordinator_skip_veto_
    would_also_skip` (Coordinator already skipped for its own reason AND
    a veto flag was also present — redundant, changed nothing), or
    `coordinator_skip_veto_irrelevant` (Coordinator skipped, no veto flag
    present either).

    INTERPRETATION NOTE (Tier 3.35): News's `"urgent"` flag already
    dampens the real Coordinator's score by 0.5x inside `_score_opinions`
    (Tier 2.9), independent of this endpoint. A `news_urgent_only`
    `coordinator_trade_veto_would_skip` case cleared threshold EVEN AFTER
    that existing dampening — this endpoint's hypothetical hard veto
    measures the MARGINAL move from "soft dampen, still enterable" to
    "hard block regardless of score," not urgent's raw effect from a
    zero baseline. Macro's `"risk_off"` has no such existing live-scoring
    effect, so `macro_risk_off_only` counts are the cleaner "raw" measure
    by contrast — the two flags aren't quite apples-to-apples here.

    `flag_basis_by_transition` splits each transition by which flag(s)
    were responsible (`news_urgent_only` / `macro_risk_off_only` /
    `both` / `neither`) — the explicit urgent-vs-risk_off-vs-overlap
    visibility the ninth reviewer asked for, at the transition level.
    `direction_flag_basis_by_transition` (Tier 3.35) adds a third axis —
    transition -> coordinator_direction -> flag_basis -> count —
    answering the tenth review's sharpest question directly: how many
    bearish (short) `coordinator_trade_veto_would_skip` cases came from
    `macro_risk_off_only` specifically (a large count there would be
    direct evidence `risk_off` is being used opposite to a plausible
    "bearish regime" directional meaning, since that reading would
    expect it to SUPPORT shorts, not block them). `coordinator_skip_
    reason_by_transition` further splits the two non-trade transitions
    by the real historical reason (`no_trade` vs `insufficient_data`),
    since a redundant veto means something different depending on why
    Coordinator was already skipping.

    `analysis_not_directional_excluded` counts candidates excluded
    before any transition (Analysis itself missing/neutral — matches
    `risk-filter-veto-attribution`'s own exclusion exactly, since both
    endpoints share the same precondition). `opinion_level_day_blocked`
    (same shared aggregator as the rest of this diagnostic family)
    re-tabulates by Analysis's own opinion identity, day-blocked.
    `news_opinion_level_day_blocked` and `macro_opinion_level_day_
    blocked` (Tier 3.35) re-run the same aggregator keyed on EACH flag's
    own opinion identity instead — cases where that particular agent
    didn't run are automatically excluded (not guessed into a bucket) —
    giving the flag-specific reuse/independence view the tenth reviewer
    asked for, distinct from the whole-policy Analysis-keyed view above.

    Population note (Tier 3.35, per the tenth review's methodology
    point): `candidates_considered` reflects however many candidates
    exist for this symbol/timeframe at pull time — it grows as
    production accumulates more history, so two pulls at different times
    are the SAME cumulative population plus an increment, not two
    independent samples. Compare `candidates_considered` explicitly
    across pulls rather than assuming a changed rate implies a regime
    change.

    Tier 3.36 (eleventh external review, items #2/#3): a fresh
    production pull through this endpoint found `risk_off`-implicated
    kills skewed ~40:1 toward short (bearish) Coordinator decisions. The
    eleventh review correctly noted this raw skew alone can't distinguish
    "risk_off is direction-agnostic but structurally correlated with
    Macro's own bearish reads feeding the same score" from "risk_off is
    functionally anti-correlated with Macro's own bearish reads" — both
    would need more before drawing conclusions. Two additive fields help:
    each case now also carries `news_direction`/`macro_direction` (that
    agent's own directional opinion, None when it didn't run).
    `macro_risk_off_direction_crosstab` cross-tabs macro_direction ->
    coordinator_direction -> count, scoped to `macro_risk_off == True`
    cases only — if the short-skew is driven by Macro itself reading
    bearish, that shows up as bearish-macro_direction rows dominating;
    risk_off firing while Macro reads neutral/bullish and still killing
    short decisions would be the more surprising pattern. `macro_opinion_
    diversity`/`news_opinion_diversity` (transition -> coordinator_
    direction -> {candidates, distinct_opinions, distinct_trading_days},
    scoped to that flag's True cases) answer "how many DISTINCT Macro/
    News opinions produced this kill count," not just how many
    candidates — since one opinion commonly anchors several candidates —
    and track distinct trading days too, per the eleventh review's own
    "day is the most conservative unit of independence" note.

    Tier 3.37 (twelfth external review, item #1): Tier 3.36's crosstab
    found 83% of risk_off-implicated killed shorts had Macro itself
    reading bearish versus 17% reading neutral. The twelfth reviewer's
    verdict was that this split doesn't resolve risk_off's semantics
    either way, AND flagged a more basic gap still open: Package #11's
    raw kill counts (128 short / 38 long) were never normalized against
    how many directional decisions Coordinator produces in EACH
    direction — 128-of-166-killed sounds large, but 128-of-however-many-
    short-decisions-exist is the number that measures propensity.
    `direction_kill_rate_summary` (coordinator_direction ->
    {total_directional_decisions, urgent_implicated_kills, risk_off_
    implicated_kills, both_implicated_kills, survived, urgent_kill_rate,
    risk_off_kill_rate}) adds exactly that missing denominator:
    total_directional_decisions is every real Coordinator trade in that
    direction, killed or not (would_skip + survives combined); each rate
    is that direction's implicated-kill count divided by its own total,
    rounded to 3 decimals (a direction with zero real trades simply
    doesn't appear as a key — no divide-by-zero case exists to return
    None for).

    Entirely offline. COORDINATOR_THRESHOLD/WEIGHTS/MIN_AVAILABLE_WEIGHT/
    analysis_risk_filtered's own veto scope are all untouched.

    Tier 3.38 (thirteenth external review, data-pull methodology item):
    every pull of this endpoint's summary/crosstab/rate fields has been
    fighting the same recurring problem — the per-candidate `cases`
    array dwarfs everything else in the response once the population
    grows into the hundreds, and WebFetch's own size-based summarization
    (used to pull production data for every package in this series) has
    repeatedly failed to reliably surface fields sitting even BEFORE
    `cases` once the total payload gets large enough, worsening as more
    fields have been added over Tiers 3.34-3.37. The reviewer's own
    diagnosis: WebFetch was never meant to be a large-number analytics
    tool, and every workaround so far (smaller `limit`, cross-validating
    partial windows, deriving one field from a different, more-reliable
    field) has been a workaround, not a fix.

    `summary_only=true` fixes it at the source: the response is
    identical in every other respect — same symbol/timeframe/candidates_
    considered/transition_summary/flag_basis_by_transition/direction_
    flag_basis_by_transition/coordinator_skip_reason_by_transition/
    macro_risk_off_direction_crosstab/macro_opinion_diversity/news_
    opinion_diversity/direction_kill_rate_summary/opinion_level_day_
    blocked/news_opinion_level_day_blocked/macro_opinion_level_day_
    blocked — with only the `cases` key omitted entirely. Every existing
    consumer of this endpoint (the dashboard, any saved query) keeps
    working unmodified with `summary_only` left at its default `false`;
    this is purely an opt-in trim for pulling aggregates at full
    population without the large array crowding them out. Fetching the
    raw per-candidate `cases` list itself (e.g. for the reviewer's
    requested raw-JSON-file extraction workflow) still works exactly as
    before via the default (unchanged) response — this flag only ever
    removes data from the response, it adds nothing and changes no other
    field's shape or meaning. No live behavior touched, no new
    population, no replay."""
    candidates = get_candidate_history(symbol=symbol, timeframe=timeframe, limit=limit)
    result = compute_veto_decision_transitions(candidates)
    if summary_only:
        result = {key: value for key, value in result.items() if key != "cases"}
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        **result,
    }


@app.get("/candidates/history/day-session-report")
def candidates_history_day_session_report(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    limit: int = Query(default=300, le=1000),
) -> dict:
    """Tier 3.18 (day/session reporting): the third external review's
    item 5 — "day/session trade counts should be a primary reported
    metric everywhere, not buried." A candidate count alone can look
    like a decent sample while spanning very few genuinely independent
    trading days, since candidates on a fast timeframe cluster tightly
    in calendar time. This is a standalone, quick check of that —
    same app/backtest.compute_day_session_breakdown() this tier also
    wired into every backtest-lite/paired/grid/champion-challenger
    report's top level, exposed here on its own so it can be checked
    before running anything heavier.

    `distinct_trading_days` uses each bar's own Pine-computed
    trading_date field (the CME/Globex-aware value already validated
    at ingestion — Tier 2.9 — not a naive UTC calendar-date split),
    falling back to app.trading_calendar.expected_trading_date() from
    the candidate's own anchor timestamp for the rare candidate with
    no stored bar. `candidates_per_day` (min/median/max) shows how
    concentrated candidates are within days. `by_session_name` is the
    bar's own coarse RTH/OVERNIGHT split; `by_timing_session_label` is
    Timing's finer London/NY/NY-PM/overlap/outside-sessions/weekend/
    holiday classification, already computed for every decision.

    Entirely offline and read-only: no LLM calls, no new data, no
    effect on COORDINATOR_THRESHOLD or any trading logic."""
    candidates = get_candidate_history(symbol=symbol, timeframe=timeframe, limit=limit)
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        **compute_day_session_breakdown(candidates),
    }


@app.get("/candidates/history/trading-date-integrity")
def candidates_history_trading_date_integrity(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    limit: int = Query(default=300, le=1000),
) -> dict:
    """Tier 3.19 (trading-date integrity, fourth external review,
    2026-08-18): day-session-report's distinct_trading_days trusts each
    bar's own payload trading_date field the moment it's present —
    unknown_trading_date_count==0 there only means a trading_date
    string existed, NOT that it was correct. This is the direct check
    of that assumption: for every candidate with a stored bar, it
    reports three independent views of what trading day it belongs to
    and flags where they disagree.

    `payload_trading_dates` / `distinct_payload_trading_days`: the
    literal wire value from each bar, unmodified — this is what day-
    session-report currently trusts.
    `computed_trading_dates` / `distinct_computed_trading_days`: each
    bar's own timestamp re-run through app.trading_calendar.
    expected_trading_date() (the same CME/Globex session-rollover
    convention Tier 2.9's check_trading_date() already applies at
    webhook ingestion — this just makes the comparison visible and
    aggregable instead of living only in a per-event log line /
    calendar_warning response field nobody may ever revisit).
    `utc_calendar_dates` / `distinct_utc_calendar_dates`: a THIRD,
    fully independent view — the anchor timestamp's own plain UTC
    calendar date, no NY-timezone/rollover adjustment at all. If this
    view shows more distinct dates than the other two, the rollover
    convention itself (not the underlying payload data) is what's
    collapsing days together; if all three agree, the stagnant day
    count is a real data/ingestion fact, not a reporting artifact.

    `mismatch_count` (never truncated) and `mismatch_examples` (capped
    at TRADING_DATE_MISMATCH_EXAMPLE_LIMIT, with `mismatch_examples_
    truncated` flagging when more exist) show concrete candidate_id/
    event_id/timestamp/payload-date/computed-date rows wherever the
    payload and recomputed views disagree — the previously-invisible
    calendar_warning case, now surfaced and countable instead of only
    ever appearing once in a log line at ingestion time.

    Deliberately a separate endpoint from day-session-report rather
    than merged into it: this is a forensic/validation tool (its
    mismatch_examples payload can be large on a long history), not a
    routine summary metric.

    Entirely offline and read-only: no new data, no LLM calls, no
    effect on COORDINATOR_THRESHOLD or any trading logic."""
    candidates = get_candidate_history(symbol=symbol, timeframe=timeframe, limit=limit)
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        **compute_trading_date_integrity_report(candidates),
    }


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

    IMPORTANT (Tier 3.12 correction): this is a POLICY comparison, not
    a paired one — each source independently applies the
    `non_overlapping` schedule against ITS OWN resolved directions, so
    different sources can end up trading different candidate subsets
    (a candidate analysis stays flat on may still open a trade under
    coordinator's direction). An earlier version of this docstring
    claimed a "same candidate population" comparison; that was
    inaccurate and has been corrected. For a true apples-to-apples
    comparison — one shared entry, one shared non-overlap schedule,
    only candidates every requested source can resolve — use
    GET /candidates/history/backtest-lite/paired instead.

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


@app.get("/candidates/history/backtest-lite/paired")
def candidates_history_backtest_lite_paired(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    limit: int = Query(default=300, le=1000),
    sources: str = Query(
        ...,
        description="comma-separated direction sources to compare on a shared candidate population, e.g. analysis,coordinator,inverse_analysis — at least one required",
    ),
    atr_stop_mult: float = Query(default=ATR_STOP_MULT),
    atr_target_mult: float = Query(default=ATR_TARGET_MULT),
    expiry_bars: int = Query(default=EXPIRY_BARS, le=200),
) -> dict:
    """Tier 3.12 (paired signal comparison): the plain backtest-lite
    endpoint above compares sources as independent POLICIES — each one
    applies its own non-overlap schedule against its own resolved
    directions, so two sources can end up trading different candidate
    subsets entirely. That is a fair comparison of "what would running
    on this signal alone have looked like," but it is NOT a clean
    "does signal A beat signal B" comparison, since a difference in
    results can come from which candidates were traded rather than
    what direction was called on the same candidate. This endpoint,
    built on app/backtest.run_paired_barrier_backtest(), fixes that:
    it keeps only candidates where EVERY requested source can resolve
    a direction (the full intersection), then runs every source
    through the identical entry price, ATR-derived stop/target
    geometry, and forward bar walk for each accepted candidate — one
    shared, direction-independent non-overlap schedule decides which
    candidates are accepted at all, so no source's own resolved
    direction can influence which candidates make the comparison.
    Any remaining difference in a source's results is attributable to
    the direction call itself, not to a different candidate mix.

    Raised by the external review after Tier 3.11: fix the fairness of
    the comparison before drawing further conclusions from
    backtest-lite. `sources` requires at least one recognized source
    (400 if empty or if any entry is unrecognized). `config` in the
    response reports `candidates_considered` (pulled from history),
    `eligible_candidates` (resolved a direction under every requested
    source), and `accepted_candidates` (also passed the shared
    non-overlap schedule) so the funnel from raw history down to
    actual paired trades is visible.

    Entirely offline: no LLM calls, no new data collection, nothing
    written to any trade table. COORDINATOR_THRESHOLD and the
    Coordinator's own scoring are untouched — this is read-only
    analysis, same as every diagnostic tier before it."""
    source_list = [s.strip() for s in sources.split(",") if s.strip()]

    candidates = get_candidate_history(symbol=symbol, timeframe=timeframe, limit=limit)
    try:
        result = run_paired_barrier_backtest(
            candidates,
            sources=source_list,
            stop_mult=atr_stop_mult,
            target_mult=atr_target_mult,
            expiry_bars=expiry_bars,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"symbol": symbol, "timeframe": timeframe, **result}


@app.get("/candidates/history/backtest-lite/sensitivity-grid")
def candidates_history_backtest_lite_sensitivity_grid(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    limit: int = Query(default=300, le=1000),
    sources: str = Query(
        ...,
        description="comma-separated direction sources to run across the grid, e.g. analysis,coordinator,inverse_analysis — at least one required",
    ),
) -> dict:
    """Tier 3.14 (parameter sensitivity grid): every backtest-lite/
    paired/champion-challenger result reported so far used ONE
    geometry (the default 1.5x ATR stop, 2.5x ATR target, 24-bar
    expiry) — a source that only looks good under that one specific
    choice could just be an artifact of that choice, not a real edge.

    Runs app/backtest.run_paired_barrier_backtest() (the corrected,
    Tier 3.12 paired comparison) once per combination in a small,
    PRE-REGISTERED grid: default stops {1.0, 1.5, 2.0}x ATR, targets
    {1.5, 2.0, 2.5}x ATR, expiry {6, 12, 24} bars = 27 combinations.
    Deliberately NOT configurable via query parameters — the grid is
    fixed at deploy time via BACKTEST_GRID_STOP_MULTS /
    BACKTEST_GRID_TARGET_MULTS / BACKTEST_GRID_EXPIRY_BARS env vars.
    Letting a caller pick the grid per-request would defeat the whole
    point of pre-registration: fixing the parameter space BEFORE
    looking at results, so nobody can quietly keep re-running different
    geometries until one happens to look favorable (that would just be
    overfitting under a different name — exactly what this feature
    exists to guard against).

    `robustness` per source reports how many of the grid's combinations
    were net positive / had profit_factor > 1, plus the range of
    total_pnl_usd across the whole grid, and the median win_rate across
    all combinations — a source with a real edge should hold up across
    MOST reasonable geometries, not just the one that happened to be
    tested first. `combinations` has one compact entry per grid point
    (trades_taken, win_rate + its 95% CI, profit_factor, total_pnl_usd
    per source) — the full per-trade detail is intentionally omitted to
    keep the response a reasonable size across 27 combinations.

    Entirely offline: no LLM calls, no new data collection, nothing
    written to any trade table. 400 if `sources` is empty or contains
    an unrecognized value. COORDINATOR_THRESHOLD and the Coordinator's
    own scoring are untouched — read-only analysis, same as every
    diagnostic tier before it."""
    source_list = [s.strip() for s in sources.split(",") if s.strip()]

    candidates = get_candidate_history(symbol=symbol, timeframe=timeframe, limit=limit)
    try:
        result = run_sensitivity_grid(candidates, sources=source_list)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"symbol": symbol, "timeframe": timeframe, **result}


@app.get("/candidates/history/veto-incremental-pnl")
def candidates_history_veto_incremental_pnl(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    limit: int = Query(default=300, le=1000),
    atr_stop_mult: float = Query(default=ATR_STOP_MULT),
    atr_target_mult: float = Query(default=ATR_TARGET_MULT),
    expiry_bars: int = Query(default=EXPIRY_BARS, le=200),
) -> dict:
    """Tier 3.39 (thirteenth external review) — see app/backtest.py's
    Tier 3.39 module comment block for the full design rationale. Every
    prior veto-decision-transitions diagnostic (Tier 3.34-3.38) answered
    HOW OFTEN or HOW SKEWED the urgent/risk_off veto's effect is — none
    of them answer whether the killed decisions would have won or lost.
    This endpoint does, by running the SAME app.backtest.
    run_barrier_backtest() ATR-barrier simulation every other backtest
    endpoint in this project uses — identical stop/target/expiry/
    slippage/commission mechanics throughout — against pre-filtered
    candidate subsets for four fixed policies and several breakdowns.

    NOT entirely offline, unlike the rest of this diagnostic family: it
    performs real forward-bar lookups and barrier simulations per
    candidate (same profile as /backtest-lite and /backtest-lite/paired)
    and runs roughly 40-50 separate backtest passes internally, so it
    can be noticeably slower than this family's purely-offline endpoints
    on a large population.

    Four policies (`policies` in the response, exact meaning): `none`
    (every real Coordinator directional decision — the pre-veto
    baseline), `urgent_only` (drop only if News carries "urgent"),
    `risk_off_only` (drop only if Macro carries "risk_off"), `both`
    (drop if either flag is present — the same candidate set Tier
    3.33's `coordinator_veto_filtered` direction_source already
    selects).

    Three views: `decision_level` (every candidate simulated
    independently, non_overlapping=False — NOT 299 independent trading
    opportunities, just a raw per-candidate baseline); `portfolio_level`
    (non_overlapping=True, independently scheduled per policy — the
    economically real number). CAUTION: each view's `short`/`long`
    sub-keys are separately-scheduled subsets, not a decomposition of
    `overall` — at `portfolio_level`, `short.trades_taken + long.
    trades_taken` can exceed `overall.trades_taken`, since real
    single-position scheduling lets a long and a short compete for the
    same slot but the isolated per-direction subsets don't reflect that
    competition. `overall` is the only "if this policy ran live" figure
    at portfolio level.

    `attribution` avoids the exact double-counting mistake the
    thirteenth review caught in an earlier package: `risk_off_solo_
    excluded`/`urgent_solo_excluded` (each flag's OWN effect, the other
    flag absent) plus `both_excluded_overlap` sum EXACTLY to `any_
    excluded_union` in candidate count — no candidate's P&L is counted
    toward both flags' "effect."

    `macro_direction_breakdown` splits the risk_off-flagged population
    by Macro's OWN direction on that candidate (continuing Tier 3.36's
    endogeneity question with real outcome data). `day_session_
    breakdown` reports distinct-opinion/distinct-day counts (Tier
    3.36's diversity convention) plus a by-session P&L split, for both
    flagged populations. `conservative_opinion_level` dedupes to one
    candidate per independent opinion — `first_per_day_and_opinion`
    (one per (trading_date, opinion_timestamp)) and the stricter
    `first_per_opinion_global` (one per opinion_timestamp across the
    entire history) — decision-level P&L only, per the thirteenth
    review's own reasoning that a schedule on an already-deduped set
    adds a confound rather than information.

    `config` reports the exact stop_mult/target_mult/expiry_bars used —
    same query params and same defaults as /backtest-lite, so a result
    here is directly comparable to any other backtest endpoint's default
    run. Entirely read-only: no candidate mutated, nothing written to
    any trade table, COORDINATOR_THRESHOLD/WEIGHTS/MIN_AVAILABLE_WEIGHT/
    AUTO_EXECUTE_ENABLED all untouched — this only ever simulates a
    HYPOTHETICAL policy against real historical price bars, it never
    places or affects a real trade.

    IMPORTANT — none of the four policies have ever been live: the real
    Coordinator applies News' "urgent" as a soft 0.5x score dampener
    (app/coordinator.py), never a hard veto, and never applies Macro's
    "risk_off" at all (it exists only in this diagnostic family and
    macro_agent.py's schema). The registered analysis_risk_filtered
    shadow experiment is a third, separate mechanism (follows Analysis's
    own direction, bypasses quorum). Do not describe any of `none`/
    `urgent_only`/`risk_off_only`/`both` as "the live policy" — they are
    all counterfactuals for comparison only (fourteenth external review's
    correction after an earlier analysis package mislabeled `both` this
    way).

    Tier 3.40 (fourteenth review): every summary dict anywhere in this
    response — every policy/split, every attribution set, every
    macro_direction_breakdown/day_session_breakdown/conservative_
    opinion_level entry — additionally reports `distinct_trading_days`,
    `distinct_news_opinions`, `distinct_macro_opinions`, and `distinct_
    joint_news_macro_opinions` (distinct (News opinion, Macro opinion)
    PAIRS, not the two counted separately) alongside every P&L figure,
    plus `max_drawdown_usd` (computed since Tier 3.39, just wasn't
    previously highlighted) — so no P&L number can be read without its
    underlying sample diversity sitting right next to it."""
    candidates = get_candidate_history(symbol=symbol, timeframe=timeframe, limit=limit)
    result = compute_veto_incremental_pnl(
        candidates, stop_mult=atr_stop_mult, target_mult=atr_target_mult, expiry_bars=expiry_bars,
    )
    return {"symbol": symbol, "timeframe": timeframe, **result}


@app.post("/experiments")
def register_experiment_endpoint(
    symbol: str = Query(...),
    timeframe: str = Query(...),
    hypothesis: str = Query(..., description="what this experiment is testing, in plain language"),
    primary_metric: str = Query(..., description=f"one of {VALID_TARGET_METRIC_KEYS} — the metric resolve_experiment() judges success/failure by"),
    comparator: str = Query(..., description=f"one of {VALID_COMPARATORS}"),
    success_threshold: float = Query(..., description="e.g. primary_metric=win_rate&comparator=>=&success_threshold=0.55"),
    secondary_metrics: list[str] = Query(default=[], description="reported at resolution but not gated — repeat this param once per metric"),
    direction_source: str = Query(default="coordinator", description=f"one of {DIRECTION_SOURCES}"),
    min_distinct_trading_days: int | None = Query(default=None),
    min_accepted_trades: int | None = Query(default=None),
    x_webhook_secret: str | None = Header(default=None, alias="X-Webhook-Secret"),
) -> dict:
    """Tier 3.20 (experiment registry), hardened in Tier 3.23 (fifth
    external review): pre-registers a hypothesis against the CURRENT
    live coordinator_threshold/weights/min_available_weight AND
    backtest geometry (ATR stop/target mult, expiry_bars, slippage,
    commission, backtest logic version), snapshotted and frozen at this
    moment — changing any of these later never retroactively edits an
    already-registered experiment's locked_config, and (Tier 3.23)
    evaluate_stopping_rule()/resolve_experiment() actually RE-SCORE
    every prospective candidate under this frozen config rather than
    trusting whatever each candidate's own stored decision happened to
    be computed under. registered_watermark_rowid (Tier 3.23 — a
    monotonic integer, not a timestamp) marks the hard boundary: only
    candidates inserted after this exact row are ever eligible to count
    toward this experiment's stopping rule or resolution — existing
    candidates are exploratory and permanently ineligible for this
    experiment, by design.

    At least one of min_distinct_trading_days / min_accepted_trades
    must be set (the stopping rule) — an experiment with no stopping
    rule could never legitimately be resolved. target_metrics (Tier
    3.23: primary_metric/comparator/success_threshold, structured and
    validated — no longer a free-text list) is a real commitment
    device: resolve_experiment() computes and reports whether the
    primary metric actually met its pre-registered bar, not just that
    some numbers were recorded.

    Secret-protected: this writes to the database and, once resolved,
    the record is permanent — same guard as /webhook/tradingview and
    the /agents/*/run endpoints."""
    _check_secret(x_webhook_secret)
    stopping_rule = {}
    if min_distinct_trading_days is not None:
        stopping_rule["min_distinct_trading_days"] = min_distinct_trading_days
    if min_accepted_trades is not None:
        stopping_rule["min_accepted_trades"] = min_accepted_trades
    target_metrics = {
        "primary_metric": primary_metric,
        "comparator": comparator,
        "success_threshold": success_threshold,
        "secondary_metrics": secondary_metrics,
    }
    try:
        return register_new_experiment(
            symbol=symbol, timeframe=timeframe, hypothesis=hypothesis,
            target_metrics=target_metrics, stopping_rule=stopping_rule,
            direction_source=direction_source,
        )
    except ExperimentError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/experiments")
def list_experiments_endpoint(
    symbol: str | None = Query(default=None),
    timeframe: str | None = Query(default=None),
) -> dict:
    """Every registered experiment, newest first — append-only history,
    not a "latest" view. Filter by symbol/timeframe, or omit both to
    see every experiment across every symbol/timeframe."""
    return {"experiments": list_experiments(symbol=symbol, timeframe=timeframe)}


@app.get("/experiments/{experiment_id}")
def experiment_by_id_endpoint(experiment_id: str) -> dict:
    """Full experiment record (hypothesis, locked_config, target_metrics,
    stopping_rule, status, resolution once resolved) PLUS a live,
    read-only, non-consuming stopping_rule_status computed against
    prospective candidates right now — checking this as many times as
    you like never resolves the experiment or affects the eventual
    outcome.

    Tier 3.23: 500 (not an unhandled crash) if the prospective window
    has grown past EXPERIMENT_MAX_PROSPECTIVE_CANDIDATES — an unusual
    condition (a very long-running experiment), not a normal 4xx."""
    experiment = get_experiment_by_id(experiment_id)
    if experiment is None:
        raise HTTPException(status_code=404, detail=f"no experiment found with id={experiment_id}")
    try:
        return {
            **experiment,
            "stopping_rule_status": evaluate_stopping_rule(experiment),
        }
    except ExperimentError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/experiments/{experiment_id}/resolve")
def resolve_experiment_endpoint(
    experiment_id: str,
    x_webhook_secret: str | None = Header(default=None, alias="X-Webhook-Secret"),
) -> dict:
    """The one-time outcome recording. 409 if the stopping rule isn't
    met yet (check GET /experiments/{id} first — no forcing an early
    look). If already resolved, returns the SAME resolution recorded
    the first time this succeeded — calling this again after more data
    accumulates never recomputes it. Secret-protected, same guard as
    registration.

    Tier 3.23: 500 (not 409) if the prospective window has grown past
    EXPERIMENT_MAX_PROSPECTIVE_CANDIDATES — that's a safety ceiling
    tripping, not "the stopping rule isn't met yet.\""""
    _check_secret(x_webhook_secret)
    try:
        return resolve_existing_experiment(experiment_id)
    except ExperimentError as e:
        message = str(e)
        if message.startswith("no experiment with id"):
            status_code = 404
        elif "not yet met" in message:
            status_code = 409
        else:
            status_code = 500
        raise HTTPException(status_code=status_code, detail=message)


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
    analysis_required: bool = Query(default=None, description="Tier 3.24 — omit to use the current live ANALYSIS_REQUIRED"),
    include_outcome: bool = Query(default=False, description="also compute the hypothetical horizon outcome for the replayed decision"),
    horizons: str = Query(default="15,30,60"),
) -> dict:
    """Tier 2.5: re-scores ONE candidate's frozen opinions_used under a
    config — the live config by default, or an explicit hypothetical
    override for weights/threshold/min_available_weight/analysis_required
    (Tier 3.24). Never mutates the original candidate or opens a trade;
    purely a read-only recompute for answering "what would the
    Coordinator have decided here under a different config?"."""
    candidate = get_candidate_by_id(candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail=f"no candidate found with id={candidate_id}")

    return replay_candidate(
        candidate,
        weights=_parse_replay_weights(weights),
        threshold=threshold,
        min_available_weight=min_available_weight,
        analysis_required=analysis_required,
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
    exclude_provenance: str | None = Query(
        default=None,
        description=(
            "Tier 3.22: comma-separated provenance values to drop from the "
            "response, e.g. exclude_provenance=manual_dashboard to see only "
            "trades the AUTO_EXECUTE_ENABLED-gated policy itself opened. "
            "Omit for the full stored history (default, backward compatible "
            "— every trade ever opened, manual or automatic)."
        ),
    ),
) -> list[dict]:
    """Closed trades, newest first, with realized pnl_usd and
    exit_reason ("stop_hit" | "target_hit").

    Tier 3.22 (fifth external review): every trade now carries a
    `provenance` field ("auto_policy" or "manual_dashboard" — see
    app/paper_trades.open_trade_from_candidate()). This endpoint's
    default behavior is UNCHANGED (returns everything, exactly as
    before) — `exclude_provenance` is opt-in, so a caller building a
    "system performance" view can exclude manual dashboard actions
    (e.g. pipeline tests) without this endpoint silently deciding that
    for them. Filtering happens AFTER `limit` is applied to the
    underlying newest-first query — with a small account this rarely
    matters, but a caller who needs an exact post-filter count should
    pass a generously large `limit` rather than trust the response
    length."""
    trades = get_recent_trades(symbol=symbol, timeframe=timeframe, limit=limit)
    if exclude_provenance:
        excluded = {v.strip() for v in exclude_provenance.split(",") if v.strip()}
        trades = [t for t in trades if t.get("provenance") not in excluded]
    return trades


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
    pattern as /trades/* and /candidates/*.

    Tier 3.22 (fifth external review): `closed_trades_by_provenance`
    is new — a breakdown of `closed_trades_considered` by
    "auto_policy" vs "manual_dashboard" (see app/paper_trades.py),
    purely for visibility into whether the numbers above include any
    manually-opened dashboard trades (e.g. pipeline tests). Deliberately
    NOT filtered out of `current_drawdown_used`/`daily_loss_used` by
    default — whether a manual paper trade should count against the
    account's real risk budget is a genuine open design question (does
    a manual pipeline test consume real paper-account risk capacity, or
    not?) that the review flagged but explicitly left to the user's own
    judgment, same as the Analysis-load-bearing design question. Not
    decided here; not silently changed."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    closed_trades = get_all_closed_trades_chronological()
    current_drawdown_used = compute_current_drawdown_used(trades=closed_trades)
    daily_loss_used = compute_daily_loss_used(now_iso, trades=closed_trades)
    closed_trades_by_provenance: dict[str, int] = {}
    for trade in closed_trades:
        key = trade.get("provenance") or "unknown"
        closed_trades_by_provenance[key] = closed_trades_by_provenance.get(key, 0) + 1
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
        "closed_trades_by_provenance": closed_trades_by_provenance,
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
        trade = open_trade_from_candidate(candidate_for_trade, provenance=PROVENANCE_MANUAL_DASHBOARD)

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
