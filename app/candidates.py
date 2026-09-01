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
    get_candidates_page,
    get_latest,
    get_latest_candidate,
    get_recent_candidates,
    get_trade_by_candidate_id,
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


class CandidateLockedError(CandidateError):
    """Tier 3.1: raised when Risk/Execution tries to attach a result to
    a candidate that already has a committed paper trade. See
    _attach_candidate_result in app/storage.py for the full reasoning
    — the short version is that once a trade exists, its entry/stop/
    size is fixed, and letting the candidate's risk_json/execution_json
    keep changing after that would describe a trade that was never
    actually taken."""

    pass


def _new_candidate_id() -> str:
    return str(uuid.uuid4())


def create_candidate(
    symbol: str,
    timeframe: str,
    bar: dict | None = None,
    analysis_opinion: dict | None = None,
) -> dict:
    """The only place a CoordinatorDecision gets turned into a
    persisted candidate. Computes the decision fresh (capturing
    opinions_used atomically — see coordinator.py), grabs the bar it
    was anchored to, and saves all of it as one new immutable row.
    Always creates a row, even for no_trade/insufficient_data.

    Tier 3.1 (causal integrity): bar/analysis_opinion let a caller —
    the webhook's auto-analysis background task — pin this candidate
    to the EXACT bar and Analysis opinion that triggered it, computed
    once and threaded through to compute_decision() too. Before this,
    the bar used for Timing (inside compute_decision), the Analysis
    opinion scored, and the bar stored on the candidate row were each
    an INDEPENDENT "get the latest" query — three separate reads that
    could each return a different bar if another webhook landed in
    between. Now there is at most one "latest" lookup for the whole
    call (only when bar is omitted — the manual /coordinator/decide
    path, where there's no specific triggering event to anchor to),
    and it's reused everywhere instead of being re-queried."""
    anchor_bar = bar if bar is not None else get_latest(symbol=symbol, timeframe=timeframe)

    decision: CoordinatorDecision = compute_decision(
        symbol=symbol, timeframe=timeframe, bar=anchor_bar, analysis_opinion=analysis_opinion
    )

    candidate_id = _new_candidate_id()
    save_candidate(
        candidate_id=candidate_id,
        symbol=symbol,
        timeframe=timeframe,
        bar=anchor_bar,
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
    result = attach_risk_result(candidate_id, risk_opinion)
    if result == "not_found":
        raise CandidateError(f"no candidate found with id={candidate_id}")
    if result == "locked":
        raise CandidateLockedError(
            f"candidate {candidate_id} already has a committed paper trade — "
            "its Risk result can no longer be changed"
        )


def record_execution_result(candidate_id: str, execution_opinion: dict) -> None:
    result = attach_execution_result(candidate_id, execution_opinion)
    if result == "not_found":
        raise CandidateError(f"no candidate found with id={candidate_id}")
    if result == "locked":
        raise CandidateLockedError(
            f"candidate {candidate_id} already has a committed paper trade — "
            "its Execution result can no longer be changed"
        )


def get_committed_trade(candidate_id: str) -> dict | None:
    """Tier 3.1: the one place callers (main.py) should check whether
    a candidate is already past the write-once boundary, BEFORE doing
    any real work (a paid Execution LLM call, a Risk sizing pass) that
    would only get rejected anyway — see _attach_candidate_result in
    app/storage.py for why the rejection itself exists."""
    return get_trade_by_candidate_id(candidate_id)


def get_candidate_history(symbol: str, timeframe: str, limit: int = 20) -> list[dict]:
    return get_recent_candidates(symbol=symbol, timeframe=timeframe, limit=limit)


def get_candidate_history_page(
    symbol: str, timeframe: str, after_rowid: int = 0, limit: int = 200
) -> dict:
    """Tier 3.48: thin wrapper over storage.get_candidates_page(), same
    role as get_candidate_history() above but for real cursor-based
    pagination instead of a newest-N pull -- see that function's
    docstring for why the two aren't interchangeable."""
    return get_candidates_page(symbol=symbol, timeframe=timeframe, after_rowid=after_rowid, limit=limit)
