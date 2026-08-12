"""
Trade Candidates — Tier 2.1 (external review, Aug 2026).

The problem this solves: before this module, Risk and Execution each
independently asked the database for "the latest" decision/opinion/
bar. Nothing guaranteed those "latest" reads referred to the same
moment — Risk could approve decision A computed from bar B, while
Execution (running seconds or minutes later) could combine that same
Risk approval with a newer Coordinator decision C and a newer bar D.
The pieces were never wrong individually; the *combination* could be
incoherent, and nothing would show that in the data.

A "candidate" is one immutable row created the moment the Coordinator
computes a decision: it freezes the exact bar and the exact
opinions_used that decision was scored from. Risk and Execution no
longer perform their own independent "latest" lookups — they operate
on THIS SAME row, and their results are written back onto it (never
into a separate table), so a candidate_id names one complete,
self-consistent history: bar → opinions → decision → risk → execution.

Design decisions made explicitly (not defaults nobody chose):
  - A candidate is created for EVERY Coordinator run, including
    no_trade/insufficient_data — cheap (it's just persisting data
    already computed) and preserves context for later analysis of
    near-miss decisions, not just ones that became trades.
  - Full snapshot, not references — this repo already has an
    admin single-record delete endpoint, so a reference-based design
    (storing event_id/opinion timestamps and re-querying) could go
    stale or break if underlying rows are ever removed. Storing the
    actual opinion/bar content inline is what makes "immutable"
    actually true regardless of what happens to the source tables
    afterward.
  - "Latest candidate" is the transparent source of truth for Risk
    and Execution — callers don't pass a candidate_id explicitly
    (avoids complicating the dashboard/API), they always act on
    whatever the most recent one is. This still solves the mismatch
    problem because there is now only ONE thing to call "latest,"
    not four independently-latest pieces that can disagree.
  - Freshness is checked against the candidate's own created_at,
    once, rather than checking each of its four ingredients
    separately (that's now redundant — they were already fresh at
    the moment the candidate was built; what matters now is how old
    the *candidate itself* is by the time Risk/Execution act on it).
"""

import os
import uuid
from datetime import datetime, timezone

from app.coordinator import CoordinatorDecision, compute_decision
from app.storage import (
    attach_execution_result,
    attach_risk_result,
    get_candidate_by_id,
    get_latest,
    get_latest_candidate,
    get_recent_candidates,
    save_candidate,
)

# How old a candidate can be before Risk/Execution refuse to act on
# it — a stale candidate represents a market moment that has already
# passed; evaluating risk or planning an order against it would be
# acting on outdated information, exactly the failure mode this
# module exists to prevent.
CANDIDATE_MAX_AGE_MINUTES = int(os.environ.get("CANDIDATE_MAX_AGE_MINUTES", "20"))


class CandidateError(Exception):
    pass


def _new_candidate_id() -> str:
    return str(uuid.uuid4())


def create_candidate(symbol: str, timeframe: str) -> dict:
    """The only place a CoordinatorDecision gets turned into a
    persisted candidate. Computes the decision fresh (capturing
    opinions_used atomically — see coordinator.py), grabs the bar it
    was anchored to, and saves all of it as one new immutable row.
    Always creates a row, even for no_trade/insufficient_data."""
    decision: CoordinatorDecision = compute_decision(symbol=symbol, timeframe=timeframe)
    bar = get_latest(symbol=symbol, timeframe=timeframe)

    candidate_id = _new_candidate_id()
    save_candidate(
        candidate_id=candidate_id,
        symbol=symbol,
        timeframe=timeframe,
        bar=bar,
        decision=decision.to_dict(),
    )
    return get_candidate_by_id(candidate_id)


def _candidate_age_minutes(candidate: dict) -> float | None:
    created_at = candidate.get("created_at")
    if not created_at:
        return None
    try:
        # SQLite's datetime('now') default -> 'YYYY-MM-DD HH:MM:SS' UTC, no offset
        dt = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds() / 60


def get_current_candidate(symbol: str, timeframe: str) -> dict:
    """The single lookup Risk and Execution should use instead of
    each independently querying 'latest decision' / 'latest opinion' /
    'latest bar'. Raises CandidateError if none exists yet, or if the
    latest one is too old to act on."""
    candidate = get_latest_candidate(symbol=symbol, timeframe=timeframe)
    if candidate is None:
        raise CandidateError("no trade candidate exists yet for this symbol/timeframe")

    age = _candidate_age_minutes(candidate)
    if age is not None and age > CANDIDATE_MAX_AGE_MINUTES:
        raise CandidateError(
            f"latest candidate is {age:.1f} minutes old, older than the "
            f"{CANDIDATE_MAX_AGE_MINUTES}-minute limit — refusing to act on a stale market moment"
        )
    return candidate


def record_risk_result(candidate_id: str, risk_opinion: dict) -> None:
    ok = attach_risk_result(candidate_id, risk_opinion)
    if not ok:
        raise CandidateError(f"no candidate found with id={candidate_id}")


def record_execution_result(candidate_id: str, execution_opinion: dict) -> None:
    ok = attach_execution_result(candidate_id, execution_opinion)
    if not ok:
        raise CandidateError(f"no candidate found with id={candidate_id}")


def get_candidate_history(symbol: str, timeframe: str, limit: int = 20) -> list[dict]:
    return get_recent_candidates(symbol=symbol, timeframe=timeframe, limit=limit)
