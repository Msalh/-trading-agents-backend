"""
Experiment Registry — Tier 3.20 (fourth external review, 2026-08-18),
hardened in Tier 3.23 (fifth external review, 2026-08-19), extended in
Tier 3.24 (analysis_required, project-owner design decision, 2026-08-19).

Every finding this project has produced so far (Tiers 3.10-3.19) has
been RETROSPECTIVE: run a report against whatever candidates already
exist, look at the number, decide what it means. The fourth review
named the problem with continuing to do only that forever: this
project also keeps a weekly scheduled check (Tier 3.18) watching the
same growing candidate pool toward a pre-registered 15-distinct-day
threshold, and keeps building new diagnostics (Tier 3.16/3.17/3.19)
that mine that same pool for insight. By the time the 15-day threshold
fires, every candidate in it will already have been looked at,
summarized, and used to generate ideas multiple times over — it will
NOT be a clean holdout sample by any normal definition, even though
nothing about scoring/weights ever changed. Continuing indefinitely in
this mode means there's no future point at which "let's see if it
still holds" means anything, because every future data point is also
a past data point some report already used.

This module is the fix: a lightweight, append-only mechanism to
PRE-REGISTER a hypothesis, freeze the exact scoring config (weights/
threshold/min_available_weight) it applies to, and mark a hard
boundary — originally a registered_at timestamp, now (Tier 3.23) a
registered_watermark_rowid — such that only candidates created AT OR
AFTER that boundary ever count toward that experiment's stopping rule
or its eventual outcome. Candidates from before registration are
explicitly "exploratory": already mined, fine to keep looking at for
ideas, but never eligible to confirm or refute a hypothesis registered
after they were already visible. This is standard pre-registration
practice (the same discipline behind clinical-trial protocols and
registered-report science), scaled down to the smallest version that
still has the property that matters: no peeking, no re-drawing the
"trial period" after seeing how it turned out.

Honest naming (Tier 3.23, per the fifth review's point (f)): this is a
PROSPECTIVE EXPERIMENT REGISTRY WITH ONE-TIME AGGREGATE RESOLUTION, not
yet a full APPEND-ONLY SHADOW EVALUATION ENGINE — resolve_experiment()
re-simulates the whole prospective window in one shot when called, it
does not maintain a running per-candidate outcome ledger as each
candidate resolves. That distinction matters if the review's later
item (a genuine shadow ledger) ever gets built — this module is a
real, useful step toward it, not the same thing under a bigger name.

Reuses ALREADY-TESTED reporting machinery (app.backtest.
compute_backtest_comparison) and app.replay.replay_candidate() rather
than reimplementing any scoring or simulation.

Tier 3.23 (fifth external review — the registry's locked_config was
recorded but not actually enforced) fixes six concrete gaps the review
found, in the same registry it originally praised for the pre-
registration IDEA but flagged as incomplete in the EXECUTION:

  (a) locked_config is now actually USED, not just stored. If
      direction_source == "coordinator", every prospective candidate is
      re-scored via app.replay.replay_candidate() under the frozen
      weights/threshold/min_available_weight BEFORE any backtest runs
      — so a live config change mid-experiment can never silently blend
      two different scoring configs into one resolution. (For the
      other direction_source values — analysis/inverse_analysis/
      always_bullish/always_bearish/vwap — Coordinator weights never
      entered the picture in the first place, so there is nothing to
      re-score; this is documented, not silently skipped.)
  (b) Backtest GEOMETRY is now part of locked_config too:
      atr_stop_mult/atr_target_mult/expiry_bars/non_overlapping are
      captured at registration and threaded through to
      compute_backtest_comparison() at evaluation/resolution time (all
      four are already real parameters of that function — this was a
      wiring gap, not a missing feature). slippage_points/
      commission_per_contract/backtest_logic_version are ALSO captured,
      but app.backtest's simulation currently reads those from module-
      level constants rather than accepting them as parameters — a
      genuine open item, not silently hidden: evaluate_stopping_rule()/
      resolve_experiment() both report a loud `geometry_drift` field
      if the LIVE values differ from what was locked, rather than
      quietly using whichever happens to be live.
  (c) target_metrics is now a STRUCTURED, validated commitment (primary
      metric + comparator + success_threshold + optional secondary
      metrics) instead of a free-text list nothing ever checked against
      — resolve_experiment() computes and reports whether the primary
      metric actually met its pre-registered bar.
  (d) The no-peeking boundary is now registered_watermark_rowid (a
      monotonic integer, captured via storage.get_max_candidate_rowid()
      at registration) instead of a second-precision registered_at
      string comparison — no same-second tie is possible.
  (e) _prospective_candidates() no longer applies any "newest N" limit
      that could silently drop the OLDEST prospective candidates once a
      long-running experiment's window grows past it — it fetches
      EVERY candidate after the watermark. EXPERIMENT_MAX_PROSPECTIVE_
      CANDIDATES is a hard safety ceiling that raises loudly instead.

Lifecycle, unchanged in shape from Tier 3.20:
  1. register_experiment() — freezes hypothesis text, structured
     target_metrics, a stopping_rule, a snapshot of the CURRENT live
     scoring config + backtest geometry, and the current
     registered_watermark_rowid. Nothing about this call touches live
     scoring or geometry; it only records what they happened to be at
     this moment, for later comparison and re-scoring.
  2. evaluate_stopping_rule() — read-only, callable any number of
     times, never mutates anything.
  3. resolve_experiment() — the ONE-TIME action. Refuses if the
     stopping rule isn't met. If already resolved, returns the
     existing resolution untouched.

Tier 3.24: coordinator.ANALYSIS_REQUIRED (explicit "no trade without a
current Analysis opinion" gate, a project-owner design decision, not
data-driven) is now a fifth locked scoring knob in locked_config,
enforced at re-scoring time exactly like coordinator_threshold/weights/
min_available_weight — see coordinator.py's Tier 3.24 docstring section
for the full reasoning. Experiments registered before this tier default
to analysis_required=True on read (the only value it has ever actually
had live), not silently to False.

Entirely additive: no existing endpoint's behavior changes for anyone
not using experiments, no COORDINATOR_THRESHOLD/WEIGHTS touched (only
read and snapshotted), no LLM calls. Table: app.storage `experiments`.
Endpoints: POST /experiments, GET /experiments, GET /experiments/{id},
POST /experiments/{id}/resolve — see app/main.py.
"""

import os
import uuid

from app.backtest import (
    ATR_STOP_MULT,
    ATR_TARGET_MULT,
    BACKTEST_LOGIC_VERSION,
    DIRECTION_SOURCES,
    EXPIRY_BARS,
    compute_backtest_comparison,
)
from app.coordinator import ANALYSIS_REQUIRED, DECISION_THRESHOLD, MIN_AVAILABLE_WEIGHT, WEIGHTS
from app.paper_trades import COMMISSION_PER_CONTRACT, SLIPPAGE_POINTS
from app.replay import replay_candidate
from app.storage import (
    count_candidates_after_rowid,
    get_candidates_after_rowid,
    get_experiment_by_id,
    get_experiments,
    get_max_candidate_rowid,
    save_experiment,
)
from app.storage import resolve_experiment as _persist_resolution

# Tier 3.23: a hard safety ceiling on how many prospective candidates
# one evaluate/resolve call will pull into memory and simulate. Unlike
# the old EXPERIMENT_HISTORY_LIMIT (Tier 3.20), hitting this raises
# ExperimentError loudly rather than silently truncating the window —
# see the module docstring's point (e). Env-configurable like every
# other tunable in this project.
EXPERIMENT_MAX_PROSPECTIVE_CANDIDATES = int(os.environ.get("EXPERIMENT_MAX_PROSPECTIVE_CANDIDATES", "20000"))

_STOPPING_RULE_KEYS = ("min_distinct_trading_days", "min_accepted_trades")

# Tier 3.23: the only backtest-summary fields (app.backtest._empty_summary)
# a target_metrics primary/secondary metric is allowed to name — keeps
# resolve_experiment() from computing "success" against a made-up key
# that silently reads as None forever.
VALID_TARGET_METRIC_KEYS = (
    "win_rate", "profit_factor", "avg_pnl_usd", "median_pnl_usd",
    "total_pnl_usd", "max_drawdown_usd", "trades_taken", "wins", "losses",
)
VALID_COMPARATORS = (">=", "<=", ">", "<", "==")


class ExperimentError(Exception):
    pass


def _new_experiment_id() -> str:
    return str(uuid.uuid4())


def _current_locked_config() -> dict:
    """A snapshot of the live scoring config AND backtest geometry,
    frozen at registration time -- read-only, never mutates any of the
    underlying module-level values themselves. See the module
    docstring's point (b) for which of these are actually enforced at
    evaluation/resolution time (scoring + pure-geometry knobs) versus
    only drift-checked (slippage/commission/logic version).

    Tier 3.24: analysis_required joins coordinator_threshold/weights/
    min_available_weight as a fourth locked, ENFORCED scoring knob
    (threaded into _rescore_under_locked_config's replay_candidate call
    below) -- see coordinator.ANALYSIS_REQUIRED for what it gates.
    Experiments registered before Tier 3.24 have no this key in their
    stored locked_config; _rescore_under_locked_config below defaults
    that case to True, which is the only value ANALYSIS_REQUIRED has
    ever actually had in production, so this is a safe backfill, not a
    guess."""
    return {
        "coordinator_threshold": DECISION_THRESHOLD,
        "weights": dict(WEIGHTS),
        "min_available_weight": MIN_AVAILABLE_WEIGHT,
        "analysis_required": ANALYSIS_REQUIRED,
        "backtest_geometry": {
            "atr_stop_mult": ATR_STOP_MULT,
            "atr_target_mult": ATR_TARGET_MULT,
            "expiry_bars": EXPIRY_BARS,
            "non_overlapping": True,
            "slippage_points": SLIPPAGE_POINTS,
            "commission_per_contract": COMMISSION_PER_CONTRACT,
            "backtest_logic_version": BACKTEST_LOGIC_VERSION,
        },
    }


def _validate_target_metrics(target_metrics: dict) -> dict:
    """Tier 3.23: target_metrics is now a structured commitment, not a
    free-text list. Requires a primary_metric (one of
    VALID_TARGET_METRIC_KEYS), a comparator (one of VALID_COMPARATORS),
    and a numeric success_threshold -- exactly what resolve_experiment()
    needs to compute a real met/not-met answer, not just "some numbers
    were reported." secondary_metrics is optional, reported but not
    gated."""
    if not isinstance(target_metrics, dict):
        raise ExperimentError("target_metrics must be an object with primary_metric/comparator/success_threshold")
    primary_metric = target_metrics.get("primary_metric")
    if primary_metric not in VALID_TARGET_METRIC_KEYS:
        raise ExperimentError(f"target_metrics.primary_metric must be one of {VALID_TARGET_METRIC_KEYS}")
    comparator = target_metrics.get("comparator")
    if comparator not in VALID_COMPARATORS:
        raise ExperimentError(f"target_metrics.comparator must be one of {VALID_COMPARATORS}")
    success_threshold = target_metrics.get("success_threshold")
    if not isinstance(success_threshold, (int, float)) or isinstance(success_threshold, bool):
        raise ExperimentError("target_metrics.success_threshold must be a number")
    secondary_metrics = target_metrics.get("secondary_metrics") or []
    unknown = [m for m in secondary_metrics if m not in VALID_TARGET_METRIC_KEYS]
    if unknown:
        raise ExperimentError(f"target_metrics.secondary_metrics has unknown metric(s) {unknown}, expected one of {VALID_TARGET_METRIC_KEYS}")
    return {
        "primary_metric": primary_metric,
        "comparator": comparator,
        "success_threshold": float(success_threshold),
        "secondary_metrics": list(secondary_metrics),
    }


def register_experiment(
    symbol: str,
    timeframe: str,
    hypothesis: str,
    target_metrics: dict,
    stopping_rule: dict,
    direction_source: str = "coordinator",
) -> dict:
    """Validates and persists a new pre-registered experiment. Raises
    ExperimentError on anything that would make the eventual
    resolution meaningless (no hypothesis text, an unstructured/invalid
    target_metrics, no usable stopping rule) rather than silently
    registering a vague experiment nobody could later evaluate against
    its own stated purpose."""
    hypothesis = (hypothesis or "").strip()
    if not hypothesis:
        raise ExperimentError(
            "hypothesis must be a non-empty description of what this experiment is testing"
        )
    validated_target_metrics = _validate_target_metrics(target_metrics)
    if not stopping_rule or not any(k in stopping_rule for k in _STOPPING_RULE_KEYS):
        raise ExperimentError(
            f"stopping_rule must set at least one of {_STOPPING_RULE_KEYS}"
        )
    for key in stopping_rule:
        if key not in _STOPPING_RULE_KEYS:
            raise ExperimentError(f"unknown stopping_rule key {key!r}, expected one of {_STOPPING_RULE_KEYS}")
    if direction_source not in DIRECTION_SOURCES:
        raise ExperimentError(f"unknown direction_source {direction_source!r}, expected one of {DIRECTION_SOURCES}")

    return save_experiment(
        experiment_id=_new_experiment_id(),
        symbol=symbol,
        timeframe=timeframe,
        hypothesis=hypothesis,
        locked_config=_current_locked_config(),
        target_metrics=validated_target_metrics,
        stopping_rule=dict(stopping_rule),
        direction_source=direction_source,
        # Tier 3.23: the real no-peeking boundary -- see module docstring point (d).
        registered_watermark_rowid=get_max_candidate_rowid(symbol, timeframe),
    )


def _prospective_candidates(experiment: dict) -> list:
    """Candidates with rowid STRICTLY AFTER this experiment's
    registered_watermark_rowid -- the only ones ever allowed to count
    toward its stopping rule or its resolution (Tier 3.23, module
    docstring points (d)/(e)). Raises ExperimentError, rather than
    silently truncating, if the prospective window has grown past
    EXPERIMENT_MAX_PROSPECTIVE_CANDIDATES."""
    watermark = experiment["registered_watermark_rowid"]
    count = count_candidates_after_rowid(experiment["symbol"], experiment["timeframe"], watermark)
    if count > EXPERIMENT_MAX_PROSPECTIVE_CANDIDATES:
        raise ExperimentError(
            f"prospective window for experiment {experiment['experiment_id']!r} has grown to "
            f"{count} candidates, past EXPERIMENT_MAX_PROSPECTIVE_CANDIDATES="
            f"{EXPERIMENT_MAX_PROSPECTIVE_CANDIDATES} -- refusing to silently truncate; "
            f"raise the limit (env var) if this experiment is intentionally this large"
        )
    return get_candidates_after_rowid(experiment["symbol"], experiment["timeframe"], watermark)


def _rescore_under_locked_config(candidates: list, experiment: dict) -> list:
    """Tier 3.23, module docstring point (a): if this experiment's
    direction_source is "coordinator", every candidate's stored
    decision was computed under WHATEVER scoring config was live at
    the moment it was created -- which may not be this experiment's
    locked_config if live weights/threshold/min_available_weight
    changed mid-experiment. Re-scores each one via
    app.replay.replay_candidate() under the FROZEN config instead of
    trusting the stored decision, and substitutes the replayed decision
    in a shallow-copied candidate dict (app.backtest only ever reads
    candidate["decision"] and candidate["bar"] for direction-source
    purposes, so this is a safe, minimal substitution).

    For every OTHER direction_source (analysis/inverse_analysis/
    always_bullish/always_bearish/vwap), Coordinator weights never
    entered the picture in the first place -- app.backtest reads
    Analysis's own opinion or the bar's own VWAP distance directly, not
    a Coordinator score -- so re-scoring would be a no-op that costs a
    replay call for nothing. Candidates are returned unchanged."""
    if experiment["direction_source"] != "coordinator":
        return candidates
    locked = experiment["locked_config"]
    rescored = []
    for candidate in candidates:
        replayed = replay_candidate(
            candidate,
            weights=locked["weights"],
            threshold=locked["coordinator_threshold"],
            min_available_weight=locked["min_available_weight"],
            # Tier 3.24: pre-3.24 experiments' locked_config has no
            # this key -- True is a backfill, not a guess (see
            # _current_locked_config's docstring).
            analysis_required=locked.get("analysis_required", True),
        )
        rescored.append({**candidate, "decision": replayed["replayed"]})
    return rescored


def _geometry_drift(locked_config: dict) -> dict | None:
    """Tier 3.23, module docstring point (b): slippage_points/
    commission_per_contract/backtest_logic_version aren't wired through
    compute_backtest_comparison() as parameters (app.backtest reads
    them from module-level constants at simulation time) -- rather than
    silently using whatever happens to be live, this reports a loud
    drift flag whenever they no longer match what was locked at
    registration. Returns None if nothing drifted."""
    locked_geometry = locked_config.get("backtest_geometry") or {}
    live = {
        "slippage_points": SLIPPAGE_POINTS,
        "commission_per_contract": COMMISSION_PER_CONTRACT,
        "backtest_logic_version": BACKTEST_LOGIC_VERSION,
    }
    drifted = {
        key: {"locked": locked_geometry.get(key), "live": live_value}
        for key, live_value in live.items()
        if key in locked_geometry and locked_geometry[key] != live_value
    }
    return drifted or None


def _run_locked_backtest(prospective: list, experiment: dict) -> dict:
    """Shared by evaluate_stopping_rule()/resolve_experiment(): rescore
    under the locked scoring config (point (a)), then run the backtest
    with the locked ATR/expiry/non-overlap GEOMETRY threaded through as
    real parameters (point (b), the parametrizable half of it)."""
    rescored = _rescore_under_locked_config(prospective, experiment)
    geometry = experiment["locked_config"].get("backtest_geometry") or {}
    return compute_backtest_comparison(
        rescored,
        sources=[experiment["direction_source"]],
        stop_mult=geometry.get("atr_stop_mult", ATR_STOP_MULT),
        target_mult=geometry.get("atr_target_mult", ATR_TARGET_MULT),
        expiry_bars=geometry.get("expiry_bars", EXPIRY_BARS),
        non_overlapping=geometry.get("non_overlapping", True),
    )


def evaluate_stopping_rule(experiment: dict) -> dict:
    """Read-only, side-effect-free: reports whether the experiment's
    pre-registered stopping rule is met right now, using only
    prospective candidates rescored under the LOCKED config -- does NOT
    resolve the experiment. Safe to call as often as desired (e.g. by
    the weekly scheduled check) to watch progress without ever
    consuming the one-time resolution."""
    prospective = _prospective_candidates(experiment)
    backtest = _run_locked_backtest(prospective, experiment)
    accepted_trades = (backtest["by_source"].get(experiment["direction_source"]) or {}).get("trades_taken", 0)
    distinct_days = backtest["day_session"]["distinct_trading_days"]

    rule = experiment["stopping_rule"]
    checks = {}
    if "min_distinct_trading_days" in rule:
        checks["min_distinct_trading_days"] = {
            "required": rule["min_distinct_trading_days"],
            "actual": distinct_days,
            "met": distinct_days >= rule["min_distinct_trading_days"],
        }
    if "min_accepted_trades" in rule:
        checks["min_accepted_trades"] = {
            "required": rule["min_accepted_trades"],
            "actual": accepted_trades,
            "met": accepted_trades >= rule["min_accepted_trades"],
        }
    return {
        "prospective_candidates_considered": len(prospective),
        "checks": checks,
        "stopping_rule_met": bool(checks) and all(c["met"] for c in checks.values()),
        "geometry_drift": _geometry_drift(experiment["locked_config"]),
    }


def _evaluate_target_metrics(backtest: dict, experiment: dict) -> dict:
    """Tier 3.23, module docstring point (c): computes whether the
    pre-registered primary metric actually met its comparator/threshold
    bar, using the SAME locked-config-rescored backtest resolve_
    experiment() is about to persist. `met` is None (not True/False)
    when the metric itself is None (e.g. profit_factor with zero
    losses so far) -- an undefined metric is inconclusive, not a
    failure."""
    target_metrics = experiment["target_metrics"]
    source_summary = backtest["by_source"].get(experiment["direction_source"]) or {}
    primary_metric = target_metrics["primary_metric"]
    comparator = target_metrics["comparator"]
    threshold = target_metrics["success_threshold"]
    actual = source_summary.get(primary_metric)

    met = None
    if actual is not None:
        met = {
            ">=": actual >= threshold, "<=": actual <= threshold,
            ">": actual > threshold, "<": actual < threshold,
            "==": actual == threshold,
        }[comparator]

    return {
        "primary_metric": primary_metric,
        "comparator": comparator,
        "success_threshold": threshold,
        "actual": actual,
        "met": met,
        "secondary_metrics": {m: source_summary.get(m) for m in target_metrics.get("secondary_metrics", [])},
    }


def resolve_experiment(experiment_id: str) -> dict:
    """The one-time outcome recording. Raises ExperimentError if the
    experiment doesn't exist or its stopping rule isn't met yet (no
    forcing an early look). If already resolved, returns the existing
    row completely untouched -- this function's whole purpose is to
    make it structurally impossible to quietly recompute a "final"
    answer after seeing more data, the exact failure mode the fourth
    review was warning about.

    Tier 3.23: the resolution now includes target_metrics_result (was
    the pre-registered primary metric actually met?) and geometry_drift
    (did slippage/commission/backtest logic change since registration?)
    alongside the same day_session/backtest detail Tier 3.20 recorded —
    all computed from candidates rescored under the LOCKED config, not
    whatever each candidate's own stored decision happened to be."""
    experiment = get_experiment_by_id(experiment_id)
    if experiment is None:
        raise ExperimentError(f"no experiment with id {experiment_id!r}")
    if experiment["status"] == "resolved":
        return experiment

    status_check = evaluate_stopping_rule(experiment)
    if not status_check["stopping_rule_met"]:
        raise ExperimentError(
            f"stopping rule not yet met for experiment {experiment_id!r}: {status_check['checks']}"
        )

    prospective = _prospective_candidates(experiment)
    backtest = _run_locked_backtest(prospective, experiment)
    resolution = {
        "resolved_from_candidates_considered": len(prospective),
        "day_session": backtest["day_session"],
        "backtest": backtest,
        "target_metrics_result": _evaluate_target_metrics(backtest, experiment),
        "geometry_drift": _geometry_drift(experiment["locked_config"]),
    }
    resolved = _persist_resolution(experiment_id, resolution)
    if resolved is None:
        raise ExperimentError(f"no experiment with id {experiment_id!r}")
    return resolved


def list_experiments(symbol: str | None = None, timeframe: str | None = None) -> list:
    return get_experiments(symbol=symbol, timeframe=timeframe)
