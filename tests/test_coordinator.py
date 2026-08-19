"""
Unit tests for app.coordinator — pure aggregation logic, no LLM,
no network. Uses a temporary SQLite file (via storage.DB_PATH
monkeypatch) so tests never touch real data.

Run with: pytest tests/test_coordinator.py -v
"""

import importlib
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

_NY_TZ = ZoneInfo("America/New_York")


@pytest.fixture
def fresh_storage(monkeypatch):
    """Point storage at a throwaway temp DB file for each test, and
    reload coordinator so it picks up the patched storage module."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setenv("DB_PATH", tmp.name)

    import app.storage as storage
    importlib.reload(storage)
    storage.init_db()

    import app.coordinator as coordinator
    importlib.reload(coordinator)

    yield storage, coordinator

    os.unlink(tmp.name)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _minutes_ago_iso(minutes: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _opinion(direction, confidence, flags=None, timestamp=None):
    return {
        "direction": direction,
        "confidence": confidence,
        "reasoning": "test",
        "key_data": {},
        "flags": flags or [],
        "timestamp": timestamp or _now_iso(),  # fresh by default — matches real agent output shape
    }


def _ny_time_to_utc_iso(year, month, day, hour, minute) -> str:
    ny_dt = datetime(year, month, day, hour, minute, tzinfo=_NY_TZ)
    return ny_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _save_bar(storage, symbol, timeframe, timestamp_iso):
    """Minimal fake market_state row so _gather_opinions' get_latest()
    call finds a bar and Timing gets evaluated against it — same
    bypass-the-Pydantic-model pattern as test_outcomes.py's _save_bar,
    since evaluate_timing only ever reads the "timestamp" field."""
    conn = storage.get_connection()
    payload = {"timestamp": timestamp_iso, "symbol": symbol, "timeframe": timeframe}
    conn.execute(
        "INSERT INTO market_state (event_id, symbol, timeframe, timestamp, payload_json) VALUES (?, ?, ?, ?, ?)",
        (f"{symbol}:{timeframe}:{timestamp_iso}", symbol, timeframe, timestamp_iso, json.dumps(payload)),
    )
    conn.commit()
    conn.close()


def test_no_opinions_returns_insufficient_data(fresh_storage):
    storage, coordinator = fresh_storage
    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")
    assert decision.decision == "insufficient_data"
    assert decision.score == 0.0
    assert set(decision.missing_agents) == {"analysis", "news", "timing", "macro"}


def test_strong_agreement_triggers_enter_long(fresh_storage):
    storage, coordinator = fresh_storage
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 80))
    storage.save_opinion("news", "TEST", "global", "t1", _opinion("bullish", 60))
    storage.save_opinion("macro", "TEST", "global", "t1", _opinion("neutral", 50))
    # No market_state bar posted -> Timing is simply absent, not an error
    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")

    # weighted_sum = 80*0.4 + 60*0.25 + 50*0*0.15(neutral=0) = 32 + 15 + 0 = 47
    # available_weight = 0.4 + 0.25 + 0.15 = 0.80 (timing missing) -> clears the 0.6 minimum
    # score = 47 / 0.80 = 58.75
    assert decision.decision == "enter_long"
    assert decision.direction == "bullish"
    assert decision.score == pytest.approx(58.75, abs=0.01)
    assert "timing" in decision.missing_agents


def test_strong_bearish_agreement_triggers_enter_short(fresh_storage):
    storage, coordinator = fresh_storage
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bearish", 90))
    storage.save_opinion("news", "TEST", "global", "t1", _opinion("bearish", 70))
    # available_weight = 0.4 + 0.25 = 0.65 -> clears the 0.6 minimum
    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")
    assert decision.decision == "enter_short"
    assert decision.direction == "bearish"
    assert decision.score < 0


def test_weak_signal_stays_no_trade(fresh_storage):
    storage, coordinator = fresh_storage
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 20))
    storage.save_opinion("news", "TEST", "global", "t1", _opinion("neutral", 50))
    # available_weight = 0.4 + 0.25 = 0.65 -> clears the 0.6 minimum, so this
    # is genuinely testing "enough evidence but not enough conviction",
    # not just "not enough evidence" (that's the min-weight test below).
    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")
    assert decision.decision == "no_trade"
    assert decision.direction == "neutral"


def test_urgent_conflict_dampens_score(fresh_storage):
    storage, coordinator = fresh_storage
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 80))
    storage.save_opinion("news", "TEST", "global", "t1", _opinion("bearish", 70, flags=["urgent"]))
    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")

    assert "analysis_news_conflict_urgent_dampened" in decision.conflict_flags
    # undampened score would be (32 - 17.5) / 0.65 = 22.3; dampened halves it
    assert decision.score == pytest.approx(11.15, abs=0.5)


def test_non_urgent_conflict_flagged_but_not_dampened(fresh_storage):
    storage, coordinator = fresh_storage
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 80))
    storage.save_opinion("news", "TEST", "global", "t1", _opinion("bearish", 70))  # no urgent flag
    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")

    assert "analysis_news_conflict" in decision.conflict_flags
    assert "analysis_news_conflict_urgent_dampened" not in decision.conflict_flags


def test_missing_agents_reported_correctly(fresh_storage):
    storage, coordinator = fresh_storage
    storage.save_opinion("news", "TEST", "global", "t1", _opinion("bullish", 60))
    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")
    assert set(decision.missing_agents) == {"analysis", "timing", "macro"}


def test_threshold_is_configurable_via_env(fresh_storage, monkeypatch):
    storage, coordinator = fresh_storage
    monkeypatch.setenv("COORDINATOR_THRESHOLD", "5")
    importlib.reload(coordinator)

    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 20))
    storage.save_opinion("news", "TEST", "global", "t1", _opinion("bullish", 20))
    # available_weight = 0.4 + 0.25 = 0.65 -> clears the 0.6 minimum
    # weighted_sum = 20*0.4 + 20*0.25 = 8 + 5 = 13; score = 13/0.65 = 20, well over threshold 5
    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")
    assert decision.decision == "enter_long"


# --- Tier-1 safety fixes (external review, Aug 2026) ---------------------


def test_analysis_alone_is_insufficient_data(fresh_storage):
    """The specific scenario an external review flagged: Analysis
    alone (40% weight) used to be able to single-handedly trigger a
    trade. It no longer can — 40% < the 60% minimum combined weight
    required before any directional call is made."""
    storage, coordinator = fresh_storage
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 90))
    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")
    assert decision.decision == "insufficient_data"
    assert decision.score == 0.0


def test_min_available_weight_configurable_via_env(fresh_storage, monkeypatch):
    storage, coordinator = fresh_storage
    monkeypatch.setenv("MIN_AVAILABLE_WEIGHT", "0.3")
    importlib.reload(coordinator)

    # Analysis alone is 40% weight, now above the lowered 30% minimum
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 90))
    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")
    assert decision.decision == "enter_long"


def test_stale_opinion_excluded_like_missing(fresh_storage, monkeypatch):
    """An opinion older than its type's max age is treated as if the
    agent never ran — not used in the score, and reported separately
    from a genuinely-missing agent. Min-weight lowered here so this
    test isolates the staleness behavior specifically, independent of
    the separate min-available-weight safeguard tested above.
    ANALYSIS_REQUIRED also disabled here (Tier 3.24) — analysis being
    stale means it's excluded from opinions exactly like missing, which
    the analysis_required gate would otherwise force to
    insufficient_data before this test ever reaches the quorum math it
    means to isolate; that gate's own behavior is covered separately in
    test_analysis_required_gate_* below."""
    storage, coordinator = fresh_storage
    monkeypatch.setenv("MIN_AVAILABLE_WEIGHT", "0.2")
    monkeypatch.setenv("ANALYSIS_REQUIRED", "false")
    importlib.reload(coordinator)

    stale_ts = _minutes_ago_iso(999)  # far older than ANALYSIS_MAX_AGE_MINUTES (15)
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 90, timestamp=stale_ts))
    storage.save_opinion("news", "TEST", "global", "t1", _opinion("bullish", 60))
    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")

    assert "analysis" in decision.stale_agents
    assert "analysis" not in decision.contributions  # excluded from the score entirely
    assert "news" in decision.contributions  # the fresh one still counts


def test_unparseable_timestamp_treated_as_stale(fresh_storage):
    """An opinion with a timestamp that isn't valid ISO-8601 is
    treated as stale (untrustworthy), not silently assumed fresh."""
    storage, coordinator = fresh_storage
    storage.save_opinion(
        "analysis", "TEST", "5m", "t1", _opinion("bullish", 90, timestamp="not-a-real-timestamp")
    )
    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")
    assert "analysis" in decision.stale_agents


def test_stale_and_missing_are_disjoint(fresh_storage):
    """A second external review caught that a stale opinion used to
    appear in BOTH missing_agents and stale_agents (missing_agents was
    computed as "not in the filtered opinions dict", which included
    agents removed for staleness). They must now be mutually exclusive
    — an agent is either present, missing (never ran), or stale (ran,
    too old), never two of these at once."""
    storage, coordinator = fresh_storage
    stale_ts = _minutes_ago_iso(999)
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 90, timestamp=stale_ts))
    # news never runs at all -> genuinely missing
    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")

    assert "analysis" in decision.stale_agents
    assert "analysis" not in decision.missing_agents
    assert "news" in decision.missing_agents
    assert "news" not in decision.stale_agents
    # no agent appears in both lists
    assert set(decision.stale_agents).isdisjoint(set(decision.missing_agents))


def test_future_timestamp_treated_as_stale(fresh_storage):
    """A materially future-dated opinion is a data integrity problem,
    not a fast clock — must not be treated as fresh."""
    storage, coordinator = fresh_storage
    from datetime import datetime, timedelta, timezone
    future_ts = (datetime.now(timezone.utc) + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 90, timestamp=future_ts))
    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")
    assert "analysis" in decision.stale_agents


def test_opinions_used_captures_exact_snapshot(fresh_storage):
    """CoordinatorDecision.opinions_used carries exactly the opinions
    the score was computed from — this is what lets a trade candidate
    be built as an atomic snapshot, without a second, separately-timed
    database read that could see different data."""
    storage, coordinator = fresh_storage
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 80))
    storage.save_opinion("news", "TEST", "global", "t1", _opinion("bullish", 60))
    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")

    assert "analysis" in decision.opinions_used
    assert "news" in decision.opinions_used
    assert decision.opinions_used["analysis"]["confidence"] == 80


# --- Tier 2.5 (replay/versioning: config_version + _score_opinions) ------


def test_config_version_records_live_config(fresh_storage):
    """Every decision now records exactly which weights/threshold/
    min_available_weight it was scored under — not whatever the env
    vars happen to be whenever this is read back later."""
    storage, coordinator = fresh_storage
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 80))
    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")

    assert decision.config_version["weights"] == coordinator.WEIGHTS
    assert decision.config_version["threshold"] == coordinator.DECISION_THRESHOLD
    assert decision.config_version["min_available_weight"] == coordinator.MIN_AVAILABLE_WEIGHT


def test_config_version_present_even_on_insufficient_data(fresh_storage):
    storage, coordinator = fresh_storage
    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")
    assert decision.decision == "insufficient_data"
    assert decision.config_version["threshold"] == coordinator.DECISION_THRESHOLD


def test_score_opinions_matches_compute_decision_under_live_config(fresh_storage):
    """compute_decision() is now just _gather_opinions() +
    _score_opinions(live config) — calling _score_opinions() directly
    with the same gathered snapshot and the same live config must
    produce an identical result."""
    storage, coordinator = fresh_storage
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 80))
    storage.save_opinion("news", "TEST", "global", "t1", _opinion("bullish", 60))

    opinions, missing, stale = coordinator._gather_opinions(symbol="TEST", timeframe="5m")
    direct = coordinator._score_opinions(
        symbol="TEST", timeframe="5m",
        opinions=opinions, missing_agents=missing, stale_agents=stale,
        weights=coordinator.WEIGHTS,
        threshold=coordinator.DECISION_THRESHOLD,
        min_available_weight=coordinator.MIN_AVAILABLE_WEIGHT,
    )
    via_compute = coordinator.compute_decision(symbol="TEST", timeframe="5m")

    assert direct.decision == via_compute.decision
    assert direct.score == pytest.approx(via_compute.score, abs=0.01)


def test_score_opinions_hypothetical_weights_override_live_config(fresh_storage):
    """The whole point of the extraction: replay a frozen opinions
    snapshot under a DIFFERENT hypothetical config without touching
    any env var or the live WEIGHTS dict."""
    storage, coordinator = fresh_storage
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 50))
    opinions, missing, stale = coordinator._gather_opinions(symbol="TEST", timeframe="5m")

    # Live config: analysis alone (40%) is below the 60% minimum -> insufficient_data
    live = coordinator._score_opinions(
        symbol="TEST", timeframe="5m",
        opinions=opinions, missing_agents=missing, stale_agents=stale,
        weights=coordinator.WEIGHTS, threshold=coordinator.DECISION_THRESHOLD,
        min_available_weight=coordinator.MIN_AVAILABLE_WEIGHT,
    )
    assert live.decision == "insufficient_data"

    # Hypothetical: give analysis 100% weight and lower the minimum -> now enough
    hypothetical_weights = {"analysis": 1.0}
    replayed = coordinator._score_opinions(
        symbol="TEST", timeframe="5m",
        opinions=opinions, missing_agents=missing, stale_agents=stale,
        weights=hypothetical_weights, threshold=25.0, min_available_weight=0.5,
    )
    assert replayed.decision == "enter_long"
    assert replayed.config_version["weights"] == hypothetical_weights
    # live WEIGHTS untouched by the hypothetical call
    assert coordinator.WEIGHTS == {"analysis": 0.40, "news": 0.25, "timing": 0.20, "macro": 0.15}


def test_score_opinions_tolerates_weights_missing_an_agent(fresh_storage):
    """weights.get(agent, 0), not weights[agent] — a hypothetical
    weights dict that omits an agent (e.g. asking 'what if timing had
    zero weight') must not KeyError, it should just contribute 0."""
    storage, coordinator = fresh_storage
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 80))
    storage.save_opinion("news", "TEST", "global", "t1", _opinion("bullish", 60))
    opinions, missing, stale = coordinator._gather_opinions(symbol="TEST", timeframe="5m")

    partial_weights = {"analysis": 0.5}  # news deliberately omitted
    decision = coordinator._score_opinions(
        symbol="TEST", timeframe="5m",
        opinions=opinions, missing_agents=missing, stale_agents=stale,
        weights=partial_weights, threshold=10.0, min_available_weight=0.1,
    )
    assert decision.contributions["news"]["weight"] == 0
    assert decision.contributions["news"]["contribution"] == 0


# --- Tier 2.8 (Coordinator redesign: Timing excluded from directional
# evidence, kept as a separate gate) -------------------------------------


def test_analysis_alone_stays_insufficient_data_even_with_timing_present(fresh_storage):
    """THE regression test for the bug the external review flagged:
    before Tier 2.8, Analysis (40%) + a present-but-neutral Timing
    (20%) cleared the 60% MIN_AVAILABLE_WEIGHT minimum trivially,
    letting Analysis alone single-handedly trigger a trade whenever a
    market bar happened to exist (i.e. almost always in production).
    Posting a bar (so Timing IS present and IS gathered into opinions)
    is the part the older test_analysis_alone_is_insufficient_data
    never actually exercised — that test posts no bar, so Timing was
    already absent there and passed by coincidence, not because the
    fix worked. This one posts a kill-zone bar specifically so Timing
    is present and confirms the decision is still insufficient_data,
    and that Timing is visible in opinions_used and missing_agents is
    empty (proving it really was gathered, not skipped)."""
    storage, coordinator = fresh_storage
    bar_ts = _ny_time_to_utc_iso(2026, 8, 11, 10, 0)  # Tuesday, NY AM kill zone
    _save_bar(storage, "TEST", "5m", bar_ts)
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 90))

    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")

    assert "timing" in decision.opinions_used  # Timing really was gathered...
    assert "timing" not in decision.missing_agents  # ...not silently skipped
    assert decision.decision == "insufficient_data"  # ...yet still correctly insufficient
    assert decision.score == 0.0


def test_analysis_plus_news_sufficient_regardless_of_timing(fresh_storage):
    """Two real directional agents (65% of the 80%-wide directional
    pool) should clear the minimum whether or not Timing happens to be
    present — the gate is now about directional evidence only."""
    storage, coordinator = fresh_storage
    bar_ts = _ny_time_to_utc_iso(2026, 8, 11, 10, 0)  # kill zone -> no dampening
    _save_bar(storage, "TEST", "5m", bar_ts)
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 80))
    storage.save_opinion("news", "TEST", "global", "t1", _opinion("bullish", 60))
    storage.save_opinion("macro", "TEST", "global", "t1", _opinion("neutral", 50))

    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")
    assert decision.decision == "enter_long"
    # identical math to test_strong_agreement_triggers_enter_long, now
    # with Timing present too (kill zone, no dampen) -- confirms
    # Timing's presence has zero effect on the score when its flags
    # don't trigger veto/dampen.
    assert decision.score == pytest.approx(58.75, abs=0.01)


def test_timing_context_populated_when_bar_present(fresh_storage):
    storage, coordinator = fresh_storage
    bar_ts = _ny_time_to_utc_iso(2026, 8, 11, 10, 0)
    _save_bar(storage, "TEST", "5m", bar_ts)
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 80))
    storage.save_opinion("news", "TEST", "global", "t1", _opinion("bullish", 60))

    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")
    assert decision.timing_context is not None
    assert decision.timing_context["session_label"] == "new_york"
    assert decision.timing_context["flags"] == []


def test_timing_context_none_when_no_bar(fresh_storage):
    storage, coordinator = fresh_storage
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 80))
    storage.save_opinion("news", "TEST", "global", "t1", _opinion("bullish", 60))

    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")
    assert decision.timing_context is None


def test_timing_low_liquidity_dampens_score(fresh_storage):
    """A weekday bar outside every kill zone halves the score instead
    of silently doing nothing — Timing's confidence swing (65 in a
    kill zone vs 20 outside one) used to have zero effect on the
    decision either way since its direction was always neutral; now
    it visibly matters via the dampener."""
    storage, coordinator = fresh_storage
    bar_ts = _ny_time_to_utc_iso(2026, 8, 11, 12, 0)  # Tuesday, gap between AM/PM kill zones
    _save_bar(storage, "TEST", "5m", bar_ts)
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 80))
    storage.save_opinion("news", "TEST", "global", "t1", _opinion("bullish", 60))
    storage.save_opinion("macro", "TEST", "global", "t1", _opinion("neutral", 50))

    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")
    assert decision.timing_context["flags"] == ["low_liquidity"]
    assert "timing_low_liquidity_dampened" in decision.conflict_flags
    # undampened score would be 58.75 (same math as the kill-zone test above)
    assert decision.score == pytest.approx(29.375, abs=0.01)


def test_timing_market_closed_vetoes_score_to_zero(fresh_storage):
    """A weekend-timestamped bar (edge case — shouldn't normally
    happen, but defensively handled) forces the score to 0 regardless
    of how strong the directional agents' agreement is, and keeps the
    decision at no_trade rather than trading on a market that's shut."""
    storage, coordinator = fresh_storage
    bar_ts = _ny_time_to_utc_iso(2026, 8, 15, 10, 0)  # Saturday
    _save_bar(storage, "TEST", "5m", bar_ts)
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 95))
    storage.save_opinion("news", "TEST", "global", "t1", _opinion("bullish", 90))

    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")
    assert decision.timing_context["flags"] == ["market_closed"]
    assert "timing_market_closed" in decision.conflict_flags
    assert decision.score == 0.0
    assert decision.decision == "no_trade"


def test_directional_agents_constant_excludes_timing(fresh_storage):
    storage, coordinator = fresh_storage
    assert coordinator.DIRECTIONAL_AGENTS == frozenset({"analysis", "news", "macro"})
    assert "timing" not in coordinator.DIRECTIONAL_AGENTS


def test_replay_style_hypothetical_config_reveals_the_old_bug(fresh_storage):
    """Demonstrates the point of Tier 2.5 (replay) + Tier 2.8
    (redesign) together: replaying the SAME frozen opinions_used from
    the regression test above under a hypothetical config that treats
    Timing as directional (old, buggy shape: including it in the
    available-weight pool the same way pre-Tier-2.8 code effectively
    did) reproduces the old wrong enter_long -- proving the live
    (non-hypothetical) fix above is the actual behavior change, not
    just a coincidence of this particular test's numbers."""
    storage, coordinator = fresh_storage
    bar_ts = _ny_time_to_utc_iso(2026, 8, 11, 10, 0)
    _save_bar(storage, "TEST", "5m", bar_ts)
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 90))

    opinions, missing, stale = coordinator._gather_opinions(symbol="TEST", timeframe="5m")
    live_decision = coordinator._score_opinions(
        symbol="TEST", timeframe="5m",
        opinions=opinions, missing_agents=missing, stale_agents=stale,
        weights=coordinator.WEIGHTS, threshold=coordinator.DECISION_THRESHOLD,
        min_available_weight=coordinator.MIN_AVAILABLE_WEIGHT,
    )
    assert live_decision.decision == "insufficient_data"  # the Tier 2.8 fix, confirmed again directly


# --- Tier 2.9 (calendar integrity: News "urgent" dampens regardless of
# analysis/news agreement, not just during a conflict) -------------------


def test_news_urgent_dampens_score_even_when_agreeing_with_analysis(fresh_storage):
    """The bug this fixes: before Tier 2.9, "urgent" only dampened the
    score INSIDE the opposing-conflict branch, so two agents that
    AGREED (e.g. both bullish right before an FOMC decision) got zero
    dampening despite the same flagged imminent-event risk."""
    storage, coordinator = fresh_storage
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 80))
    storage.save_opinion("news", "TEST", "global", "t1", _opinion("bullish", 60, flags=["urgent"]))

    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")

    assert "news_urgent_dampened" in decision.conflict_flags
    assert "analysis_news_conflict" not in decision.conflict_flags  # they agree, no conflict
    # undampened score would be 47/0.65 = 72.31; halved by the urgent dampener
    assert decision.score == pytest.approx(36.15, abs=0.1)


def test_news_urgent_and_opposing_still_uses_single_combined_flag(fresh_storage):
    """When BOTH conditions apply (opposing direction AND urgent), the
    result stays a single 0.5 dampen under the original combined flag
    name — not two separate dampens (0.25x) or two separate flags for
    one event. Same scenario/numbers as
    test_urgent_conflict_dampens_score above, re-asserted here as the
    Tier 2.9 regression check that the refactor didn't change it."""
    storage, coordinator = fresh_storage
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 80))
    storage.save_opinion("news", "TEST", "global", "t1", _opinion("bearish", 70, flags=["urgent"]))

    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")

    assert decision.conflict_flags == ["analysis_news_conflict_urgent_dampened"]
    assert decision.score == pytest.approx(11.15, abs=0.5)


def test_no_news_opinion_no_urgent_dampening(fresh_storage):
    """No News opinion at all -> nothing to read a flag from; the new
    urgent check must not error or fire on a missing agent."""
    storage, coordinator = fresh_storage
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 90))
    storage.save_opinion("macro", "TEST", "global", "t1", _opinion("bullish", 60))

    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")
    assert "news_urgent_dampened" not in decision.conflict_flags


# ---------------------------------------------------------------------------
# Tier 3.1: causal integrity — compute_decision()/_gather_opinions()
# accept explicit bar/analysis_opinion anchors instead of always
# independently re-querying "latest".
# ---------------------------------------------------------------------------

def test_compute_decision_uses_explicit_bar_anchor_for_timing(fresh_storage):
    """A weekend timestamp should veto the score via Timing's
    market_closed flag — proving the explicitly-passed bar= is what
    actually drives Timing, not whatever get_latest() would return."""
    storage, coordinator = fresh_storage
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 90))
    storage.save_opinion("news", "TEST", "global", "t1", _opinion("bullish", 80))
    storage.save_opinion("macro", "TEST", "global", "t1", _opinion("bullish", 70))

    # A Saturday in NY time -> Timing's market_closed veto.
    weekend_bar = {"timestamp": "2026-08-15T15:00:00Z"}
    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m", bar=weekend_bar)

    assert decision.score == 0.0
    assert "timing_market_closed" in decision.conflict_flags


def test_compute_decision_uses_explicit_analysis_opinion_over_stored(fresh_storage):
    """Even if a DIFFERENT Analysis opinion is sitting in storage, the
    explicitly-passed analysis_opinion is what gets scored — this is
    the exact guarantee that keeps a candidate's frozen decision
    consistent with the specific Analysis run that triggered it."""
    storage, coordinator = fresh_storage
    storage.save_opinion("analysis", "TEST", "5m", "t-stored", _opinion("bearish", 90))
    storage.save_opinion("news", "TEST", "global", "t1", _opinion("bullish", 60))
    storage.save_opinion("macro", "TEST", "global", "t1", _opinion("neutral", 50))

    anchored_opinion = _opinion("bullish", 80)
    decision = coordinator.compute_decision(
        symbol="TEST", timeframe="5m", analysis_opinion=anchored_opinion
    )

    assert decision.opinions_used["analysis"]["direction"] == "bullish"
    assert decision.opinions_used["analysis"]["confidence"] == 80


def test_compute_decision_without_anchors_falls_back_to_latest(fresh_storage):
    """No behavior change for existing callers (the manual
    /coordinator/decide endpoint, and every test above this one in
    this file) that never pass bar=/analysis_opinion=."""
    storage, coordinator = fresh_storage
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 80))
    storage.save_opinion("news", "TEST", "global", "t1", _opinion("bullish", 60))
    storage.save_opinion("macro", "TEST", "global", "t1", _opinion("neutral", 50))

    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")
    assert decision.decision == "enter_long"
    assert "timing" not in decision.opinions_used  # no bar in storage, as before


# --- Tier 3.24 (analysis_required explicit gate, project-owner design
# decision — fifth external review's open question, resolved by the
# owner, not by data) -----------------------------------------------------


def test_analysis_required_defaults_true_and_is_recorded_in_config_version(fresh_storage):
    """ANALYSIS_REQUIRED defaults to True (no env var set), and every
    decision now records it in config_version alongside
    weights/threshold/min_available_weight, live or not."""
    storage, coordinator = fresh_storage
    assert coordinator.ANALYSIS_REQUIRED is True
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 80))
    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")
    assert decision.config_version["analysis_required"] is True


def test_analysis_required_blocks_news_macro_only_decision_even_under_hypothetical_quorum(fresh_storage):
    """THE regression test for the exact scenario Tier 3.24 exists to
    prevent: under a hypothetical weights/min_available_weight config
    where News+Macro alone could clear quorum WITHOUT Analysis (not
    true under the live weights today, per Tier 3.21's proof, but
    could become true after a future retune), analysis_required=True
    must still force insufficient_data — independent of, and checked
    before, the quorum math."""
    storage, coordinator = fresh_storage
    storage.save_opinion("news", "TEST", "global", "t1", _opinion("bullish", 90))
    storage.save_opinion("macro", "TEST", "global", "t1", _opinion("bullish", 90))
    # deliberately no analysis opinion saved at all -- missing, not stale
    opinions, missing, stale = coordinator._gather_opinions(symbol="TEST", timeframe="5m")
    assert "analysis" in missing
    assert "analysis" not in opinions

    # Hypothetical config: news+macro alone (40% combined) easily clears
    # a lowered 30% minimum -- would be "enter_long" if analysis_required
    # weren't checked first.
    hypothetical_weights = {"news": 0.25, "macro": 0.15}
    without_gate = coordinator._score_opinions(
        symbol="TEST", timeframe="5m",
        opinions=opinions, missing_agents=missing, stale_agents=stale,
        weights=hypothetical_weights, threshold=10.0, min_available_weight=0.3,
        analysis_required=False,
    )
    assert without_gate.decision == "enter_long"  # proves the hypothetical quorum really would pass

    with_gate = coordinator._score_opinions(
        symbol="TEST", timeframe="5m",
        opinions=opinions, missing_agents=missing, stale_agents=stale,
        weights=hypothetical_weights, threshold=10.0, min_available_weight=0.3,
        analysis_required=True,
    )
    assert with_gate.decision == "insufficient_data"
    assert with_gate.score == 0.0
    assert "analysis_required=True" in with_gate.summary
    assert with_gate.config_version["analysis_required"] is True


def test_analysis_required_is_a_no_op_under_the_live_config(fresh_storage):
    """Confirms the claim made when this was proposed: under the REAL
    live weights/threshold/min_available_weight, adding the explicit
    gate changes no decision that the quorum math didn't already
    produce on its own -- analysis-missing already meant
    insufficient_data before Tier 3.24, and still does, for the same
    end result (though now via an explicit, documented rule instead of
    an accident of the weights)."""
    storage, coordinator = fresh_storage
    storage.save_opinion("news", "TEST", "global", "t1", _opinion("bullish", 90))
    storage.save_opinion("macro", "TEST", "global", "t1", _opinion("bullish", 90))
    # no analysis opinion -- live MIN_AVAILABLE_WEIGHT=0.6 already fails
    # quorum for news+macro alone (40% of the 80%-wide directional pool)

    with_gate = coordinator.compute_decision(symbol="TEST", timeframe="5m")
    assert with_gate.decision == "insufficient_data"

    opinions, missing, stale = coordinator._gather_opinions(symbol="TEST", timeframe="5m")
    without_gate = coordinator._score_opinions(
        symbol="TEST", timeframe="5m",
        opinions=opinions, missing_agents=missing, stale_agents=stale,
        weights=coordinator.WEIGHTS, threshold=coordinator.DECISION_THRESHOLD,
        min_available_weight=coordinator.MIN_AVAILABLE_WEIGHT, analysis_required=False,
    )
    assert without_gate.decision == "insufficient_data"  # same outcome either way, live config


def test_analysis_required_does_not_block_present_but_neutral_analysis(fresh_storage):
    """Scoped narrowly on purpose (the project owner's explicit choice):
    the gate checks Analysis's mere PRESENCE, not its direction. A
    present-but-neutral Analysis opinion still satisfies it -- News
    alone can still swing the decision, exactly as it could before
    Tier 3.24, since this isn't a "must be directional" gate."""
    storage, coordinator = fresh_storage
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("neutral", 50))
    storage.save_opinion("news", "TEST", "global", "t1", _opinion("bullish", 90))
    storage.save_opinion("macro", "TEST", "global", "t1", _opinion("bullish", 90))

    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")

    assert "analysis" in decision.opinions_used  # present, just neutral
    assert decision.decision != "insufficient_data"  # gate did not block it


def test_analysis_required_false_via_env_falls_back_to_quorum_only(fresh_storage, monkeypatch):
    """ANALYSIS_REQUIRED=false (env override) restores the pre-Tier-3.24
    quorum-only behavior for compute_decision(), not just for direct
    _score_opinions() callers.

    Explicitly reloads coordinator back to a clean-env state before
    returning (rather than relying on monkeypatch's automatic env-var
    teardown, which does NOT re-run importlib.reload) -- other test
    modules (e.g. test_experiments.py, which imports several names
    directly out of app.coordinator at its own reload time) would
    otherwise silently inherit this test's mutated MIN_AVAILABLE_WEIGHT/
    ANALYSIS_REQUIRED if this happened to be the last test in this file
    to touch coordinator's module state."""
    storage, coordinator = fresh_storage
    monkeypatch.setenv("ANALYSIS_REQUIRED", "false")
    monkeypatch.setenv("MIN_AVAILABLE_WEIGHT", "0.3")
    importlib.reload(coordinator)
    assert coordinator.ANALYSIS_REQUIRED is False

    storage.save_opinion("news", "TEST", "global", "t1", _opinion("bullish", 90))
    storage.save_opinion("macro", "TEST", "global", "t1", _opinion("bullish", 90))
    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")

    assert decision.decision == "enter_long"  # news+macro (40%) clears the lowered 30% minimum
    assert decision.config_version["analysis_required"] is False

    monkeypatch.delenv("ANALYSIS_REQUIRED", raising=False)
    monkeypatch.delenv("MIN_AVAILABLE_WEIGHT", raising=False)
    importlib.reload(coordinator)
