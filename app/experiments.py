"""
Experiment Registry — Tier 3.20 (fourth external review, 2026-08-18).

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
boundary in time — registered_at — such that only candidates created
AT OR AFTER that moment ever count toward that experiment's stopping
rule or its eventual outcome. Candidates from before registration are
explicitly "exploratory": already mined, fine to keep looking at for
ideas, but never eligible to confirm or refute a hypothesis registered
after they were already visible. This is standard pre-registration
practice (the same discipline behind clinical-trial protocols and
registered-report science), scaled down to the smallest version that
still has the property that matters: no peeking, no re-drawing the
"trial period" after seeing how it turned out.

Deliberately NOT the full shadow-trading engine review §3.6 gestures
at ("a completely separate execution path that mirrors production
exactly") — the review's own guidance was explicit that a simple
registration + one-time resolution log is enough for now ("يمكن
تنفيذ هذا حتى كوثيقة/سجل صغير قبل بناء shadow engine الكامل"). This
reuses ALREADY-TESTED reporting machinery (app.backtest.
compute_backtest_comparison, compute_day_session_breakdown) rather
than reimplementing any scoring or simulation — an experiment doesn't
change what's computed, only WHICH candidates are allowed to feed it
and WHEN the answer is allowed to be looked at.

Lifecycle, by design:
  1. register_experiment() — freezes hypothesis text, target_metrics
     (what the caller says they'll judge success/failure by), a
     stopping_rule (min_distinct_trading_days and/or
     min_accepted_trades, both computed the same way Tier 3.18/3.19
     and backtest-lite already do), and a snapshot of the CURRENT live
     coordinator_threshold/weights/min_available_weight — status
     starts 'active'. Nothing about this call touches live scoring;
     it only records what the live scoring happened to be at this
     moment, for later comparison if it's ever changed.
  2. evaluate_stopping_rule() — read-only, callable any number of
     times, never mutates anything. Reports whether the rule is met
     yet using ONLY prospective (post-registration) candidates, and
     the raw numbers either way — lets a caller check progress without
     resolving early.
  3. resolve_experiment() — the ONE-TIME action. Refuses if the
     stopping rule isn't met (no early/forced resolution). If already
     resolved, returns the existing resolution untouched — calling it
     again after more data has piled up does NOT recompute or replace
     it; resolution_json is exactly what storage.resolve_experiment()
     writes, once, on the write-once UPDATE ... WHERE status='active'
     guard.

Entirely additive: no existing endpoint's behavior changes, no
COORDINATOR_THRESHOLD/WEIGHTS touched (only read and snapshotted), no
LLM calls. New table (app.storage `experiments`), three new endpoints
(POST /experiments, GET /experiments, GET /experiments/{id},
POST /experiments/{id}/resolve) — see app/main.py for the HTTP layer.
"""

import os
import uuid

from app.backtest import DIRECTION_SOURCES, compute_backtest_comparison
from app.candidates import get_candidate_history
from app.coordinator import DECISION_THRESHOLD, MIN_AVAILABLE_WEIGHT, WEIGHTS
from app.storage import get_experiment_by_id, get_experiments, save_experiment
from app.storage import resolve_experiment as _persist_resolution

# How far back get_candidate_history looks when computing a stopping-
# rule check or a resolution -- generous enough that a slow-accumulating
# experiment's prospective window is unlikely to fall outside it before
# someone notices and raises this. Env-configurable like every other
# tunable in this project, NOT a per-request parameter (a caller
# picking their own window per request could cherry-pick which
# candidates are "in scope," defeating the entire point of this
# module).
EXPERIMENT_HISTORY_LIMIT = int(os.environ.get("EXPERIMENT_HISTORY_LIMIT", "2000"))

_STOPPING_RULE_KEYS = ("min_distinct_trading_days", "min_accepted_trades")


class ExperimentError(Exception):
    pass


def _new_experiment_id() -> str:
    return str(uuid.uuid4())


def _current_locked_config() -> dict:
    """A snapshot of the live scoring config, frozen at registration
    time -- read-only, never mutates WEIGHTS/DECISION_THRESHOLD/
    MIN_AVAILABLE_WEIGHT themselves."""
    return {
        "coordinator_threshold": DECISION_THRESHOLD,
        "weights": dict(WEIGHTS),
        "min_available_weight": MIN_AVAILABLE_WEIGHT,
    }


def register_experiment(
    symbol: str,
    timeframe: str,
    hypothesis: str,
    target_metrics: list,
    stopping_rule: dict,
    direction_source: str = "coordinator",
) -> dict:
    """Validates and persists a new pre-registered experiment. Raises
    ExperimentError on anything that would make the eventual
    resolution meaningless (no hypothesis text, no target metrics, no
    usable stopping rule) rather than silently registering a vague
    experiment nobody could later evaluate against its own stated
    purpose."""
    hypothesis = (hypothesis or "").strip()
    if not hypothesis:
        raise ExperimentError(
            "hypothesis must be a non-empty description of what this experiment is testing"
        )
    if not target_metrics:
        raise ExperimentError(
            "target_metrics must list at least one metric this experiment will report on"
        )
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
        target_metrics=list(target_metrics),
        stopping_rule=dict(stopping_rule),
        direction_source=direction_source,
    )


def _prospective_candidates(experiment: dict) -> list:
    """Candidates created AT OR AFTER this experiment's registered_at
    -- the only ones ever allowed to count toward its stopping rule or
    its resolution. Both timestamps come from SQLite's own
    datetime('now') (trade_candidates.created_at and
    experiments.registered_at), so this string comparison is exact,
    not an approximation across two different clocks."""
    all_candidates = get_candidate_history(
        symbol=experiment["symbol"], timeframe=experiment["timeframe"], limit=EXPERIMENT_HISTORY_LIMIT
    )
    registered_at = experiment["registered_at"]
    return [c for c in all_candidates if (c.get("created_at") or "") >= registered_at]


def evaluate_stopping_rule(experiment: dict) -> dict:
    """Read-only, side-effect-free: reports whether the experiment's
    pre-registered stopping rule is met right now, using only
    prospective candidates -- does NOT resolve the experiment. Safe to
    call as often as desired (e.g. by the weekly scheduled check) to
    watch progress without ever consuming the one-time resolution."""
    prospective = _prospective_candidates(experiment)
    backtest = compute_backtest_comparison(prospective, sources=[experiment["direction_source"]])
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
    }


def resolve_experiment(experiment_id: str) -> dict:
    """The one-time outcome recording. Raises ExperimentError if the
    experiment doesn't exist or its stopping rule isn't met yet (no
    forcing an early look). If already resolved, returns the existing
    row completely untouched -- this function's whole purpose is to
    make it structurally impossible to quietly recompute a "final"
    answer after seeing more data, the exact failure mode the fourth
    review was warning about."""
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
    backtest = compute_backtest_comparison(prospective, sources=[experiment["direction_source"]])
    resolution = {
        "resolved_from_candidates_considered": len(prospective),
        "day_session": backtest["day_session"],
        "backtest": backtest,
    }
    resolved = _persist_resolution(experiment_id, resolution)
    if resolved is None:
        raise ExperimentError(f"no experiment with id {experiment_id!r}")
    return resolved


def list_experiments(symbol: str | None = None, timeframe: str | None = None) -> list:
    return get_experiments(symbol=symbol, timeframe=timeframe)
