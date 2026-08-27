"""
Backtest-lite — Tier 3.10 (ATR-barrier benchmark).

Every accuracy figure in this project up through Tier 3.9 (outcomes.py,
replay.py) uses the same proxy: "was price higher/lower than the
decision price N minutes later." That's cheap and has driven every
real finding so far (the below-50% Analysis accuracy, the bullish
bias, the base-rate baselines), but it was never a trade simulation —
no entry/stop/target geometry, no slippage, no commission, no notion
of "the stop got hit before the target did." The second external
review (2026-08-14) named this directly: comparing Analysis against
simple baselines only means something once the comparison uses
REALISTIC trade mechanics, the same mechanics the live paper-trade
engine already uses for real trades (app/paper_trades.process_new_bar)
— otherwise a baseline that "wins" on the horizon-price proxy might
lose once stop-outs and slippage are accounted for, or vice versa.

This module runs that same fill/stop/target/slippage/commission logic
OFFLINE, over bars already sitting in storage, for a hypothetical
trade that was never actually taken — no LLM calls, no side effects,
nothing written to any trade table. It deliberately mirrors
process_new_bar()'s conventions exactly (gap-adjusted stop, stop wins
a same-bar tie against the target, commission subtracted on close, no
favorable-gap credit at the target) so a backtest-lite number and a
real paper-trade number are computed the same way and are actually
comparable, not two different yardsticks.

Entry/stop/target geometry: ATR-based, not Execution's LLM-proposed
levels — the point of this benchmark is to answer "does directional
signal X have a testable edge against a cheap, deterministic
baseline," not to re-litigate what Execution would have picked.
Entry is the bar immediately AFTER the candidate's own anchor bar
(the anchor bar is already closed by the time any decision is made
from it — entering "into" it would be lookahead bias, same rule the
live engine follows for real fills). Stop/target are the anchor bar's
own ATR (already computed and stored, see app/models.py `atr` field,
no lookahead there either) times a stop/target multiple.

Non-overlapping sampling: consecutive candidates on a fast timeframe
can be seconds/minutes apart, so back-to-back "trades" from adjacent
candidates would mostly be the same underlying price move counted
twice — not independent evidence either way. compute_backtest() skips
any candidate whose anchor timestamp falls before the previous
simulated trade (from the same run) resolved, exactly mirroring
MAX_OPEN_POSITIONS=1's real-world constraint of one position at a
time rather than pretending unlimited concurrent hypothetical
positions.

Direction sources (what decides bullish/bearish for a given
candidate) let the SAME barrier engine be pointed at different
signals for comparison, without re-implementing the simulation once
per signal: "analysis" (Analysis's own opinion, independent of the
blended Coordinator decision — same principle Tier 3.5 established),
"coordinator" (the actual blended enter_long/enter_short decision),
"inverse_analysis" (Analysis's calls flipped — diagnostic only, never
acted on, same framing as Tier 3.8's baseline), "always_bullish",
"always_bearish", and "vwap" (bullish when the anchor bar's own
distance_from_vwap_points > 0, same convention as Tier 3.8's
vwap_direction baseline). Comparing a real signal's numbers against
the trivial baselines' numbers, all run through the identical barrier
mechanics, is what the review meant by "if Analysis doesn't beat
these, there's no case for its 40% weight" — that comparison couldn't
be made honestly on the horizon-price proxy alone.

COORDINATOR_THRESHOLD and the Coordinator's own scoring are untouched
by this module, same as every diagnostic tier before it — this is
read-only analysis, not a trading-logic change.

Tier 3.11 (champion/challenger, out-of-sample): the first real
backtest-lite run against production found inverse_analysis (Analysis's
calls flipped) as the only source with profit_factor > 1 — exactly the
kind of finding the external review warned about, since it was found
on the same historical sample it would be used to justify a change
against. split_candidates_chronologically() and
compute_champion_challenger_report() below hold out the most RECENT
slice of candidate history (never a random split — regimes are
time-correlated) and run every source on the calibration window AND
the held-out validation window separately, so a reader can see whether
a challenger's apparent edge survives on data it was never fitted to.
Reports both windows side by side; never picks a winner or flips
anything on its own.

Tier 3.12 (methodology corrections): a second external review pass
(2026-08-14), reviewing the Tier 3.10/3.11 results at commit b2003b7,
raised three issues fixed here. (1) compute_backtest_comparison()
above runs each source as an independent POLICY — its own
non-overlap schedule against its own resolved directions — so
different sources can trade different candidate subsets; that
function's docstring previously (inaccurately) called this a "same
candidate population" comparison. run_paired_barrier_backtest() below
is the actual paired comparison: one shared entry price, one shared,
direction-independent non-overlap schedule, and only candidates every
requested source can resolve a direction for, so a difference in
results is attributable to the direction call alone. (2) a
calibration-window candidate near the validation boundary could have
its forward barrier walk read bars that fall inside the validation
window — a leakage risk for calibration's own numbers specifically
(not for validation's, since a validation trade only ever looks
forward from its own anchor). split_candidates_chronologically()
gained an optional expiry_bars embargo that purges any such
calibration candidate; compute_champion_challenger_report() now uses
it and reports the purged count. (3) simulate_barrier_trade()'s
"expired" exit previously priced the mark-to-last-close exit with no
slippage, inconsistent with every other exit type in the function —
fixed to apply the same against-the-trader slippage as a stop-out.
All three are read-only methodology fixes: no trading-logic change,
COORDINATOR_THRESHOLD untouched.

Tier 3.13 (small-sample statistics): every backtest-lite/champion-
challenger result so far has been read off win_rate and profit_factor
alone, which are exactly the two statistics that are most volatile at
the sample sizes this project actually has (paired comparisons run as
low as single-digit trade counts, Tier 3.12's own paired endpoint
returned 7 accepted trades on its first production run). A 5/7 vs 2/7
split LOOKS like a big difference in win_rate but is well within noise
at that N. Each summary now also reports win_rate_ci95_low/
win_rate_ci95_high (a Wilson score interval on wins/decided, the
standard correction for small-N binomial proportions — plain
Wald/normal-approximation intervals misbehave badly below ~30 trades),
median_pnl_usd (a robustness check against a single large win or loss
dominating the mean), and max_drawdown_usd (largest peak-to-trough
equity dip across the trade sequence in the order it was taken, i.e.
"how bad did it get along the way" rather than just the ending total).
None of this changes which trades are simulated or how — purely
additional read-only reporting on results already being computed,
same as every diagnostic tier before it.

Tier 3.14 (parameter sensitivity grid): every result reported through
Tier 3.13 used ONE geometry (1.5x ATR stop, 2.5x ATR target, 24-bar
expiry) — a source that only looks good under that one specific choice
could just be an artifact of that choice, not a real edge. The
external review's own recommendation was a small, PRE-REGISTERED grid
(fixed before looking at results, so nobody can quietly keep re-running
different geometries until one looks favorable — that would just be
overfitting by another name). run_sensitivity_grid() below runs
run_paired_barrier_backtest() (Tier 3.12's corrected paired comparison)
across a small fixed grid (default stops {1.0, 1.5, 2.0}x ATR, targets
{1.5, 2.0, 2.5}x ATR, expiry {6, 12, 24} bars = 27 combinations,
env-configurable via BACKTEST_GRID_STOP_MULTS / BACKTEST_GRID_TARGET_
MULTS / BACKTEST_GRID_EXPIRY_BARS — deliberately NOT a per-request
query parameter, since letting a caller pick the grid per-request
would defeat the entire point of pre-registration). Reports a compact
per-combination result per source, plus a robustness summary (how many
of the 27 combinations were net positive / had profit_factor > 1, and
the range of total_pnl_usd across the whole grid) — a source with a
real edge should look decent across MOST reasonable geometries, not
just the one first tested. Entirely offline, no LLM calls, no new
trades simulated beyond what backtest-lite already simulates per
combination — COORDINATOR_THRESHOLD untouched.

Tier 3.18 (day/session reporting): the third external review's item 5
— "day/session trade counts should be a primary reported metric
everywhere, not buried." A win_rate/profit_factor headline on N
candidates can look like a large sample while actually spanning very
few genuinely independent trading days (candidates on a fast timeframe
cluster tightly in calendar time; two decisions minutes apart during
the same session are far closer to one data point than two).
compute_day_session_breakdown() below reports, for whatever candidate
set a caller already has, the count of distinct trading days spanned
(using each bar's own Pine-computed trading_date field — the CME/
Globex-aware value, not a naive UTC calendar-date split — falling back
to app.trading_calendar.expected_trading_date() applied to the
candidate's own anchor timestamp for the rare candidate with no stored
bar), how candidates are distributed per day (min/median/max), and a
breakdown by session (the bar's own RTH/OVERNIGHT session_name, plus
the finer London/NY/NY-PM/overlap/outside-sessions breakdown Timing
already classifies every decision into). Wired into every existing
backtest-lite/paired/grid/champion-challenger report's top level (the
"not buried" part — the reviewer's literal complaint) rather than left
as a separate, easy-to-skip endpoint only. Also exposed standalone via
GET /candidates/history/day-session-report for a quick check before
running anything else. Purely descriptive, read-only reporting on
candidates already fetched — no new data collection, no change to
which trades are simulated or how, COORDINATOR_THRESHOLD untouched.

Tier 3.19 (trading-date integrity, fourth external review, 2026-08-18):
after Tier 3.18 shipped, real production data showed distinct_trading_
days stuck at 4 even as candidates_considered grew by 43 over a window
that included a genuine trading weekday (a Monday). The review flagged
a specific, previously-unverified assumption in compute_day_session_
breakdown() (and _candidate_trading_date() below): it trusts a bar's
payload trading_date field at face value the moment it's present, and
unknown_trading_date_count==0 only means "a trading_date string was
present" — NOT that the string is correct. Tier 2.9's check_trading_
date() already computes a mismatch warning (calendar_warning) at
webhook ingestion time, but only returns/logs it per-event — it was
never persisted on the candidate or aggregated anywhere, so a
systematic mismatch (a stale Pine Script value, a DST edge case, clock
skew) would have been invisible in every report built since.

compute_trading_date_integrity_report() below is the direct fix: for
every candidate with a stored bar, it cross-checks the LITERAL payload
trading_date against a freshly recomputed one (the same app.trading_
calendar.expected_trading_date() call check_trading_date() already
makes, just re-run here so the result is visible/aggregable instead of
living only in a per-event log line), plus a third, fully independent
view — the anchor timestamp's own plain UTC calendar date, with no NY-
timezone/session-rollover adjustment at all. Three independent date
views (not two) means a maintainer isn't relying on the same rollover
math on both sides of any single comparison. Reports per-view distinct-
date counts, a UTC-calendar-date sanity breakdown, the total mismatch
count (never capped) plus a capped list of concrete mismatch examples
(candidate_id/event_id/anchor_timestamp/payload date/computed date),
and the candidate set's earliest/latest anchor timestamp. Entirely
offline/read-only — no new data, no scoring change, COORDINATOR_
THRESHOLD untouched. Exposed via GET /candidates/history/trading-date-
integrity, deliberately kept separate from day-session-report rather
than merged into it: this is a forensic/validation tool (its mismatch_
examples payload can be large) with a different purpose than the day/
session summary it's meant to double-check.

Tier 3.30 ("analysis_risk_filtered" shadow policy, sixth external
review, ranked backlog item #4, 2026-08-24): the reviewer asked for a
parallel policy — Analysis alone decides direction, News/Macro act only
as risk filters — run alongside the live system without touching it.
Every diagnostic tier before this one has answered a "what if" question
by re-walking already-stored candidate history offline, never by
standing up a new live-running process; this is the same pattern, not
an exception. "analysis_risk_filtered" is simply a new entry in
DIRECTION_SOURCES: same direction call as the existing "analysis"
source (Analysis's own opinion, independent of Coordinator's blended
decision), but the candidate is skipped entirely (both are direction
sources are None/None) if News's opinion carries the "urgent" flag or
Macro's opinion carries the "risk_off" flag — News/Macro can only VETO
a trade Analysis wanted to take, never supply or shift its direction,
matching "risk filter" as the reviewer meant it. Confirmed with the
project owner (2026-08-24) which two flags actually count as a risk
veto out of News's full urgent/low_data/stale_data vocabulary and
Macro's full risk_off/conflicting_signals/stale_data vocabulary — only
"urgent" and "risk_off" are genuinely ABOUT elevated risk; the rest are
about data quality or ambiguity, a different concern this filter has no
mandate to act on (see _RISK_FILTER_NEWS_VETO_FLAGS/_RISK_FILTER_
MACRO_VETO_FLAGS below). An agent that never ran for a given candidate
can't veto it — absence of a risk signal is not itself a risk signal.

Because DIRECTION_SOURCES is consumed generically everywhere in this
module (compute_backtest_comparison's default source list,
run_paired_barrier_backtest, run_sensitivity_grid, compute_champion_
challenger_report, and their validation), adding this one entry means
every existing backtest-lite/paired/grid/champion-challenger endpoint
picks it up automatically, with the full existing win_rate/profit_
factor/CI95/median_pnl/max_drawdown/day-session reporting machinery
applied to it for free — no new endpoint, no new simulation logic, no
duplicated statistics code. Directly comparable against "analysis" (the
same direction calls, MINUS the veto) and "coordinator" (the live
blended policy) side by side in the same by_source report, which is
exactly the comparison the reviewer's request was for. Entirely
offline, no LLM calls, no new trades, COORDINATOR_THRESHOLD/WEIGHTS/
AUTO_EXECUTE_ENABLED untouched — this changes what a BACKTEST simulates
for one more hypothetical source, nothing about the live Coordinator or
Risk/Execution pipeline.

Tier 3.33 (exploratory 4-way factorial, eighth external review,
2026-08-25): Tier 3.31's veto-attribution report found "analysis_risk_
filtered" trading 254 candidates the live Coordinator wouldn't, and 90%
of that gap (131/145) came from bypassing the MIN_AVAILABLE_WEIGHT
quorum gate, not from the urgent/risk_off veto (10%, 14/145) or Timing
(0%, structurally). The reviewer's own next question: "analysis_risk_
filtered" still bundles the quorum-bypass effect and the veto-filter
effect into one policy, so its backtest numbers can't say which effect
— if either — actually helps. Two new DIRECTION_SOURCES entries isolate
them, so all four of live Coordinator / veto-only / quorum-bypass-only /
analysis_risk_filtered can be compared side by side through the exact
same backtest-lite/paired/grid/champion-challenger machinery, before
committing to registering any ONE of them as a confirmatory prospective
experiment (that registration decision is deliberately NOT made by this
tier — see the module comment at _direction_for_source's "coordinator_
quorum_bypass" case for why).

"coordinator_veto_filtered": the REAL historical Coordinator decision
(same as the "coordinator" source), with the urgent/risk_off veto
layered on top post-hoc — skipped entirely if Coordinator actually
traded but News's opinion carries "urgent" or Macro's carries
"risk_off" (_RISK_FILTER_NEWS_VETO_FLAGS/_RISK_FILTER_MACRO_VETO_FLAGS,
the same constants "analysis_risk_filtered" uses). No re-scoring
needed — quorum, weights, and Timing all stay exactly as the live
Coordinator computed them; ONLY the veto is added. This isolates the
veto-filter effect alone.

"coordinator_quorum_bypass": re-scores each candidate via app.replay.
replay_candidate() with min_available_weight=0.0 — the ONLY override;
weights/threshold/analysis_required are left at their LIVE values (not
overridden), so Timing's veto/dampen and the ANALYSIS_REQUIRED gate
both still apply exactly as they do for the real Coordinator. This
isolates the quorum-bypass effect alone, with NO new veto added — a
candidate that would fail quorum today (e.g. Analysis alone, News and
Macro both missing) gets a real blended score computed from whatever
directional agents ARE present instead of being forced to
insufficient_data.

Both are exploratory only, same as every other DIRECTION_SOURCES entry
— reusing existing backtest-lite/paired/grid/champion-challenger
reporting, no new statistics code, no live scoring change.
COORDINATOR_THRESHOLD/WEIGHTS/MIN_AVAILABLE_WEIGHT/AUTO_EXECUTE_ENABLED
all untouched (min_available_weight=0.0 is passed only to this one
OFFLINE replay call per candidate, never touching the live
app.coordinator.MIN_AVAILABLE_WEIGHT constant itself).
"""

import math
import os

from app.outcomes import _candidate_anchor_timestamp, _resolve_anchor_timestamp, compute_baseline_comparison
from app.paper_trades import COMMISSION_PER_CONTRACT, MNQ_POINT_VALUE, SLIPPAGE_POINTS
from app.replay import replay_candidate
from app.storage import get_bars_after
from app.trading_calendar import expected_trading_date

# Same "explicit env var, sane default" pattern every other tunable in
# this project follows (COORDINATOR_THRESHOLD, RISK_FRACTION_PER_TRADE,
# etc.) — these are backtest-lite specific and do NOT affect real
# trades or Execution's actual proposed geometry in any way.
ATR_STOP_MULT = float(os.environ.get("BACKTEST_ATR_STOP_MULT", "1.5"))
ATR_TARGET_MULT = float(os.environ.get("BACKTEST_ATR_TARGET_MULT", "2.5"))
EXPIRY_BARS = int(os.environ.get("BACKTEST_EXPIRY_BARS", "24"))

# Tier 3.23 (fifth external review — experiment registry hardening): a
# hand-maintained marker for the barrier-simulation LOGIC itself (fill/
# stop/target ordering, slippage/commission application, expiry
# handling — everything in run_barrier_backtest that ISN'T a config
# knob already covered by ATR_STOP_MULT/ATR_TARGET_MULT/EXPIRY_BARS/
# SLIPPAGE_POINTS/COMMISSION_PER_CONTRACT above). Bump this by hand
# whenever a change to that logic could alter results for candidates
# that were simulated before vs after the change — app.experiments
# locks this value at registration and flags a mismatch at resolution
# rather than silently blending two different simulation behaviors.
BACKTEST_LOGIC_VERSION = "1"


def _parse_float_list(env_value: str | None, default: tuple[float, ...]) -> tuple[float, ...]:
    if not env_value:
        return default
    return tuple(float(x.strip()) for x in env_value.split(",") if x.strip())


def _parse_int_list(env_value: str | None, default: tuple[int, ...]) -> tuple[int, ...]:
    if not env_value:
        return default
    return tuple(int(x.strip()) for x in env_value.split(",") if x.strip())


# Tier 3.14: the pre-registered sensitivity grid. Env-configurable
# (deploy-time only, NOT a query parameter) so the grid stays fixed
# across requests -- letting a caller pass an arbitrary grid per
# request would reopen the exact "keep trying configs until one looks
# good" risk this feature exists to guard against.
GRID_STOP_MULTS = _parse_float_list(os.environ.get("BACKTEST_GRID_STOP_MULTS"), (1.0, 1.5, 2.0))
GRID_TARGET_MULTS = _parse_float_list(os.environ.get("BACKTEST_GRID_TARGET_MULTS"), (1.5, 2.0, 2.5))
GRID_EXPIRY_BARS = _parse_int_list(os.environ.get("BACKTEST_GRID_EXPIRY_BARS"), (6, 12, 24))

DIRECTION_SOURCES = (
    "analysis",
    "coordinator",
    "inverse_analysis",
    "always_bullish",
    "always_bearish",
    "vwap",
    "analysis_risk_filtered",
    "coordinator_veto_filtered",
    "coordinator_quorum_bypass",
)

# Tier 3.30 — the exact two flags "analysis_risk_filtered" treats as a
# veto. Deliberately narrow: News's full vocabulary is "urgent"/
# "low_data"/"stale_data" and Macro's is "risk_off"/"conflicting_
# signals"/"stale_data" (see app/news_agent.py, app/macro_agent.py), but
# only "urgent" and "risk_off" are actually ABOUT elevated risk/
# volatility — "low_data"/"stale_data"/"conflicting_signals" are about
# data quality or ambiguity, a different concern a risk-FILTER has no
# stated mandate to act on. Project-owner decision (not a default this
# module invented on its own), confirmed against this exact vocabulary
# before building.
_RISK_FILTER_NEWS_VETO_FLAGS = frozenset({"urgent"})
_RISK_FILTER_MACRO_VETO_FLAGS = frozenset({"risk_off"})

_FLIP = {"bullish": "bearish", "bearish": "bullish"}


def _pnl_usd(direction: str, entry_price: float, exit_price: float, size: int) -> float:
    diff = (exit_price - entry_price) if direction == "bullish" else (entry_price - exit_price)
    return round(diff * MNQ_POINT_VALUE * size, 2)


def _round_trip_commission(size: int) -> float:
    return round(COMMISSION_PER_CONTRACT * size, 2)


def _apply_stop_slippage(raw_price: float, direction: str) -> float:
    return round(raw_price - SLIPPAGE_POINTS, 4) if direction == "bullish" else round(raw_price + SLIPPAGE_POINTS, 4)


def _apply_entry_slippage(raw_price: float, direction: str) -> float:
    """Every backtest-lite entry is treated as a market order (there's
    no LLM-proposed limit price to test against here — Execution's
    actual geometry isn't what this module benchmarks) — same
    always-market-order convention process_new_bar() applies slippage
    under."""
    return round(raw_price + SLIPPAGE_POINTS, 4) if direction == "bullish" else round(raw_price - SLIPPAGE_POINTS, 4)


def compute_atr_stop_target(
    direction: str,
    entry_price: float,
    atr: float,
    stop_mult: float = ATR_STOP_MULT,
    target_mult: float = ATR_TARGET_MULT,
) -> tuple[float, float]:
    """Deterministic, no LLM — the whole point of a cheap baseline
    geometry to test signals against."""
    if direction == "bullish":
        return round(entry_price - atr * stop_mult, 4), round(entry_price + atr * target_mult, 4)
    return round(entry_price + atr * stop_mult, 4), round(entry_price - atr * target_mult, 4)


def simulate_barrier_trade(
    direction: str,
    entry_price: float,
    stop_price: float,
    target_price: float,
    forward_bars: list[dict],
    size: int = 1,
    expiry_bars: int = EXPIRY_BARS,
) -> dict:
    """Walks forward_bars (ascending, strictly after the entry bar)
    applying the exact fill/stop/target/slippage/commission rules
    app/paper_trades.process_new_bar() uses for real trades — entry is
    assumed filled already (a market order at forward_bars[0]'s open,
    computed by the caller), so this only resolves the exit: whichever
    of stop/target is hit first, stop winning a same-bar tie (the
    project's standing "never assume the better outcome" convention),
    with gap-adjusted stop pricing and no favorable-gap credit at the
    target. Tracks MFE/MAE (points, in the trade's favor/against
    direction) across every bar actually walked, win or lose, for
    visibility beyond just the final exit.

    Returns exit_reason "stop_hit" / "target_hit" / "expired" (ran out
    of forward_bars or hit expiry_bars first) / "no_data" (a bar was
    missing OHLC — skipped, not counted as an exit)."""
    mfe_points = 0.0
    mae_points = 0.0
    bars_held = 0

    for bar in forward_bars[:expiry_bars]:
        high, low, open_ = bar.get("high"), bar.get("low"), bar.get("open")
        if high is None or low is None or open_ is None:
            continue
        bars_held += 1

        favorable = (high - entry_price) if direction == "bullish" else (entry_price - low)
        adverse = (entry_price - low) if direction == "bullish" else (high - entry_price)
        mfe_points = max(mfe_points, favorable)
        mae_points = max(mae_points, adverse)

        stop_hit = (low <= stop_price) if direction == "bullish" else (high >= stop_price)
        target_hit = (high >= target_price) if direction == "bullish" else (low <= target_price)

        if stop_hit:
            raw_exit = min(open_, stop_price) if direction == "bullish" else max(open_, stop_price)
            exit_price = _apply_stop_slippage(raw_exit, direction)
            pnl = _pnl_usd(direction, entry_price, exit_price, size) - _round_trip_commission(size)
            return {
                "exit_reason": "stop_hit", "exit_price": exit_price, "pnl_usd": pnl,
                "bars_held": bars_held, "mfe_points": round(mfe_points, 4), "mae_points": round(mae_points, 4),
                "exit_timestamp": bar.get("timestamp"),
            }
        if target_hit:
            exit_price = target_price
            pnl = _pnl_usd(direction, entry_price, exit_price, size) - _round_trip_commission(size)
            return {
                "exit_reason": "target_hit", "exit_price": exit_price, "pnl_usd": pnl,
                "bars_held": bars_held, "mfe_points": round(mfe_points, 4), "mae_points": round(mae_points, 4),
                "exit_timestamp": bar.get("timestamp"),
            }

    if bars_held == 0:
        return {
            "exit_reason": "no_data", "exit_price": None, "pnl_usd": None,
            "bars_held": 0, "mfe_points": None, "mae_points": None, "exit_timestamp": None,
        }

    # Expired unresolved — mark-to-last-seen-close for a defined (if
    # not realized) pnl_usd, same idea as a discretionary trader flat-
    # closing an overdue setup rather than holding it forever. That
    # close-out is itself a market order (Tier 3.12 fix — this was
    # previously exact-priced with no slippage, inconsistent with
    # every other exit type in this function), so the same
    # against-the-trader slippage applies here too.
    last_bar = None
    for bar in forward_bars[:expiry_bars]:
        if bar.get("close") is not None:
            last_bar = bar
    raw_exit_price = last_bar["close"] if last_bar else None
    exit_price = _apply_stop_slippage(raw_exit_price, direction) if raw_exit_price is not None else None
    pnl = _pnl_usd(direction, entry_price, exit_price, size) - _round_trip_commission(size) if exit_price is not None else None
    return {
        "exit_reason": "expired", "exit_price": exit_price, "pnl_usd": pnl,
        "bars_held": bars_held, "mfe_points": round(mfe_points, 4), "mae_points": round(mae_points, 4),
        "exit_timestamp": last_bar.get("timestamp") if last_bar else None,
    }


def _direction_for_source(source: str, candidate: dict) -> tuple[str | None, str | None]:
    """Returns (direction, anchor_timestamp) for the given
    direction_source, or (None, None) if this candidate has nothing
    usable for that source (e.g. no Analysis opinion, no VWAP
    distance, a no_trade Coordinator decision)."""
    decision = candidate.get("decision") or {}
    bar = candidate.get("bar") or {}
    opinions_used = decision.get("opinions_used") or {}
    analysis_opinion = opinions_used.get("analysis")

    if source == "coordinator":
        trade_decision = decision.get("decision")
        if trade_decision not in ("enter_long", "enter_short"):
            return None, None
        direction = "bullish" if trade_decision == "enter_long" else "bearish"
        return direction, _candidate_anchor_timestamp(candidate)

    if source == "analysis":
        if not analysis_opinion or analysis_opinion.get("direction") not in ("bullish", "bearish"):
            return None, None
        anchor = _resolve_anchor_timestamp("analysis", candidate, analysis_opinion, decision)
        return analysis_opinion.get("direction"), anchor

    if source == "inverse_analysis":
        if not analysis_opinion or analysis_opinion.get("direction") not in ("bullish", "bearish"):
            return None, None
        anchor = _resolve_anchor_timestamp("analysis", candidate, analysis_opinion, decision)
        return _FLIP[analysis_opinion["direction"]], anchor

    if source == "always_bullish":
        return "bullish", _candidate_anchor_timestamp(candidate)

    if source == "always_bearish":
        return "bearish", _candidate_anchor_timestamp(candidate)

    if source == "vwap":
        vwap_distance = bar.get("distance_from_vwap_points")
        if not vwap_distance:
            return None, None
        return ("bullish" if vwap_distance > 0 else "bearish"), _candidate_anchor_timestamp(candidate)

    if source == "analysis_risk_filtered":
        # Sixth external review, ranked backlog item #4: "a shadow
        # policy where Analysis alone decides direction and News/Macro
        # act only as risk filters" (i.e. they can VETO a trade, never
        # supply or shift its direction — no blending, no score). Same
        # direction call as the "analysis" source above; the only
        # difference is the veto below. An agent that never ran for
        # this candidate (missing/stale, no opinion at all) can't veto
        # anything — absence of a risk signal is not itself a risk
        # signal, so the trade proceeds exactly as "analysis" would.
        if not analysis_opinion or analysis_opinion.get("direction") not in ("bullish", "bearish"):
            return None, None
        news_opinion = opinions_used.get("news")
        if news_opinion and _RISK_FILTER_NEWS_VETO_FLAGS & set(news_opinion.get("flags") or []):
            return None, None
        macro_opinion = opinions_used.get("macro")
        if macro_opinion and _RISK_FILTER_MACRO_VETO_FLAGS & set(macro_opinion.get("flags") or []):
            return None, None
        anchor = _resolve_anchor_timestamp("analysis", candidate, analysis_opinion, decision)
        return analysis_opinion.get("direction"), anchor

    if source == "coordinator_veto_filtered":
        # Tier 3.33: isolates the veto-filter effect ALONE, no quorum
        # change — the real historical Coordinator decision, with the
        # same urgent/risk_off veto "analysis_risk_filtered" uses
        # layered on top post-hoc. No re-scoring: quorum/weights/Timing
        # all stay exactly as the live Coordinator computed them.
        trade_decision = decision.get("decision")
        if trade_decision not in ("enter_long", "enter_short"):
            return None, None
        news_opinion = opinions_used.get("news")
        if news_opinion and _RISK_FILTER_NEWS_VETO_FLAGS & set(news_opinion.get("flags") or []):
            return None, None
        macro_opinion = opinions_used.get("macro")
        if macro_opinion and _RISK_FILTER_MACRO_VETO_FLAGS & set(macro_opinion.get("flags") or []):
            return None, None
        direction = "bullish" if trade_decision == "enter_long" else "bearish"
        return direction, _candidate_anchor_timestamp(candidate)

    if source == "coordinator_quorum_bypass":
        # Tier 3.33: isolates the quorum-bypass effect ALONE, no new
        # veto — re-scores under the LIVE weights/threshold/
        # analysis_required (Timing's veto/dampen and the
        # ANALYSIS_REQUIRED gate both still apply exactly as they do for
        # the real Coordinator) but with min_available_weight=0.0, so a
        # candidate that would fail the 60% availability floor today
        # gets a real blended score from whatever directional agents ARE
        # present instead of being forced to insufficient_data. This
        # OFFLINE replay call never touches the live app.coordinator.
        # MIN_AVAILABLE_WEIGHT constant itself.
        replayed = replay_candidate(candidate, min_available_weight=0.0)["replayed"]
        trade_decision = replayed.get("decision")
        if trade_decision not in ("enter_long", "enter_short"):
            return None, None
        direction = "bullish" if trade_decision == "enter_long" else "bearish"
        return direction, _candidate_anchor_timestamp(candidate)

    raise ValueError(f"unknown direction_source {source!r} — must be one of {DIRECTION_SOURCES}")


def _wilson_score_interval(wins: int, n: int, z: float = 1.959964) -> tuple[float, float] | None:
    """95% Wilson score confidence interval on a binomial proportion
    (wins/n). Returns None when n == 0 (nothing decided yet).

    Deliberately NOT the plain normal-approximation interval
    (phat +/- z*sqrt(phat*(1-phat)/n)) -- that one is well known to
    misbehave (can go negative, or exceed 1, or be badly miscalibrated)
    at exactly the small-N regime this project actually has (single-
    digit to low-double-digit trade counts). Wilson's interval stays
    well-behaved down to very small n and is the standard correction
    for this, at the cost of being slightly more involved to compute."""
    if n == 0:
        return None
    phat = wins / n
    z2 = z * z
    denominator = 1 + z2 / n
    center = phat + z2 / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) / n) + (z2 / (4 * n * n)))
    lower = (center - margin) / denominator
    upper = (center + margin) / denominator
    return (round(max(0.0, lower), 4), round(min(1.0, upper), 4))


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return round(ordered[mid], 2)
    return round((ordered[mid - 1] + ordered[mid]) / 2, 2)


def _max_drawdown(pnl_sequence: list[float]) -> float:
    """Largest peak-to-trough dip in the running equity curve, in the
    order trades were actually taken (chronological -- pnl_sequence is
    appended in accepted-trade order, not sorted after the fact).
    Returns 0.0 for an empty or all-winning sequence (no drawdown)."""
    peak = 0.0
    running = 0.0
    max_dd = 0.0
    for pnl in pnl_sequence:
        running += pnl
        if running > peak:
            peak = running
        drawdown = peak - running
        if drawdown > max_dd:
            max_dd = drawdown
    return round(max_dd, 2)


def _candidate_trading_date(candidate: dict) -> str | None:
    """The CME/Globex trading day a candidate belongs to. Prefers the
    triggering bar's own `trading_date` field (Pine-Script-computed,
    already carries the correct session-rollover handling — Tier 2.9)
    over recomputing it, since that field is the actual wire value the
    real system ingested; only recomputes (via
    app.trading_calendar.expected_trading_date) from the candidate's
    own anchor timestamp when there's no stored bar to read it from
    (very old data, or the manual /coordinator/decide path)."""
    bar = candidate.get("bar") or {}
    trading_date = bar.get("trading_date")
    if trading_date:
        return trading_date
    anchor = _candidate_anchor_timestamp(candidate)
    if not anchor:
        return None
    try:
        return expected_trading_date(anchor)
    except (ValueError, AttributeError, TypeError):
        return None


# Tier 3.41 (fifteenth external review): a real correction to how this
# session had been reasoning about `limit`-bounded pulls. A limit=700
# pull earlier this tier returned one attribution subset complete
# (63/63) and another silently partial (55/58) in the SAME response —
# this session first mischaracterized that as a WebFetch artifact, but
# it's ordinary and entirely expected: `limit` restricts which
# candidates enter the computation at all, and different subsets can be
# complete or partial independently depending on how their candidates
# are distributed across the pulled window. The review's real point:
# there was no way to tell from the response ALONE whether a given pull
# was complete, short of manually cross-checking against a previously
# known total — and "limit=850 is reliable" is false as a standing
# rule, true only because total history happened to be under 850 at
# pull time. compute_data_range_metadata() makes this self-evident
# going forward: every response in this diagnostic family that accepts
# a `limit` param now also reports the TRUE total row count for this
# symbol/timeframe (a fresh COUNT(*), independent of `limit`), how many
# rows were actually returned, and an explicit `hit_limit_ceiling` flag
# — computed by comparing the two counts directly, never inferred from
# `returned_count == requested_limit` (which can coincide and mislead).
# This doesn't yet solve pulling more than 1000 candidates in one
# request (still capped by the endpoint's own `limit<=` constraint,
# raised this tier from 1000 to 5000 to buy headroom) — genuine
# pagination (since_rowid-style, matching app.experiments' existing
# rowid-boundary convention) is deferred until the population actually
# requires it, per the review's own three-option list.
def compute_data_range_metadata(candidates: list[dict], total_in_storage: int, requested_limit: int) -> dict:
    returned_count = len(candidates)
    timestamps = [t for t in (_candidate_anchor_timestamp(c) for c in candidates) if t]
    trading_dates = {d for d in (_candidate_trading_date(c) for c in candidates) if d}
    return {
        "total_candidates_in_storage": total_in_storage,
        "requested_limit": requested_limit,
        "returned_count": returned_count,
        "hit_limit_ceiling": total_in_storage > returned_count,
        "earliest_candidate_timestamp": min(timestamps) if timestamps else None,
        "latest_candidate_timestamp": max(timestamps) if timestamps else None,
        "distinct_trading_days_in_window": len(trading_dates),
    }


def compute_day_session_breakdown(candidates: list[dict]) -> dict:
    """Tier 3.18: how many genuinely independent trading days/sessions
    a candidate set actually spans — see the module docstring's Tier
    3.18 paragraph for why this matters. Read-only, offline, no new
    data — walks fields already stored on each candidate."""
    trading_dates: list[str] = []
    per_day: dict[str, int] = {}
    by_session_name: dict[str, int] = {}
    by_timing_session_label: dict[str, int] = {}
    unknown_trading_date_count = 0

    for candidate in candidates:
        trading_date = _candidate_trading_date(candidate)
        if trading_date:
            trading_dates.append(trading_date)
            per_day[trading_date] = per_day.get(trading_date, 0) + 1
        else:
            unknown_trading_date_count += 1

        bar = candidate.get("bar") or {}
        session_name = bar.get("session_name")
        if session_name:
            by_session_name[session_name] = by_session_name.get(session_name, 0) + 1

        timing_context = (candidate.get("decision") or {}).get("timing_context") or {}
        session_label = timing_context.get("session_label")
        if session_label:
            by_timing_session_label[session_label] = by_timing_session_label.get(session_label, 0) + 1

    counts_per_day = list(per_day.values())
    return {
        "candidates_considered": len(candidates),
        "distinct_trading_days": len(per_day),
        "candidates_per_day": {
            "min": min(counts_per_day) if counts_per_day else None,
            "median": _median([float(c) for c in counts_per_day]),
            "max": max(counts_per_day) if counts_per_day else None,
        },
        "by_session_name": by_session_name,
        "by_timing_session_label": by_timing_session_label,
        "unknown_trading_date_count": unknown_trading_date_count,
    }


# Tier 3.19: capped so a large candidate history doesn't return an
# unbounded mismatch_examples payload. mismatch_count itself is never
# capped -- only the illustrative example list.
TRADING_DATE_MISMATCH_EXAMPLE_LIMIT = 20


def compute_trading_date_integrity_report(candidates: list[dict]) -> dict:
    """Tier 3.19: cross-checks every candidate's bar's own payload
    trading_date against a freshly recomputed one (same convention
    app.trading_calendar.check_trading_date() already applies at
    webhook ingestion, just re-run here so the result is aggregable
    instead of living only in a per-event log line/response field),
    plus a third, fully independent plain-UTC-calendar-date view with
    no NY-timezone/session-rollover adjustment at all. See the module
    docstring's Tier 3.19 paragraph for the full rationale. Read-only,
    offline -- no new data, no scoring change."""
    payload_trading_dates: dict[str, int] = {}
    computed_trading_dates: dict[str, int] = {}
    utc_calendar_dates: dict[str, int] = {}
    mismatch_examples: list[dict] = []
    mismatch_count = 0
    candidates_missing_bar = 0
    candidates_bar_missing_trading_date = 0
    anchor_timestamps: list[str] = []

    for candidate in candidates:
        has_bar = bool(candidate.get("bar"))
        bar = candidate.get("bar") or {}
        anchor = _candidate_anchor_timestamp(candidate)
        if anchor:
            anchor_timestamps.append(anchor)

        if not has_bar:
            candidates_missing_bar += 1

        payload_date = bar.get("trading_date") if has_bar else None
        if payload_date:
            payload_trading_dates[payload_date] = payload_trading_dates.get(payload_date, 0) + 1
        elif has_bar:
            candidates_bar_missing_trading_date += 1

        computed_date = None
        if anchor:
            try:
                computed_date = expected_trading_date(anchor)
            except (ValueError, AttributeError, TypeError):
                computed_date = None
        if computed_date:
            computed_trading_dates[computed_date] = computed_trading_dates.get(computed_date, 0) + 1

        # Deliberately NOT run through expected_trading_date()'s NY-
        # timezone/rollover logic -- this is meant to be a fully
        # independent third view, not a second computation of the
        # same thing under a different name. Timestamps are already
        # ISO-8601 UTC ("...T...Z"), so the date portion is a plain
        # slice, no timezone conversion needed.
        utc_date = anchor[:10] if anchor else None
        if utc_date:
            utc_calendar_dates[utc_date] = utc_calendar_dates.get(utc_date, 0) + 1

        if payload_date and computed_date and payload_date != computed_date:
            mismatch_count += 1
            if len(mismatch_examples) < TRADING_DATE_MISMATCH_EXAMPLE_LIMIT:
                mismatch_examples.append(
                    {
                        "candidate_id": candidate.get("candidate_id"),
                        "event_id": bar.get("event_id"),
                        "anchor_timestamp": anchor,
                        "payload_trading_date": payload_date,
                        "computed_trading_date": computed_date,
                    }
                )

    return {
        "candidates_considered": len(candidates),
        "candidates_missing_bar": candidates_missing_bar,
        "candidates_bar_missing_trading_date": candidates_bar_missing_trading_date,
        "payload_trading_dates": payload_trading_dates,
        "computed_trading_dates": computed_trading_dates,
        "utc_calendar_dates": utc_calendar_dates,
        "distinct_payload_trading_days": len(payload_trading_dates),
        "distinct_computed_trading_days": len(computed_trading_dates),
        "distinct_utc_calendar_dates": len(utc_calendar_dates),
        "mismatch_count": mismatch_count,
        "mismatch_examples": mismatch_examples,
        "mismatch_examples_truncated": mismatch_count > len(mismatch_examples),
        "earliest_anchor_timestamp": min(anchor_timestamps) if anchor_timestamps else None,
        "latest_anchor_timestamp": max(anchor_timestamps) if anchor_timestamps else None,
    }


def _empty_summary() -> dict:
    return {
        "trades_taken": 0,
        "skipped_no_direction": 0,
        "skipped_no_atr": 0,
        "skipped_overlapping": 0,
        "skipped_no_forward_data": 0,
        "wins": 0,
        "losses": 0,
        "breakeven": 0,
        "expired": 0,
        "total_pnl_usd": 0.0,
        "gross_profit_usd": 0.0,
        "gross_loss_usd": 0.0,
        "win_rate": None,
        "win_rate_ci95_low": None,
        "win_rate_ci95_high": None,
        "profit_factor": None,
        "avg_pnl_usd": None,
        "median_pnl_usd": None,
        "max_drawdown_usd": None,
        "trades": [],
        "_pnl_sequence": [],
    }


def _accumulate_trade_result(summary: dict, result: dict) -> None:
    """Shared win/loss/pnl bookkeeping for one resolved trade — used by
    both run_barrier_backtest (independent per-source schedule) and
    run_paired_barrier_backtest (Tier 3.12, one shared schedule), so
    the two can never silently drift apart on how a result is scored."""
    summary["trades_taken"] += 1
    pnl = result["pnl_usd"]
    if pnl is not None:
        summary["total_pnl_usd"] = round(summary["total_pnl_usd"] + pnl, 2)
        summary["_pnl_sequence"].append(pnl)
        if pnl > 0:
            summary["wins"] += 1
            summary["gross_profit_usd"] = round(summary["gross_profit_usd"] + pnl, 2)
        elif pnl < 0:
            summary["losses"] += 1
            summary["gross_loss_usd"] = round(summary["gross_loss_usd"] + abs(pnl), 2)
        else:
            summary["breakeven"] += 1
    if result["exit_reason"] == "expired":
        summary["expired"] += 1


def _finalize_summary(summary: dict) -> None:
    """Shared tail computation (win_rate/profit_factor/avg_pnl_usd plus
    the Tier 3.13 small-sample additions), in place — same reasoning
    both callers share: profit_factor is null (not a misleading
    infinity) when there are no losses to divide by, and
    win_rate/avg_pnl_usd stay null until something has actually
    resolved to a decided win/loss. Pops the internal _pnl_sequence
    scratch list before returning -- it's bookkeeping only, never part
    of the public summary shape."""
    decided = summary["wins"] + summary["losses"]
    if decided > 0:
        summary["win_rate"] = round(summary["wins"] / decided, 4)
        ci = _wilson_score_interval(summary["wins"], decided)
        if ci is not None:
            summary["win_rate_ci95_low"], summary["win_rate_ci95_high"] = ci
    if summary["gross_loss_usd"] > 0:
        summary["profit_factor"] = round(summary["gross_profit_usd"] / summary["gross_loss_usd"], 4)
    elif summary["gross_profit_usd"] > 0:
        summary["profit_factor"] = None
    if summary["trades_taken"] > 0:
        summary["avg_pnl_usd"] = round(summary["total_pnl_usd"] / summary["trades_taken"], 2)
    pnl_sequence = summary.pop("_pnl_sequence")
    summary["median_pnl_usd"] = _median(pnl_sequence)
    summary["max_drawdown_usd"] = _max_drawdown(pnl_sequence)


def run_barrier_backtest(
    candidates: list[dict],
    direction_source: str,
    stop_mult: float = ATR_STOP_MULT,
    target_mult: float = ATR_TARGET_MULT,
    expiry_bars: int = EXPIRY_BARS,
    non_overlapping: bool = True,
    size: int = 1,
    include_trades: bool = False,
) -> dict:
    """Runs the ATR-barrier simulation for ONE direction_source across
    a list of candidates (oldest-first ordering not required — this
    sorts by anchor timestamp itself so callers can pass candidates in
    whatever order get_candidate_history() returns them). Returns
    aggregate stats; per-trade detail only included when
    include_trades=True (kept off by default for WebFetch-reliability,
    same constraint that's shaped every other endpoint in this
    project queried against production through this session)."""
    dated: list[tuple[str, dict]] = []
    for candidate in candidates:
        direction, anchor_timestamp = _direction_for_source(direction_source, candidate)
        if direction is None or not anchor_timestamp:
            continue
        dated.append((anchor_timestamp, candidate, direction))
    dated.sort(key=lambda t: t[0])

    summary = _empty_summary()
    skipped_no_direction = len(candidates) - len(dated)
    summary["skipped_no_direction"] = skipped_no_direction

    blocked_until: str | None = None

    for anchor_timestamp, candidate, direction in dated:
        if non_overlapping and blocked_until is not None and anchor_timestamp < blocked_until:
            summary["skipped_overlapping"] += 1
            continue

        bar = candidate.get("bar") or {}
        atr = bar.get("atr")
        if not atr or atr <= 0:
            summary["skipped_no_atr"] += 1
            continue

        forward = get_bars_after(candidate["symbol"], candidate["timeframe"], anchor_timestamp, limit=expiry_bars)
        if not forward:
            summary["skipped_no_forward_data"] += 1
            continue

        entry_bar = forward[0]
        raw_entry_price = entry_bar.get("open")
        if raw_entry_price is None:
            summary["skipped_no_forward_data"] += 1
            continue
        # Stop/target are sized off the intended (pre-slippage) entry
        # level — same "the plan is made against a clean price, the
        # realized fill may differ slightly" split Execution/Risk
        # already follow for real trades. Slippage only affects the
        # realized fill price used for P&L below, not the geometry.
        stop_price, target_price = compute_atr_stop_target(direction, raw_entry_price, atr, stop_mult, target_mult)
        entry_price = _apply_entry_slippage(raw_entry_price, direction)
        # The fill bar's own high/low are ALSO checked for an
        # immediate stop/target touch — same same-bar-check convention
        # process_new_bar() uses (a market order fills at the bar's
        # open, then that same bar's range is checked against stop/
        # target right away, not starting only on the NEXT bar).
        result = simulate_barrier_trade(
            direction=direction, entry_price=entry_price, stop_price=stop_price, target_price=target_price,
            forward_bars=forward, size=size, expiry_bars=expiry_bars,
        )

        if result["exit_reason"] == "no_data":
            summary["skipped_no_forward_data"] += 1
            continue

        _accumulate_trade_result(summary, result)
        blocked_until = result.get("exit_timestamp") or anchor_timestamp

        if include_trades:
            summary["trades"].append({
                "anchor_timestamp": anchor_timestamp,
                "direction": direction,
                "entry_price": entry_price,
                "stop_price": stop_price,
                "target_price": target_price,
                **result,
            })

    _finalize_summary(summary)
    return summary


def compute_backtest_comparison(
    candidates: list[dict],
    sources: list[str] | None = None,
    stop_mult: float = ATR_STOP_MULT,
    target_mult: float = ATR_TARGET_MULT,
    expiry_bars: int = EXPIRY_BARS,
    non_overlapping: bool = True,
) -> dict:
    """Runs run_barrier_backtest() once per requested direction_source
    and returns them side by side. ATR geometry and expiry are held
    fixed across every source, but — flagged by a second external
    review pass (2026-08-14, reviewing Tiers 3.10/3.11's own results)
    — each source runs its OWN independent non-overlapping schedule,
    so different sources end up trading DIFFERENT subsets of
    `candidates` (a source with faster-resolving trades reopens sooner
    and accepts more candidates than one that ties up longer). This is
    a legitimate "policy comparison" — it measures each source's full
    strategy, abstention/blocking included — but it is NOT a clean
    "same population, only the direction differs" comparison, and a
    difference in results is NOT attributable to the direction signal
    alone. For that, use run_paired_barrier_backtest() below (Tier
    3.12), which intersects every source onto one shared, direction-
    independent candidate set and schedule before simulating."""
    sources = sources or list(DIRECTION_SOURCES)
    return {
        "config": {
            "atr_stop_mult": stop_mult, "atr_target_mult": target_mult,
            "expiry_bars": expiry_bars, "non_overlapping": non_overlapping,
            "candidates_considered": len(candidates),
        },
        "day_session": compute_day_session_breakdown(candidates),
        "by_source": {
            source: run_barrier_backtest(
                candidates, direction_source=source, stop_mult=stop_mult, target_mult=target_mult,
                expiry_bars=expiry_bars, non_overlapping=non_overlapping,
            )
            for source in sources
        },
    }


# ---------------------------------------------------------------------------
# Tier 3.12: paired signal comparison + holdout-boundary embargo
# ---------------------------------------------------------------------------
#
# A second external review pass (2026-08-14), reviewing Tier 3.10/3.11's
# OWN results, found two real methodological gaps rather than just
# reacting to the numbers:
#
# 1. compute_backtest_comparison() (Tier 3.10) runs each source's
#    non-overlapping schedule independently, so different sources end
#    up trading different candidate subsets (confirmed directly: of
#    156 candidates, coordinator took 15, analysis 22, inverse_analysis
#    21, always_bullish 22, always_bearish 28 — not the "same
#    population" the original docstring claimed). run_paired_barrier_
#    backtest() below fixes this: it intersects every requested source
#    onto ONE shared eligible-candidate set (only candidates every
#    source can score) and ONE shared non-overlap schedule computed
#    from the shared entry/expiry window itself (not from any one
#    source's win/loss resolution time, which would just reintroduce
#    the same bias under a different name) — so a difference in
#    results really is attributable to the direction signal alone.
#
# 2. split_candidates_chronologically() (Tier 3.11) could let a
#    calibration-window candidate's forward barrier walk extend past
#    the validation cutoff — its own reported P&L would then partially
#    reflect price action from the nominally held-out period. This
#    doesn't invalidate a VALIDATION-window result (validation trades
#    only ever look forward from their own, later anchors), but it
#    does mean calibration's numbers weren't as cleanly separated from
#    validation as claimed. split_candidates_chronologically() now
#    takes expiry_bars and purges (embargoes) any calibration candidate
#    whose forward window would cross the validation boundary, so
#    "calibration" and "validation" are genuinely disjoint in the
#    price data they use, not just in which candidates they start from.

def run_paired_barrier_backtest(
    candidates: list[dict],
    sources: list[str],
    stop_mult: float = ATR_STOP_MULT,
    target_mult: float = ATR_TARGET_MULT,
    expiry_bars: int = EXPIRY_BARS,
    size: int = 1,
    include_trades: bool = False,
) -> dict:
    """The paired counterpart to compute_backtest_comparison(): every
    requested source is simulated on the exact SAME candidates, the
    exact same entry price, and the exact same non-overlap schedule —
    only the direction (and therefore stop/target sign) differs. A
    candidate is only eligible at all if EVERY requested source can
    resolve a direction for it (e.g. always_bullish/always_bearish
    are eligible everywhere, but "analysis"/"inverse_analysis" need a
    real Analysis opinion and "coordinator" needs a directional
    decision — the eligible set is the intersection, which can be
    noticeably smaller than any single source's own eligible set).

    The shared non-overlap schedule paces by the full expiry_bars
    window from each ACCEPTED candidate's own anchor — not by when any
    particular source's trade actually resolved, which would silently
    let one source's win/loss timing set the pace for every source
    again (the exact bug this function exists to avoid)."""
    if len(sources) < 1:
        raise ValueError("run_paired_barrier_backtest needs at least one source")
    unknown = [s for s in sources if s not in DIRECTION_SOURCES]
    if unknown:
        raise ValueError(f"unknown source(s) {unknown} — must be one of {DIRECTION_SOURCES}")

    prepared: list[tuple[str, dict, dict[str, str], float]] = []
    for candidate in candidates:
        anchor_timestamp = _candidate_anchor_timestamp(candidate)
        if not anchor_timestamp:
            continue
        bar = candidate.get("bar") or {}
        atr = bar.get("atr")
        if not atr or atr <= 0:
            continue
        directions = {source: _direction_for_source(source, candidate)[0] for source in sources}
        if any(d is None for d in directions.values()):
            continue  # not eligible for ALL requested sources -- paired mode requires full intersection
        prepared.append((anchor_timestamp, candidate, directions, atr))
    prepared.sort(key=lambda t: t[0])

    eligible_candidates = len(prepared)
    summaries = {source: _empty_summary() for source in sources}
    accepted_candidates = 0
    skipped_overlapping = 0
    skipped_no_forward_data = 0

    blocked_until: str | None = None
    for anchor_timestamp, candidate, directions, atr in prepared:
        if blocked_until is not None and anchor_timestamp < blocked_until:
            skipped_overlapping += 1
            continue

        forward = get_bars_after(candidate["symbol"], candidate["timeframe"], anchor_timestamp, limit=expiry_bars)
        if not forward:
            skipped_no_forward_data += 1
            continue
        raw_entry_price = forward[0].get("open")
        if raw_entry_price is None:
            skipped_no_forward_data += 1
            continue

        accepted_candidates += 1
        # Shared schedule: pace by the full expiry window every source
        # was given, not by any one source's actual resolution time.
        blocked_until = forward[-1].get("timestamp") or anchor_timestamp

        for source in sources:
            direction = directions[source]
            stop_price, target_price = compute_atr_stop_target(direction, raw_entry_price, atr, stop_mult, target_mult)
            entry_price = _apply_entry_slippage(raw_entry_price, direction)
            result = simulate_barrier_trade(
                direction=direction, entry_price=entry_price, stop_price=stop_price, target_price=target_price,
                forward_bars=forward, size=size, expiry_bars=expiry_bars,
            )
            summary = summaries[source]
            if result["exit_reason"] == "no_data":
                continue
            _accumulate_trade_result(summary, result)
            if include_trades:
                summary["trades"].append({
                    "anchor_timestamp": anchor_timestamp, "direction": direction,
                    "entry_price": entry_price, "stop_price": stop_price, "target_price": target_price,
                    **result,
                })

    for source in sources:
        summary = summaries[source]
        summary["skipped_overlapping"] = skipped_overlapping
        summary["skipped_no_forward_data"] = skipped_no_forward_data
        _finalize_summary(summary)

    return {
        "config": {
            "atr_stop_mult": stop_mult, "atr_target_mult": target_mult, "expiry_bars": expiry_bars,
            "candidates_considered": len(candidates),
            "eligible_candidates": eligible_candidates,
            "accepted_candidates": accepted_candidates,
        },
        "day_session": compute_day_session_breakdown(candidates),
        "sources": sources,
        "by_source": summaries,
    }


# ---------------------------------------------------------------------------
# Tier 3.11: champion/challenger out-of-sample evaluation
# ---------------------------------------------------------------------------
#
# Tier 3.10's own first real result (inverse_analysis showing the only
# profit_factor > 1 among six sources tested) is exactly the kind of
# finding the external review warned about: it was found by looking at
# the same historical sample it would be used to justify a change
# against. A pattern "discovered" and "validated" on identical data is
# not validated at all — it's fitting to history. The review's
# explicit recommendation was a champion/challenger design evaluated
# strictly on data NOT used to find the pattern.
#
# split_candidates_chronologically() and compute_champion_challenger_report()
# below are the harness for that: hold out the most RECENT slice of
# candidate history (never a random split — a random split lets a
# challenger "see" the future relative to a calibration-window
# decision, and lets time-correlated regimes leak across the split),
# run the exact same run_barrier_backtest() machinery Tier 3.10 built
# on BOTH the calibration (older) and validation (held-out, newer)
# windows independently for the champion and every requested
# challenger, and report both side by side rather than collapsing them
# into a single pass/fail verdict — a rigid threshold would be its own
# kind of overfitting at this sample size. A challenger whose edge
# holds up on the validation window is meaningfully different evidence
# than one that only looked good on the window used to notice it.

DEFAULT_HOLDOUT_FRACTION = float(os.environ.get("BACKTEST_HOLDOUT_FRACTION", "0.3"))


def split_candidates_chronologically(
    candidates: list[dict],
    holdout_fraction: float = DEFAULT_HOLDOUT_FRACTION,
    expiry_bars: int | None = None,
) -> tuple[list[dict], list[dict]]:
    """Sorts candidates by their own anchor timestamp (oldest first,
    same anchor logic every other function in this module uses —
    candidates with no resolvable anchor are dropped, same as
    run_barrier_backtest already does implicitly per-source) and
    splits them into (calibration, validation): the earliest
    (1 - holdout_fraction) as calibration, the most recent
    holdout_fraction as validation. Never a random split — see the
    module note above for why.

    Tier 3.12 (boundary embargo): pass expiry_bars to additionally
    purge any calibration candidate whose own forward barrier walk
    would extend PAST the validation cutoff — without this, a
    calibration candidate anchored near the boundary could resolve
    (or expire) using bars that fall inside the nominally held-out
    validation window, so calibration's own reported P&L would
    partially reflect price action from the period it's supposed to
    be excluded from. This does NOT affect validation's own integrity
    (a validation-window trade only ever looks forward from its own,
    later anchor — never backward into calibration), only how cleanly
    separated calibration's numbers are. Omit expiry_bars (the
    default) to skip this check — cheaper, but not embargoed."""
    if not (0.0 < holdout_fraction < 1.0):
        raise ValueError(f"holdout_fraction must be between 0 and 1 (exclusive), got {holdout_fraction!r}")

    dated = [
        (anchor, candidate)
        for candidate in candidates
        for anchor in [_candidate_anchor_timestamp(candidate)]
        if anchor
    ]
    dated.sort(key=lambda pair: pair[0])

    split_index = round(len(dated) * (1 - holdout_fraction))
    calibration_dated = dated[:split_index]
    validation = [candidate for _, candidate in dated[split_index:]]

    if expiry_bars and calibration_dated and validation:
        cutoff = dated[split_index][0]  # first validation candidate's anchor
        embargoed = []
        for anchor, candidate in calibration_dated:
            forward = get_bars_after(candidate["symbol"], candidate["timeframe"], anchor, limit=expiry_bars)
            resolves_by = forward[-1]["timestamp"] if forward else anchor
            if resolves_by < cutoff:
                embargoed.append(candidate)
        calibration = embargoed
    else:
        calibration = [candidate for _, candidate in calibration_dated]

    return calibration, validation


def compute_champion_challenger_report(
    candidates: list[dict],
    champion: str = "coordinator",
    challengers: list[str] | None = None,
    holdout_fraction: float = DEFAULT_HOLDOUT_FRACTION,
    stop_mult: float = ATR_STOP_MULT,
    target_mult: float = ATR_TARGET_MULT,
    expiry_bars: int = EXPIRY_BARS,
    non_overlapping: bool = True,
) -> dict:
    """The actual champion/challenger report: champion is the
    currently-live decision source (default "coordinator" — the real
    system), challengers are any other direction_source(s) being
    considered as a replacement or input to one (default: every other
    entry in DIRECTION_SOURCES). Every source is run TWICE — once on
    the calibration window, once on the held-out validation window —
    with the exact same ATR/expiry/non-overlap config both times, so a
    reader can see directly whether a challenger's apparent edge
    survives on data it was never fitted to, or was only ever visible
    on the window used to spot it in the first place.

    Tier 3.12: the split now embargoes any calibration candidate whose
    forward barrier walk would cross the validation boundary (see
    split_candidates_chronologically's own docstring) — reported here
    as "purged_at_boundary" for transparency, same "no silent caps"
    principle every other endpoint in this project follows. Also adds
    a "base_rate" section per window (app.outcomes.compute_baseline_
    comparison, Tier 3.8's own always-bullish/always-bearish market
    base-rate check) — a quick, independent way to see whether the
    calibration and validation windows were simply different market
    regimes (e.g. validation trending more bullish than calibration),
    which would help explain a source's numbers moving between windows
    without any of its own signal quality having actually changed.

    This function only reports; it never picks a winner or flips
    anything — matches Tier 3.10's inverse_analysis staying purely
    diagnostic, and the standing project rule that any real trading-
    logic change needs the user's explicit direction."""
    if champion not in DIRECTION_SOURCES:
        raise ValueError(f"unknown champion {champion!r} — must be one of {DIRECTION_SOURCES}")
    challengers = challengers if challengers is not None else [s for s in DIRECTION_SOURCES if s != champion]
    for c in challengers:
        if c not in DIRECTION_SOURCES:
            raise ValueError(f"unknown challenger {c!r} — must be one of {DIRECTION_SOURCES}")

    calibration_unembargoed, _ = split_candidates_chronologically(candidates, holdout_fraction)
    calibration, validation = split_candidates_chronologically(candidates, holdout_fraction, expiry_bars=expiry_bars)
    purged_at_boundary = len(calibration_unembargoed) - len(calibration)

    def _run(pool: list[dict], source: str) -> dict:
        return run_barrier_backtest(
            pool, direction_source=source, stop_mult=stop_mult, target_mult=target_mult,
            expiry_bars=expiry_bars, non_overlapping=non_overlapping,
        )

    sources_to_run = [champion] + [c for c in challengers if c != champion]
    return {
        "config": {
            "atr_stop_mult": stop_mult, "atr_target_mult": target_mult,
            "expiry_bars": expiry_bars, "non_overlapping": non_overlapping,
            "holdout_fraction": holdout_fraction,
            "candidates_considered": len(candidates),
            "calibration_candidates": len(calibration),
            "validation_candidates": len(validation),
            "purged_at_boundary": purged_at_boundary,
        },
        "champion": champion,
        "challengers": [c for c in challengers if c != champion],
        "base_rate": {
            "calibration": compute_baseline_comparison(calibration),
            "validation": compute_baseline_comparison(validation),
        },
        "day_session": {
            "calibration": compute_day_session_breakdown(calibration),
            "validation": compute_day_session_breakdown(validation),
        },
        "by_source": {
            source: {
                "calibration": _run(calibration, source),
                "validation": _run(validation, source),
            }
            for source in sources_to_run
        },
    }


# ---------------------------------------------------------------------------
# Tier 3.14: pre-registered parameter sensitivity grid
# ---------------------------------------------------------------------------

_GRID_SUMMARY_FIELDS = (
    "trades_taken",
    "win_rate",
    "win_rate_ci95_low",
    "win_rate_ci95_high",
    "profit_factor",
    "total_pnl_usd",
)


def _summarize_robustness(combo_results: list[dict]) -> dict:
    """How consistent a source's results are across the whole grid --
    a real edge should hold up across MOST reasonable geometries, not
    just look good on the one config that happened to get tested
    first."""
    win_rates = sorted(r["win_rate"] for r in combo_results if r["win_rate"] is not None)
    median_win_rate = None
    if win_rates:
        n = len(win_rates)
        mid = n // 2
        median_win_rate = round(win_rates[mid] if n % 2 == 1 else (win_rates[mid - 1] + win_rates[mid]) / 2, 4)
    pnls = [r["total_pnl_usd"] for r in combo_results]
    return {
        "combinations_run": len(combo_results),
        "combinations_with_positive_pnl": sum(1 for p in pnls if p > 0),
        "combinations_with_profit_factor_above_1": sum(
            1 for r in combo_results if r["profit_factor"] is not None and r["profit_factor"] > 1
        ),
        "median_win_rate_across_grid": median_win_rate,
        "min_total_pnl_usd": min(pnls) if pnls else None,
        "max_total_pnl_usd": max(pnls) if pnls else None,
    }


def run_sensitivity_grid(
    candidates: list[dict],
    sources: list[str],
    stop_mults: tuple[float, ...] = GRID_STOP_MULTS,
    target_mults: tuple[float, ...] = GRID_TARGET_MULTS,
    expiry_bars_list: tuple[int, ...] = GRID_EXPIRY_BARS,
    size: int = 1,
) -> dict:
    """Runs run_paired_barrier_backtest() (the corrected, Tier 3.12
    paired comparison) once per (stop_mult, target_mult, expiry_bars)
    combination in the pre-registered grid, for every requested
    source. Each combination's per-source result is compacted to just
    the fields relevant to judging robustness (trades_taken, win_rate
    + its CI, profit_factor, total_pnl_usd) rather than the full
    summary shape, to stay a reasonable response size across a grid
    this size. Same validation as the paired endpoint: at least one
    recognized source required."""
    if len(sources) < 1:
        raise ValueError("run_sensitivity_grid needs at least one source")
    unknown = [s for s in sources if s not in DIRECTION_SOURCES]
    if unknown:
        raise ValueError(f"unknown source(s) {unknown} — must be one of {DIRECTION_SOURCES}")

    combinations: dict[str, dict] = {}
    per_source_results: dict[str, list[dict]] = {source: [] for source in sources}

    for stop_mult in stop_mults:
        for target_mult in target_mults:
            for expiry_bars in expiry_bars_list:
                paired = run_paired_barrier_backtest(
                    candidates, sources, stop_mult=stop_mult, target_mult=target_mult,
                    expiry_bars=expiry_bars, size=size,
                )
                combo_key = f"stop{stop_mult}x_target{target_mult}x_expiry{expiry_bars}b"
                combo_by_source = {}
                for source in sources:
                    full = paired["by_source"][source]
                    compact = {field: full[field] for field in _GRID_SUMMARY_FIELDS}
                    combo_by_source[source] = compact
                    per_source_results[source].append(compact)
                combinations[combo_key] = {
                    "stop_mult": stop_mult,
                    "target_mult": target_mult,
                    "expiry_bars": expiry_bars,
                    "accepted_candidates": paired["config"]["accepted_candidates"],
                    "by_source": combo_by_source,
                }

    return {
        "grid": {
            "stop_mults": list(stop_mults),
            "target_mults": list(target_mults),
            "expiry_bars": list(expiry_bars_list),
            "total_combinations": len(combinations),
        },
        "day_session": compute_day_session_breakdown(candidates),
        "sources": sources,
        "robustness": {source: _summarize_robustness(results) for source, results in per_source_results.items()},
        "combinations": combinations,
    }


# ---------------------------------------------------------------------------
# Tier 3.39: factorial incremental P&L for the urgent/risk_off veto
# ---------------------------------------------------------------------------
#
# Chain of external reviews leading here: Tier 3.34/3.35 (ninth/tenth
# reviews) found the urgent/risk_off hard veto would kill 56.5% of
# Coordinator's real directional decisions, sharply skewed toward
# shorts. The eleventh/twelfth/thirteenth reviews progressively
# corrected the framing — normalizing against each direction's own
# base rate (Tier 3.37: risk_off kills 73.3% of shorts vs 2.2% of
# longs), then separating solo/overlap contribution (thirteenth review:
# risk_off ALONE, ignoring urgent entirely, still kills 34.2% of shorts
# vs 2.2% of longs — the union/combined-policy number of 79.5%/27.5%
# double-counts the 63 candidates both flags would have killed anyway).
# Every one of these was a DIRECTION-count or KILL-RATE diagnostic —
# none of them touch the one question that actually decides whether the
# veto helps or hurts the account: would the killed decisions have won
# or lost under the SAME realistic trading rules real Coordinator trades
# use?
#
# This is NOT an "entirely offline" diagnostic like the rest of this
# module — it runs the real ATR-barrier simulation (run_barrier_
# backtest, same engine every other backtest endpoint in this project
# uses) against real stored forward price bars, so results depend on
# what candles Actually happened after each candidate. No new
# simulation logic is introduced: every P&L figure below comes from
# run_barrier_backtest(..., direction_source="coordinator", ...) called
# against a pre-filtered candidate SUBSET — the four veto policies and
# every breakdown differ only in WHICH candidates are included, never in
# how a trade is priced, sized, or scheduled. Same stop_mult/
# target_mult/expiry_bars, same slippage/commission constants, same
# forward-bar source, for every policy and every breakdown — the
# thirteenth review's explicit "identical trading mechanics across
# policies" requirement.
#
# Four fixed policies (thirteenth review, exact spec): "none" (no veto
# at all — every real Coordinator directional decision, the pre-veto
# baseline), "urgent_only" (drop a candidate only if News carries
# "urgent", risk_off is ignored), "risk_off_only" (drop only if Macro
# carries "risk_off", urgent is ignored), "both" (drop if EITHER flag is
# present — this is the SAME candidate set Tier 3.33's
# `coordinator_veto_filtered` direction_source already selects, just
# computed independently here rather than reusing that source, to keep
# every policy's candidate-selection logic visible in one place).
#
# Three views, per policy: `decision_level` (every candidate simulated
# independently, non_overlapping=False — acknowledged NOT to represent
# 299 independent trading opportunities, since consecutive candidates
# from the same move are correlated and many would never coexist under
# real one-position-at-a-time scheduling); `portfolio_level` (the SAME
# non_overlapping=True scheduling every other backtest endpoint in this
# project uses, run independently per policy — the economically real
# number). CAUTION: each view's own `short`/`long` sub-keys are
# SEPARATELY non-overlap-scheduled subsets, not a decomposition of
# `overall` — `overall` reflects real single-position competition
# BETWEEN directions (a long and a short can never both be open), so
# `short.trades_taken + long.trades_taken` can exceed `overall.
# trades_taken` at portfolio level. This is documented, not a bug —
# useful for isolating each direction's own numbers, but `overall` is
# the only "if we ran this policy live" figure.
#
# `attribution`: risk_off's SOLO effect (`risk_off_solo_excluded` —
# risk_off present, urgent absent), urgent's SOLO effect
# (`urgent_solo_excluded`), the two flags' OVERLAP (`both_excluded_
# overlap`), and the UNION (`any_excluded_union` — either flag present,
# what a hard veto with no schema change would drop as a whole). These
# four candidate sets are mutually exclusive except union = solo ∪ solo
# ∪ overlap, so summing solo+solo+overlap P&L equals union P&L exactly
# — no double-counting the 63 overlap candidates into both flags'
# "effect" the way a naive "risk_off_implicated" vs "urgent_implicated"
# comparison would (the thirteenth review's central correction).
#
# `macro_direction_breakdown`: the risk_off-flagged population (any
# candidate risk_off is present on, regardless of urgent) split by
# Macro's OWN direction on that candidate — decision-level P&L per
# bucket, continuing Tier 3.36's endogeneity question with real outcome
# data instead of just counts.
#
# `day_session_breakdown`: distinct-opinion/distinct-day counts (Tier
# 3.36's own diversity convention) plus a by-session decision-level P&L
# split, for both the risk_off-flagged and urgent-flagged populations.
#
# `conservative_opinion_level`: the thirteenth review's explicit
# disambiguation — "first candidate per (trading_date, agent_opinion_
# timestamp)" (one observation per independent judgment call PER DAY)
# and the stricter "first candidate per agent_opinion_timestamp,
# globally" (one observation per independent judgment call across the
# ENTIRE history, collapsing a reused opinion spanning multiple days
# into one). Both dedupe by keeping the chronologically EARLIEST
# candidate in each group (ties broken by anchor_timestamp string
# comparison) — decision-level P&L only; a schedule-level view on an
# already-deduped, already-sparse set adds a second confound without
# adding real information.
#
# Tier 3.40 (fourteenth external review): Package #14's first production
# pull mislabeled the "both" counterfactual as "(live policy)" — the
# review caught this and it was independently confirmed against source
# as wrong: the live Coordinator applies News' "urgent" as a SOFT 0.5x
# score dampener (app/coordinator.py, unconditional whenever "urgent" is
# set), never a hard veto, and "risk_off" is never referenced in live
# coordinator/execution code at all — it exists only in macro_agent.py's
# prompt schema and this diagnostic family. The registered
# analysis_risk_filtered shadow experiment (watermark 950) is a third,
# separate mechanism (follows Analysis's own direction, bypasses
# quorum). None of the four policies this module computes have EVER
# been live in any form — they are all counterfactuals; every summary
# dict below documents this explicitly rather than implying otherwise.
#
# The same review also asked for day/opinion diversity figures across
# the FULL 4x3 policy matrix (not just the flagged populations
# day_session_breakdown already covers) and a specific joint-opinion-pair
# count for the 63-candidate overlap subset, to test the new "urgent+
# risk_off agreement may mark genuinely tradeable moves" hypothesis
# without over-trusting a small, possibly low-diversity sample. Every
# summary dict `_run()` produces (policies, attribution, macro_direction_
# breakdown, day_session_breakdown's session buckets, conservative_
# opinion_level) now additionally reports: `distinct_trading_days`,
# `distinct_news_opinions`, `distinct_macro_opinions` (each computed the
# same way Tier 3.36 already does elsewhere in this project — count
# distinct non-null values, never guessing a missing identity into a
# bucket), and `distinct_joint_news_macro_opinions` (distinct (news_
# opinion_timestamp, macro_opinion_timestamp) PAIRS — a candidate
# missing either timestamp is excluded from this count entirely, same
# convention _first_per_group already uses). For the `both_excluded_
# overlap` set specifically, `distinct_joint_news_macro_opinions` is the
# number the review asked for directly: how many genuinely independent
# (News, Macro) judgment-call PAIRS produced the 63 overlap candidates,
# as opposed to "16 opinions" counted per-flag separately without
# knowing how many distinct combinations those 16 actually form.
# `max_drawdown_usd` was already computed by run_barrier_backtest/
# _finalize_summary in every summary dict before this tier — it was
# simply never pulled from production; no code change was needed to
# expose it, only to remember to ask for it.

_VETO_POLICIES = ("none", "urgent_only", "risk_off_only", "both")


def _veto_pnl_population(candidates: list[dict]) -> list[dict]:
    """Same analysis-directional + coordinator-traded population
    app.coordinator_diagnostics.compute_veto_decision_transitions()
    scopes to (the union of its coordinator_trade_veto_would_skip and
    coordinator_trade_veto_survives transitions): every candidate with
    a directional Analysis opinion AND a real Coordinator enter_long/
    enter_short decision. Returns the ORIGINAL candidate dicts (not a
    distilled summary) since run_barrier_backtest() needs the full
    candidate shape to fetch forward bars and read `bar.atr`."""
    population = []
    for candidate in candidates:
        if not candidate.get("candidate_id"):
            continue
        decision = candidate.get("decision") or {}
        opinions_used = decision.get("opinions_used") or {}
        analysis_opinion = opinions_used.get("analysis")
        if not analysis_opinion or analysis_opinion.get("direction") not in ("bullish", "bearish"):
            continue
        if decision.get("decision") not in ("enter_long", "enter_short"):
            continue
        population.append(candidate)
    return population


def _veto_pnl_flags(candidate: dict) -> dict:
    """Per-candidate flag/attribute extraction, same implicated-flag
    definitions app.coordinator_diagnostics.compute_veto_decision_
    transitions() uses. Kept independently here (not imported) since
    app.coordinator_diagnostics already imports FROM this module —
    importing back would create a cycle."""
    decision = candidate.get("decision") or {}
    bar = candidate.get("bar") or {}
    opinions_used = decision.get("opinions_used") or {}
    news_opinion = opinions_used.get("news")
    macro_opinion = opinions_used.get("macro")
    news_urgent = bool(news_opinion and _RISK_FILTER_NEWS_VETO_FLAGS & set(news_opinion.get("flags") or []))
    macro_risk_off = bool(macro_opinion and _RISK_FILTER_MACRO_VETO_FLAGS & set(macro_opinion.get("flags") or []))
    trade_decision = decision.get("decision")
    return {
        "direction": "bullish" if trade_decision == "enter_long" else "bearish",
        "news_urgent": news_urgent,
        "macro_risk_off": macro_risk_off,
        "macro_direction": (macro_opinion or {}).get("direction"),
        "news_opinion_timestamp": (news_opinion or {}).get("timestamp"),
        "macro_opinion_timestamp": (macro_opinion or {}).get("timestamp"),
        "trading_date": bar.get("trading_date"),
        "session_name": bar.get("session_name"),
        "anchor_timestamp": _candidate_anchor_timestamp(candidate),
    }


def _policy_included(policy: str, news_urgent: bool, macro_risk_off: bool) -> bool:
    if policy == "none":
        return True
    if policy == "urgent_only":
        return not news_urgent
    if policy == "risk_off_only":
        return not macro_risk_off
    if policy == "both":
        return not news_urgent and not macro_risk_off
    raise ValueError(f"unknown policy {policy!r} — must be one of {_VETO_POLICIES}")


def _distinct_count(meta: dict, ids: set, field: str) -> int:
    return len({meta[cid][field] for cid in ids if meta[cid].get(field) is not None})


def _distinct_pair_count(meta: dict, ids: set, fields: tuple) -> int:
    """Tier 3.40 — distinct combinations of `fields` (e.g. (news_opinion_
    timestamp, macro_opinion_timestamp)) among `ids`. A candidate missing
    ANY of the given fields is excluded from the count entirely, not
    guessed into a bucket — same convention _first_per_group already
    uses for the same reason."""
    pairs = set()
    for cid in ids:
        candidate_meta = meta[cid]
        key = tuple(candidate_meta.get(f) for f in fields)
        if any(v is None for v in key):
            continue
        pairs.add(key)
    return len(pairs)


def _first_per_group(meta: dict, ids: set, group_fields: tuple) -> set:
    """Dedupes `ids` down to one candidate per distinct combination of
    group_fields, keeping the chronologically EARLIEST candidate (by
    anchor_timestamp) in each group. A candidate missing any field in
    group_fields is excluded entirely, not guessed into a bucket —
    same convention this project's other aggregators already use."""
    best_by_group: dict[tuple, tuple[str, str]] = {}
    for cid in ids:
        candidate_meta = meta[cid]
        group_key = tuple(candidate_meta.get(f) for f in group_fields)
        if any(v is None for v in group_key):
            continue
        anchor = candidate_meta.get("anchor_timestamp") or ""
        current = best_by_group.get(group_key)
        if current is None or anchor < current[0]:
            best_by_group[group_key] = (anchor, cid)
    return {cid for _, cid in best_by_group.values()}


def compute_veto_incremental_pnl(
    candidates: list[dict],
    stop_mult: float = ATR_STOP_MULT,
    target_mult: float = ATR_TARGET_MULT,
    expiry_bars: int = EXPIRY_BARS,
) -> dict:
    """Tier 3.39 (thirteenth external review), extended Tier 3.40
    (fourteenth review) — see the module comment block above for the
    full design rationale. Runs the SAME barrier simulation (run_
    barrier_backtest, direction_source="coordinator") against
    pre-filtered candidate subsets for each of the four fixed
    COUNTERFACTUAL veto policies (`none`/`urgent_only`/`risk_off_only`/
    `both` — none of these have ever been live; the real Coordinator
    applies "urgent" as a soft 0.5x score dampener and never applies
    "risk_off" at all), at both `decision_level` (non_overlapping=False)
    and `portfolio_level` (non_overlapping=True, independently scheduled
    per policy) — plus a solo/overlap/union `attribution` split, a
    `macro_direction_breakdown` of the risk_off-flagged population, a
    `day_session_breakdown`, and a `conservative_opinion_level` view
    deduped to one candidate per independent opinion. Every summary dict
    everywhere in the response also reports `distinct_trading_days`/
    `distinct_news_opinions`/`distinct_macro_opinions`/`distinct_joint_
    news_macro_opinions` (Tier 3.40) alongside its P&L figures, so no
    figure can be read without its underlying sample diversity in the
    same place. NOT entirely offline — performs real forward-bar
    lookups and barrier simulations per candidate, same performance
    profile as any other backtest endpoint (can be slower than this
    diagnostic family's other, purely-offline endpoints on a large
    population, since it runs roughly 40-50 separate backtest passes
    internally across all the breakdowns)."""
    population = _veto_pnl_population(candidates)
    candidates_by_id = {c["candidate_id"]: c for c in population}
    meta = {cid: _veto_pnl_flags(c) for cid, c in candidates_by_id.items()}
    all_ids = set(candidates_by_id.keys())

    short_ids = {cid for cid in all_ids if meta[cid]["direction"] == "bearish"}
    long_ids = {cid for cid in all_ids if meta[cid]["direction"] == "bullish"}
    urgent_ids = {cid for cid in all_ids if meta[cid]["news_urgent"]}
    risk_off_ids = {cid for cid in all_ids if meta[cid]["macro_risk_off"]}
    both_ids = urgent_ids & risk_off_ids
    urgent_solo_ids = urgent_ids - risk_off_ids
    risk_off_solo_ids = risk_off_ids - urgent_ids
    any_excluded_ids = urgent_ids | risk_off_ids

    def _run(ids: set, non_overlapping: bool) -> dict:
        subset = [candidates_by_id[cid] for cid in ids]
        summary = run_barrier_backtest(
            subset, direction_source="coordinator",
            stop_mult=stop_mult, target_mult=target_mult, expiry_bars=expiry_bars,
            non_overlapping=non_overlapping, include_trades=False,
        )
        summary.pop("trades", None)
        summary["candidates_in_subset"] = len(ids)
        # Tier 3.40 (fourteenth external review): day/opinion diversity
        # alongside every P&L figure, so a reader never has to guess
        # whether a result rests on a handful of reused judgment calls.
        summary["distinct_trading_days"] = _distinct_count(meta, ids, "trading_date")
        summary["distinct_news_opinions"] = _distinct_count(meta, ids, "news_opinion_timestamp")
        summary["distinct_macro_opinions"] = _distinct_count(meta, ids, "macro_opinion_timestamp")
        summary["distinct_joint_news_macro_opinions"] = _distinct_pair_count(
            meta, ids, ("news_opinion_timestamp", "macro_opinion_timestamp"),
        )
        return summary

    policy_ids = {
        policy: {cid for cid in all_ids if _policy_included(policy, meta[cid]["news_urgent"], meta[cid]["macro_risk_off"])}
        for policy in _VETO_POLICIES
    }

    decision_level = {}
    portfolio_level = {}
    for policy in _VETO_POLICIES:
        ids = policy_ids[policy]
        decision_level[policy] = {
            "overall": _run(ids, non_overlapping=False),
            "short": _run(ids & short_ids, non_overlapping=False),
            "long": _run(ids & long_ids, non_overlapping=False),
        }
        portfolio_level[policy] = {
            "overall": _run(ids, non_overlapping=True),
            "short": _run(ids & short_ids, non_overlapping=True),
            "long": _run(ids & long_ids, non_overlapping=True),
        }

    attribution = {
        "risk_off_solo_excluded": {
            "decision_level": _run(risk_off_solo_ids, non_overlapping=False),
            "portfolio_level": _run(risk_off_solo_ids, non_overlapping=True),
        },
        "urgent_solo_excluded": {
            "decision_level": _run(urgent_solo_ids, non_overlapping=False),
            "portfolio_level": _run(urgent_solo_ids, non_overlapping=True),
        },
        "both_excluded_overlap": {
            "decision_level": _run(both_ids, non_overlapping=False),
            "portfolio_level": _run(both_ids, non_overlapping=True),
        },
        "any_excluded_union": {
            "decision_level": _run(any_excluded_ids, non_overlapping=False),
            "portfolio_level": _run(any_excluded_ids, non_overlapping=True),
        },
    }

    macro_direction_groups: dict[str, set] = {}
    for cid in risk_off_ids:
        key = meta[cid]["macro_direction"] or "neutral"
        macro_direction_groups.setdefault(key, set()).add(cid)
    macro_direction_breakdown = {
        direction: {
            "candidates": len(ids),
            "distinct_opinions": _distinct_count(meta, ids, "macro_opinion_timestamp"),
            "distinct_trading_days": _distinct_count(meta, ids, "trading_date"),
            "decision_level": _run(ids, non_overlapping=False),
        }
        for direction, ids in macro_direction_groups.items()
    }

    def _session_breakdown(ids: set) -> dict:
        by_session: dict[str, set] = {}
        for cid in ids:
            session = meta[cid]["session_name"] or "unknown"
            by_session.setdefault(session, set()).add(cid)
        return {
            session: {"candidates": len(session_ids), "decision_level": _run(session_ids, non_overlapping=False)}
            for session, session_ids in by_session.items()
        }

    day_session_breakdown = {
        "risk_off_excluded": {
            "distinct_trading_days": _distinct_count(meta, risk_off_ids, "trading_date"),
            "distinct_opinions": _distinct_count(meta, risk_off_ids, "macro_opinion_timestamp"),
            "by_session_name": _session_breakdown(risk_off_ids),
        },
        "urgent_excluded": {
            "distinct_trading_days": _distinct_count(meta, urgent_ids, "trading_date"),
            "distinct_opinions": _distinct_count(meta, urgent_ids, "news_opinion_timestamp"),
            "by_session_name": _session_breakdown(urgent_ids),
        },
    }

    def _conservative(ids: set, opinion_field: str) -> dict:
        by_day_and_opinion = _first_per_group(meta, ids, ("trading_date", opinion_field))
        global_by_opinion = _first_per_group(meta, ids, (opinion_field,))
        return {
            "candidates_before_dedup": len(ids),
            "first_per_day_and_opinion": {
                "candidates_after_dedup": len(by_day_and_opinion),
                "decision_level": _run(by_day_and_opinion, non_overlapping=False),
            },
            "first_per_opinion_global": {
                "candidates_after_dedup": len(global_by_opinion),
                "decision_level": _run(global_by_opinion, non_overlapping=False),
            },
        }

    conservative_opinion_level = {
        "risk_off_excluded": _conservative(risk_off_ids, "macro_opinion_timestamp"),
        "urgent_excluded": _conservative(urgent_ids, "news_opinion_timestamp"),
    }

    return {
        "config": {"stop_mult": stop_mult, "target_mult": target_mult, "expiry_bars": expiry_bars},
        "population": {
            "candidates_considered": len(candidates),
            "coordinator_traded_population": len(all_ids),
            "short": len(short_ids),
            "long": len(long_ids),
        },
        "policies": list(_VETO_POLICIES),
        "decision_level": decision_level,
        "portfolio_level": portfolio_level,
        "attribution": attribution,
        "macro_direction_breakdown": macro_direction_breakdown,
        "day_session_breakdown": day_session_breakdown,
        "conservative_opinion_level": conservative_opinion_level,
    }
