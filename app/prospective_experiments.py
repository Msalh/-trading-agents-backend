"""
Prospective Experiment Registry — Tier 3.43 (sixteenth external review,
2026-08-27).

Tier 3.42 built a genuinely useful CALCULATOR (compute_prospective_
overlap_comparison, GET /candidates/history/veto-prospective-comparison)
for the urgent+risk_off overlap hypothesis, and this session manually
froze a watermark (rowid 1183) and reported it in an untracked package.
The sixteenth review's core objection: that is not yet a pre-registered
experiment in any enforceable sense. The endpoint accepts an arbitrary
since_rowid/atr_stop_mult/atr_target_mult/expiry_bars on every call —
nothing stops a future call from quietly using a different watermark,
trying several stop/target combinations after seeing results, or only
reporting the best-looking run. The honest description of Tier 3.42 was
"an on-demand prospective-window calculator with a manually recorded
intended watermark," not "a frozen registered prospective experiment."
This module is the fix — reusing app.backtest.compute_prospective_
overlap_comparison() for all the actual simulation work, adding exactly
the pre-registration discipline the review asked for around it.

Why a SEPARATE small table instead of extending app.experiments: the
review itself said so explicitly — "create a small permanent record for
this comparison, no need to redesign the whole experiment registry."
app.experiments' `experiments` table is genuinely single-arm shaped (one
direction_source column, one target_metrics triple) and already has two
live registrations; forcing this module's fixed 3-arm shape into it
would be exactly the redesign-of-two-live-rows risk Tier 3.42's own
design rationale already rejected once. `prospective_experiments` is a
new, independent table with the same append-only / write-once-resolution
shape, copied deliberately from app.experiments rather than re-derived,
so the two stay recognizably siblings without being the same thing.

Four things get locked at registration and enforced (not just recorded)
from then on, directly answering the review's four listed gaps:

  1. IMMUTABLE RECORD: register_prospective_experiment() freezes
     hypothesis, the fixed 3 arms (app.backtest.PROSPECTIVE_ARMS —
     "none"/"solo_veto_only"/"overlap_only", not caller-configurable),
     target_metrics (a single primary metric — ALWAYS read at
     portfolio_level.overall, never decision_level, per the review's own
     "primary result must be portfolio-level" reading-arms note),
     stopping_rule, the live Coordinator scoring config + backtest
     geometry + News/Macro model/prompt versions, and the current
     registered_watermark_rowid — all in one write, in the
     `prospective_experiments` table (app.storage). GET /prospective-
     experiments/{id} then reads ONLY experiment_id and replays the
     frozen config — no since_rowid/atr_stop_mult/etc. accepted for a
     registered experiment's own evaluation, closing the "anyone could
     change the watermark or geometry tomorrow" gap directly. The
     original Tier 3.42 calculator endpoint (GET /candidates/history/
     veto-prospective-comparison) remains available and useful for
     genuinely exploratory ad-hoc questions — its docs now say plainly
     that it is NOT this pre-registered experiment.

  2. STOPPING RULE, DEFINED BEFORE ANY RESULT IS SEEN: register_
     prospective_experiment() REQUIRES at least one of
     min_distinct_trading_days / min_baseline_portfolio_trades /
     min_overlap_joint_opinion_pairs — the exact three the review named
     (day coverage, baseline sample size, and overlap-specific opinion-
     pair diversity, not just a raw candidate count, since the overlap
     arm is rare by construction). evaluate_prospective_stopping_rule()
     is read-only and callable any number of times WITHOUT ever
     revealing a P&L/win-rate/profit-factor figure — only counts and
     met/not-met booleans (see its own docstring: this is the review's
     item #6, "don't publish early results daily, show progress counts
     only," enforced at the API shape level, not left to human
     discipline). resolve_prospective_experiment() is the ONE-TIME
     action that actually computes and persists the full comparison,
     and refuses outright if the stopping rule isn't met yet — no
     forcing an early look, identical guarantee to app.experiments.
     resolve_experiment().

  3. NO SILENT OVERRIDES: arms are fixed, not accepted as input. The
     registered watermark and locked geometry/config are read from the
     stored row, never taken from request parameters, at both status
     and resolve time.

  4. CONFIG/PROMPT/MODEL DRIFT DETECTION: app.news_agent.PROMPT_VERSION
     and app.macro_agent.PROMPT_VERSION (new this tier, same hand-
     maintained-marker convention as app.backtest.BACKTEST_LOGIC_
     VERSION) plus each agent's MODEL are locked at registration
     alongside Coordinator's weights/threshold/min_available_weight/
     analysis_required (which are actively RE-APPLIED via app.replay.
     replay_candidate() before every evaluation/resolution — Coordinator
     drift is corrected by construction, not just detected, exactly
     like app.experiments already does). News/Macro opinions can't be
     replayed the same way (they're LLM judgment calls, not a pure
     function of stored inputs) — so drift there is DETECTED and
     SEPARATED instead: every prospective candidate's News/Macro
     opinion is classified "matches" / "drifted" / "unversioned"
     (opinions from before this tier shipped, predating the version
     fields entirely) / "absent". Only "drifted" candidates are
     excluded from the primary computation; "unversioned" candidates
     stay in (excluding them would leave zero data until versioning
     fully propagates) but their count is always reported alongside
     every result so a reader can see how much of the window predates
     versioning. Backtest geometry/slippage/commission/logic-version
     drift is reported the same way app.experiments already does
     (geometry_drift, loud flag, not silently applied).

Entirely additive: no existing endpoint's behavior changes, no
COORDINATOR_THRESHOLD/WEIGHTS/MIN_AVAILABLE_WEIGHT touched (only read
and snapshotted), app.experiments and its two live registrations
untouched, DIRECTION_SOURCES untouched (arms always resolve through
app.backtest.compute_prospective_overlap_comparison's own
direction_source="coordinator" internals). Table: app.storage
`prospective_experiments`. Endpoints: POST /prospective-experiments,
GET /prospective-experiments, GET /prospective-experiments/{id}, GET
/prospective-experiments/{id}/status, POST /prospective-experiments/
{id}/resolve — see app/main.py.
"""

import uuid

from app.backtest import (
    ATR_STOP_MULT,
    ATR_TARGET_MULT,
    BACKTEST_LOGIC_VERSION,
    EXPIRY_BARS,
    PROSPECTIVE_ARMS,
    PROSPECTIVE_POPULATION_SAFETY_CAP,
    compute_prospective_overlap_comparison,
)
from app.coordinator import ANALYSIS_REQUIRED, DECISION_THRESHOLD, MIN_AVAILABLE_WEIGHT, WEIGHTS
from app.experiments import VALID_COMPARATORS, VALID_TARGET_METRIC_KEYS
from app.macro_agent import MODEL as MACRO_MODEL
from app.macro_agent import PROMPT_VERSION as MACRO_PROMPT_VERSION
from app.news_agent import MODEL as NEWS_MODEL
from app.news_agent import PROMPT_VERSION as NEWS_PROMPT_VERSION
from app.paper_trades import COMMISSION_PER_CONTRACT, SLIPPAGE_POINTS
from app.replay import replay_candidate
from app.storage import (
    count_candidates_after_rowid,
    get_candidates_after_rowid,
    get_max_candidate_rowid,
    get_prospective_experiment_by_id,
    get_prospective_experiments,
    save_prospective_experiment,
)
from app.storage import resolve_prospective_experiment as _persist_prospective_resolution

# The three components the sixteenth review explicitly named: day
# coverage of the whole prospective window, baseline sample size (so
# the comparison itself is statistically meaningful), and the overlap
# arm's OWN opinion-pair diversity specifically (since it's rare by
# construction — a raw candidate-count minimum alone could be satisfied
# by one arm fanning out while the other stays thin).
_STOPPING_RULE_KEYS = ("min_distinct_trading_days", "min_baseline_portfolio_trades", "min_overlap_joint_opinion_pairs")


class ProspectiveExperimentError(Exception):
    pass


def _new_experiment_id() -> str:
    return str(uuid.uuid4())


def _current_locked_config() -> dict:
    """Snapshot of the live Coordinator scoring config, backtest
    geometry, and News/Macro agent model+prompt versions, frozen at
    registration -- read-only, never mutates any of the underlying
    module-level values. See the module docstring's point 4 for which
    of these are actively RE-APPLIED at evaluation/resolution time
    (Coordinator scoring, via replay_candidate) versus only
    drift-checked (backtest geometry/slippage/commission/logic version,
    News/Macro model/prompt version)."""
    return {
        "coordinator_threshold": DECISION_THRESHOLD,
        "weights": dict(WEIGHTS),
        "min_available_weight": MIN_AVAILABLE_WEIGHT,
        "analysis_required": ANALYSIS_REQUIRED,
        "backtest_geometry": {
            "atr_stop_mult": ATR_STOP_MULT,
            "atr_target_mult": ATR_TARGET_MULT,
            "expiry_bars": EXPIRY_BARS,
            "slippage_points": SLIPPAGE_POINTS,
            "commission_per_contract": COMMISSION_PER_CONTRACT,
            "backtest_logic_version": BACKTEST_LOGIC_VERSION,
        },
        "news_agent": {"model": NEWS_MODEL, "prompt_version": NEWS_PROMPT_VERSION},
        "macro_agent": {"model": MACRO_MODEL, "prompt_version": MACRO_PROMPT_VERSION},
    }


def _validate_target_metrics(target_metrics: dict) -> dict:
    """A single primary metric, expressed as a comparison between two
    of the three fixed arms -- either `incremental` (treatment's metric
    minus baseline's, e.g. "incremental portfolio P&L", the review's own
    first example) or `treatment_only` (the treatment arm's metric
    alone, e.g. a profit_factor floor). Both values are ALWAYS read from
    portfolio_level.overall by the caller of this validated dict, never
    decision_level and never a caller-supplied "level" -- not exposed as
    a field here at all, so it can't be silently switched to a more
    flattering view after the fact."""
    if not isinstance(target_metrics, dict):
        raise ProspectiveExperimentError("target_metrics must be an object")
    treatment_arm = target_metrics.get("treatment_arm")
    baseline_arm = target_metrics.get("baseline_arm") or "none"
    if treatment_arm not in PROSPECTIVE_ARMS:
        raise ProspectiveExperimentError(f"target_metrics.treatment_arm must be one of {PROSPECTIVE_ARMS}")
    if baseline_arm not in PROSPECTIVE_ARMS:
        raise ProspectiveExperimentError(f"target_metrics.baseline_arm must be one of {PROSPECTIVE_ARMS}")
    if treatment_arm == baseline_arm:
        raise ProspectiveExperimentError("target_metrics.treatment_arm and baseline_arm must differ")
    metric = target_metrics.get("metric")
    if metric not in VALID_TARGET_METRIC_KEYS:
        raise ProspectiveExperimentError(f"target_metrics.metric must be one of {VALID_TARGET_METRIC_KEYS}")
    comparison_mode = target_metrics.get("comparison_mode") or "incremental"
    if comparison_mode not in ("incremental", "treatment_only"):
        raise ProspectiveExperimentError("target_metrics.comparison_mode must be 'incremental' or 'treatment_only'")
    comparator = target_metrics.get("comparator")
    if comparator not in VALID_COMPARATORS:
        raise ProspectiveExperimentError(f"target_metrics.comparator must be one of {VALID_COMPARATORS}")
    success_threshold = target_metrics.get("success_threshold")
    if not isinstance(success_threshold, (int, float)) or isinstance(success_threshold, bool):
        raise ProspectiveExperimentError("target_metrics.success_threshold must be a number")
    secondary_metrics = target_metrics.get("secondary_metrics") or []
    unknown = [m for m in secondary_metrics if m not in VALID_TARGET_METRIC_KEYS]
    if unknown:
        raise ProspectiveExperimentError(f"target_metrics.secondary_metrics has unknown metric(s) {unknown}, expected one of {VALID_TARGET_METRIC_KEYS}")
    return {
        "treatment_arm": treatment_arm,
        "baseline_arm": baseline_arm,
        "metric": metric,
        "comparison_mode": comparison_mode,
        "comparator": comparator,
        "success_threshold": float(success_threshold),
        "secondary_metrics": list(secondary_metrics),
    }


def register_prospective_experiment(
    symbol: str,
    timeframe: str,
    hypothesis: str,
    target_metrics: dict,
    stopping_rule: dict,
) -> dict:
    """Validates and persists a new pre-registered prospective
    experiment. Arms are always the fixed app.backtest.PROSPECTIVE_ARMS
    triple -- not a parameter, deliberately, so a caller can't register
    an ad-hoc arm set that only resembles pre-registration. Raises
    ProspectiveExperimentError on anything that would make the eventual
    resolution meaningless, same philosophy as app.experiments.
    register_experiment()."""
    hypothesis = (hypothesis or "").strip()
    if not hypothesis:
        raise ProspectiveExperimentError(
            "hypothesis must be a non-empty description of what this experiment is testing"
        )
    validated_target_metrics = _validate_target_metrics(target_metrics)
    if not stopping_rule or not any(k in stopping_rule for k in _STOPPING_RULE_KEYS):
        raise ProspectiveExperimentError(f"stopping_rule must set at least one of {_STOPPING_RULE_KEYS}")
    for key in stopping_rule:
        if key not in _STOPPING_RULE_KEYS:
            raise ProspectiveExperimentError(f"unknown stopping_rule key {key!r}, expected one of {_STOPPING_RULE_KEYS}")

    return save_prospective_experiment(
        experiment_id=_new_experiment_id(),
        symbol=symbol,
        timeframe=timeframe,
        hypothesis=hypothesis,
        arms=list(PROSPECTIVE_ARMS),
        locked_config=_current_locked_config(),
        target_metrics=validated_target_metrics,
        stopping_rule=dict(stopping_rule),
        registered_watermark_rowid=get_max_candidate_rowid(symbol, timeframe),
    )


def _prospective_candidates(experiment: dict) -> list:
    """Candidates with rowid STRICTLY AFTER this experiment's
    registered_watermark_rowid -- same no-peeking boundary and same
    "raise loudly rather than silently truncate" safety check as
    app.experiments._prospective_candidates(), reusing app.backtest.
    PROSPECTIVE_POPULATION_SAFETY_CAP rather than inventing a second
    ceiling constant."""
    watermark = experiment["registered_watermark_rowid"]
    count = count_candidates_after_rowid(experiment["symbol"], experiment["timeframe"], watermark)
    if count > PROSPECTIVE_POPULATION_SAFETY_CAP:
        raise ProspectiveExperimentError(
            f"prospective window for experiment {experiment['experiment_id']!r} has grown to "
            f"{count} candidates, past PROSPECTIVE_POPULATION_SAFETY_CAP="
            f"{PROSPECTIVE_POPULATION_SAFETY_CAP} -- refusing to silently truncate"
        )
    return get_candidates_after_rowid(experiment["symbol"], experiment["timeframe"], watermark)


def _rescore_under_locked_config(candidates: list, experiment: dict) -> list:
    """Every arm resolves through direction_source="coordinator"
    internally (app.backtest.compute_prospective_overlap_comparison),
    so -- exactly like app.experiments._rescore_under_locked_config --
    each candidate is re-scored via app.replay.replay_candidate() under
    the FROZEN weights/threshold/min_available_weight/analysis_required
    before any arm is computed. This is what makes a live Coordinator
    config change mid-experiment harmless: it is never silently blended
    into the comparison, it's replaced outright. The candidate's
    opinions_used (News/Macro opinions, with their own flags/model/
    prompt_version) pass through unchanged -- replay_candidate only
    re-runs the SCORING step, not the underlying LLM opinions."""
    locked = experiment["locked_config"]
    rescored = []
    for candidate in candidates:
        replayed = replay_candidate(
            candidate,
            weights=locked["weights"],
            threshold=locked["coordinator_threshold"],
            min_available_weight=locked["min_available_weight"],
            analysis_required=locked.get("analysis_required", True),
        )
        rescored.append({**candidate, "decision": replayed["replayed"]})
    return rescored


def _classify_opinion_drift(opinion: dict | None, locked_model: str, locked_prompt_version: str) -> str:
    """Per-opinion drift classification against this experiment's
    locked News/Macro model+prompt_version. "absent" (no opinion
    recorded at all -- a separate, pre-existing gap this tier doesn't
    create) and "unversioned" (opinion predates the model/prompt_
    version fields this tier adds) are both expected, non-alarming
    states, kept in the primary computation. Only "drifted" (present,
    versioned, and genuinely different from what was locked) is
    excluded -- see _split_by_drift."""
    if not opinion:
        return "absent"
    if "prompt_version" not in opinion or "model" not in opinion:
        return "unversioned"
    if opinion["prompt_version"] == locked_prompt_version and opinion["model"] == locked_model:
        return "matches"
    return "drifted"


def _classify_candidate_drift(candidate: dict, experiment: dict) -> str:
    """Combines the News and Macro classifications for one candidate:
    "drifted" (excluded from the primary computation) if EITHER agent's
    opinion drifted, else "unversioned" (kept in, counted separately) if
    either is unversioned, else "matches"."""
    locked = experiment["locked_config"]
    opinions_used = (candidate.get("decision") or {}).get("opinions_used") or {}
    news_status = _classify_opinion_drift(
        opinions_used.get("news"), locked["news_agent"]["model"], locked["news_agent"]["prompt_version"],
    )
    macro_status = _classify_opinion_drift(
        opinions_used.get("macro"), locked["macro_agent"]["model"], locked["macro_agent"]["prompt_version"],
    )
    if "drifted" in (news_status, macro_status):
        return "drifted"
    if "unversioned" in (news_status, macro_status):
        return "unversioned"
    return "matches"


def _split_by_drift(candidates: list, experiment: dict) -> tuple[list, int, int]:
    """Tier 3.43, module docstring point 4 ("the report must reject or
    SEPARATE any config drift" -- this implementation separates):
    partitions `candidates` into `clean` (fed into the primary
    computation -- "matches" and "unversioned") and excluded
    ("drifted"). Returns (clean, drifted_excluded_count,
    unversioned_count) -- both counts always reported alongside every
    result, never silently absorbed."""
    clean = []
    drifted_count = 0
    unversioned_count = 0
    for candidate in candidates:
        status = _classify_candidate_drift(candidate, experiment)
        if status == "drifted":
            drifted_count += 1
            continue
        if status == "unversioned":
            unversioned_count += 1
        clean.append(candidate)
    return clean, drifted_count, unversioned_count


def _geometry_drift(locked_config: dict) -> dict | None:
    """Same shape and purpose as app.experiments._geometry_drift: these
    three fields aren't parametrized into app.backtest's simulation
    (read from module-level constants instead), so rather than silently
    using whatever happens to be live, this reports a loud drift flag
    whenever they no longer match what was locked. Returns None if
    nothing drifted."""
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


def _run_locked_comparison(clean_candidates: list, experiment: dict) -> dict:
    """Shared by evaluate_prospective_stopping_rule()/resolve_
    prospective_experiment(): rescore under the locked Coordinator
    config, then run compute_prospective_overlap_comparison with the
    locked ATR/expiry GEOMETRY threaded through as real parameters --
    the same locked-config-then-locked-geometry sequencing app.
    experiments._run_locked_backtest() already established."""
    rescored = _rescore_under_locked_config(clean_candidates, experiment)
    geometry = experiment["locked_config"].get("backtest_geometry") or {}
    return compute_prospective_overlap_comparison(
        rescored,
        stop_mult=geometry.get("atr_stop_mult", ATR_STOP_MULT),
        target_mult=geometry.get("atr_target_mult", ATR_TARGET_MULT),
        expiry_bars=geometry.get("expiry_bars", EXPIRY_BARS),
    )


def evaluate_prospective_stopping_rule(experiment: dict) -> dict:
    """Read-only, side-effect-free, safe to call any number of times --
    does NOT resolve the experiment. Tier 3.43 (sixteenth review, item
    #6, "don't publish early results daily; show progress counts only
    until checkpoint"): deliberately reports ONLY candidate/day/
    opinion-pair COUNTS and met/not-met stopping-rule checks -- NEVER
    any P&L/win-rate/profit-factor/max-drawdown figure, even though the
    full comparison is computed internally to extract those counts. This
    mirrors app.experiments.evaluate_stopping_rule()'s existing "counts
    only, no peeking" shape exactly -- not a new invention, the same
    discipline extended to a second experiment type."""
    prospective = _prospective_candidates(experiment)
    clean, drifted_count, unversioned_count = _split_by_drift(prospective, experiment)
    comparison = _run_locked_comparison(clean, experiment)

    baseline_arm = experiment["target_metrics"]["baseline_arm"]
    none_arm_overall = comparison["results"]["none"]["decision_level"]["overall"]
    baseline_portfolio = comparison["results"][baseline_arm]["portfolio_level"]["overall"]
    overlap_decision = comparison["results"]["overlap_only"]["decision_level"]["overall"]

    rule = experiment["stopping_rule"]
    checks = {}
    if "min_distinct_trading_days" in rule:
        actual = none_arm_overall["distinct_trading_days"]
        checks["min_distinct_trading_days"] = {
            "required": rule["min_distinct_trading_days"], "actual": actual,
            "met": actual >= rule["min_distinct_trading_days"],
        }
    if "min_baseline_portfolio_trades" in rule:
        actual = baseline_portfolio["trades_taken"]
        checks["min_baseline_portfolio_trades"] = {
            "required": rule["min_baseline_portfolio_trades"], "actual": actual,
            "met": actual >= rule["min_baseline_portfolio_trades"],
        }
    if "min_overlap_joint_opinion_pairs" in rule:
        actual = overlap_decision["distinct_joint_news_macro_opinions"]
        checks["min_overlap_joint_opinion_pairs"] = {
            "required": rule["min_overlap_joint_opinion_pairs"], "actual": actual,
            "met": actual >= rule["min_overlap_joint_opinion_pairs"],
        }

    return {
        "prospective_candidates_considered": len(prospective),
        "clean_candidates_considered": len(clean),
        "config_drift": {
            "drifted_excluded_count": drifted_count,
            "unversioned_count": unversioned_count,
        },
        "checks": checks,
        "stopping_rule_met": bool(checks) and all(c["met"] for c in checks.values()),
        "geometry_drift": _geometry_drift(experiment["locked_config"]),
    }


def _evaluate_prospective_target_metrics(comparison: dict, experiment: dict) -> dict:
    """Computes the pre-registered primary metric against the SAME
    locked-config-rescored, drift-excluded comparison resolve_
    prospective_experiment() is about to persist. Always reads
    portfolio_level.overall for both arms -- never decision_level, per
    the module docstring. `met` is None (not True/False) when the
    metric itself is None for either arm (e.g. profit_factor with zero
    losses so far) -- an undefined metric is inconclusive, not a
    failure, same convention as app.experiments._evaluate_target_
    metrics()."""
    target_metrics = experiment["target_metrics"]
    treatment_arm = target_metrics["treatment_arm"]
    baseline_arm = target_metrics["baseline_arm"]
    metric = target_metrics["metric"]
    comparator = target_metrics["comparator"]
    threshold = target_metrics["success_threshold"]
    comparison_mode = target_metrics["comparison_mode"]

    treatment_summary = comparison["results"][treatment_arm]["portfolio_level"]["overall"]
    baseline_summary = comparison["results"][baseline_arm]["portfolio_level"]["overall"]
    treatment_value = treatment_summary.get(metric)
    baseline_value = baseline_summary.get(metric)

    if comparison_mode == "incremental":
        actual = None if treatment_value is None or baseline_value is None else treatment_value - baseline_value
    else:
        actual = treatment_value

    met = None
    if actual is not None:
        met = {
            ">=": actual >= threshold, "<=": actual <= threshold,
            ">": actual > threshold, "<": actual < threshold,
            "==": actual == threshold,
        }[comparator]

    return {
        "treatment_arm": treatment_arm,
        "baseline_arm": baseline_arm,
        "metric": metric,
        "comparison_mode": comparison_mode,
        "treatment_value": treatment_value,
        "baseline_value": baseline_value,
        "comparator": comparator,
        "success_threshold": threshold,
        "actual": actual,
        "met": met,
        "secondary_metrics": {
            m: {"treatment": treatment_summary.get(m), "baseline": baseline_summary.get(m)}
            for m in target_metrics.get("secondary_metrics", [])
        },
    }


def resolve_prospective_experiment(experiment_id: str) -> dict:
    """The one-time outcome recording. Raises ProspectiveExperimentError
    if the experiment doesn't exist or its stopping rule isn't met yet
    (no forcing an early look -- the exact discipline the sixteenth
    review's "optional stopping" warning is about). If already resolved,
    returns the existing row completely untouched."""
    experiment = get_prospective_experiment_by_id(experiment_id)
    if experiment is None:
        raise ProspectiveExperimentError(f"no prospective experiment with id {experiment_id!r}")
    if experiment["status"] == "resolved":
        return experiment

    status_check = evaluate_prospective_stopping_rule(experiment)
    if not status_check["stopping_rule_met"]:
        raise ProspectiveExperimentError(
            f"stopping rule not yet met for prospective experiment {experiment_id!r}: {status_check['checks']}"
        )

    prospective = _prospective_candidates(experiment)
    clean, drifted_count, unversioned_count = _split_by_drift(prospective, experiment)
    comparison = _run_locked_comparison(clean, experiment)
    resolution = {
        "resolved_from_candidates_considered": len(prospective),
        "resolved_from_clean_candidates": len(clean),
        "config_drift": {
            "drifted_excluded_count": drifted_count,
            "unversioned_count": unversioned_count,
        },
        "population": comparison["population"],
        "arms_results": comparison["results"],
        "target_metrics_result": _evaluate_prospective_target_metrics(comparison, experiment),
        "geometry_drift": _geometry_drift(experiment["locked_config"]),
    }
    resolved = _persist_prospective_resolution(experiment_id, resolution)
    if resolved is None:
        raise ProspectiveExperimentError(f"no prospective experiment with id {experiment_id!r}")
    return resolved


def list_prospective_experiments(symbol: str | None = None, timeframe: str | None = None) -> list:
    return get_prospective_experiments(symbol=symbol, timeframe=timeframe)
