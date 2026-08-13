"""
Unit tests for app.risk_agent — deterministic risk math only. No
LLM, no network, no database. Env vars are read at module import
time, so tests reload the module after monkeypatching them.

Tier 2.2: Risk Agent is now two functions instead of one —
evaluate_risk_gate() (no stop price, checks limits/drawdown room
only) and size_position() (sizes from a real entry/stop distance,
never ATR). Tested separately below.

Run with: pytest tests/test_risk_agent.py -v
"""

import importlib

import pytest


@pytest.fixture
def risk_module(monkeypatch):
    def _make(**env_overrides):
        defaults = {
            "ACCOUNT_BALANCE": "50000",
            "MAX_DRAWDOWN": "2000",
            "CURRENT_DRAWDOWN_USED": "0",
            "MAX_OPEN_POSITIONS": "1",
            "CURRENT_OPEN_POSITIONS": "0",
            "BASE_POSITION_SIZE": "1",
            "RISK_FRACTION_PER_TRADE": "0.5",
        }
        defaults.update(env_overrides)
        for k, v in defaults.items():
            monkeypatch.setenv(k, v)
        import app.risk_agent as risk_agent
        importlib.reload(risk_agent)
        return risk_agent

    return _make


# ---------------------------------------------------------------------------
# Stage 1: evaluate_risk_gate — no stop price, limits/drawdown room only
# ---------------------------------------------------------------------------

def test_gate_no_action_when_coordinator_says_no_trade(risk_module):
    ra = risk_module()
    result = ra.evaluate_risk_gate("TEST", "5m", {"decision": "no_trade"})
    assert result.decision == "no_action"
    assert result.stage == "gate"


def test_gate_no_action_when_coordinator_says_insufficient_data(risk_module):
    ra = risk_module()
    result = ra.evaluate_risk_gate("TEST", "5m", {"decision": "insufficient_data"})
    assert result.decision == "no_action"


def test_gate_clears_to_pending_execution(risk_module):
    ra = risk_module()
    result = ra.evaluate_risk_gate("TEST", "5m", {"decision": "enter_long"})
    assert result.decision == "pending_execution"
    assert result.suggested_size is None  # nothing sized yet — no stop exists


def test_gate_rejects_when_max_positions_reached(risk_module):
    ra = risk_module(MAX_OPEN_POSITIONS="1", CURRENT_OPEN_POSITIONS="1")
    result = ra.evaluate_risk_gate("TEST", "5m", {"decision": "enter_long"})
    assert result.decision == "reject"
    assert "max_positions_reached" in result.flags


def test_gate_rejects_when_drawdown_exhausted(risk_module):
    ra = risk_module(CURRENT_DRAWDOWN_USED="2000")  # equals MAX_DRAWDOWN
    result = ra.evaluate_risk_gate("TEST", "5m", {"decision": "enter_long"})
    assert result.decision == "reject"
    assert "drawdown_exhausted" in result.flags


def test_gate_max_positions_takes_priority_over_drawdown_check(risk_module):
    ra = risk_module(MAX_OPEN_POSITIONS="1", CURRENT_OPEN_POSITIONS="1", CURRENT_DRAWDOWN_USED="2000")
    result = ra.evaluate_risk_gate("TEST", "5m", {"decision": "enter_long"})
    assert "max_positions_reached" in result.flags


def test_gate_never_references_atr(risk_module):
    """The whole point of Tier 2.2: the gate stage doesn't take or
    need ATR/market-bar data at all — it's purely account-state math."""
    ra = risk_module()
    result = ra.evaluate_risk_gate("TEST", "5m", {"decision": "enter_long"})
    assert "atr" not in result.key_data
    assert "atr_points" not in result.key_data


def test_gate_uses_explicit_open_positions_over_env_var(risk_module):
    """Tier 2.3: the caller (main.py) passes the live open-trade count
    explicitly — it must win over the static env var even when they
    disagree, since the env var is now only a fallback."""
    ra = risk_module(MAX_OPEN_POSITIONS="1", CURRENT_OPEN_POSITIONS="0")  # env says room available
    result = ra.evaluate_risk_gate("TEST", "5m", {"decision": "enter_long"}, current_open_positions=1)
    assert result.decision == "reject"
    assert "max_positions_reached" in result.flags
    assert result.key_data["current_open_positions"] == 1


def test_gate_falls_back_to_env_var_when_not_passed(risk_module):
    ra = risk_module(MAX_OPEN_POSITIONS="1", CURRENT_OPEN_POSITIONS="1")
    result = ra.evaluate_risk_gate("TEST", "5m", {"decision": "enter_long"})
    assert result.decision == "reject"


# ---------------------------------------------------------------------------
# Stage 2: size_position — sized from the REAL entry/stop, not ATR
# ---------------------------------------------------------------------------

def test_size_approve_within_budget(risk_module):
    ra = risk_module(BASE_POSITION_SIZE="1")
    # stop distance = 25 pts -> risk/contract = 25*2 = $50; budget = 50% * 2000 = $1000
    result = ra.size_position("TEST", "5m", entry_price=20000.0, stop_loss=19975.0)
    assert result.decision == "approve"
    assert result.suggested_size == 1
    assert result.stage == "size"
    assert "sized_from_actual_stop" in result.flags


def test_size_approve_at_exact_budget_edge(risk_module):
    # remaining=500, budget=250; 5 contracts * $50 = $250 exactly
    ra = risk_module(CURRENT_DRAWDOWN_USED="1500", BASE_POSITION_SIZE="5")
    result = ra.size_position("TEST", "5m", entry_price=20000.0, stop_loss=19975.0)
    assert result.decision == "approve"
    assert result.suggested_size == 5


def test_size_modify_reduces_size_to_fit_budget(risk_module):
    # remaining=500, budget=250; 10 contracts would be $500, over budget
    ra = risk_module(CURRENT_DRAWDOWN_USED="1500", BASE_POSITION_SIZE="10")
    result = ra.size_position("TEST", "5m", entry_price=20000.0, stop_loss=19975.0)
    assert result.decision == "modify"
    assert result.original_size == 10
    assert result.suggested_size == 5  # floor(250 / 50)


def test_size_reject_when_budget_too_small_for_one_contract(risk_module):
    # remaining=10, budget=5; even 1 contract at $50 risk is unaffordable
    ra = risk_module(CURRENT_DRAWDOWN_USED="1990", BASE_POSITION_SIZE="1")
    result = ra.size_position("TEST", "5m", entry_price=20000.0, stop_loss=19975.0)
    assert result.decision == "reject"
    assert "budget_too_small_for_min_size" in result.flags


def test_size_reject_when_drawdown_exhausted(risk_module):
    ra = risk_module(CURRENT_DRAWDOWN_USED="2000")
    result = ra.size_position("TEST", "5m", entry_price=20000.0, stop_loss=19975.0)
    assert result.decision == "reject"
    assert "drawdown_exhausted" in result.flags


def test_size_reject_on_zero_stop_distance(risk_module):
    ra = risk_module()
    result = ra.size_position("TEST", "5m", entry_price=20000.0, stop_loss=20000.0)
    assert result.decision == "reject"
    assert "invalid_stop_distance" in result.flags


def test_size_uses_absolute_distance_regardless_of_direction(risk_module):
    """A short's stop is ABOVE entry — the distance is still positive
    and should size identically to a long with the same point gap."""
    ra = risk_module(BASE_POSITION_SIZE="1")
    long_result = ra.size_position("TEST", "5m", entry_price=20000.0, stop_loss=19975.0)
    short_result = ra.size_position("TEST", "5m", entry_price=20000.0, stop_loss=20025.0)
    assert long_result.decision == short_result.decision == "approve"
    assert long_result.key_data["risk_per_contract_usd"] == short_result.key_data["risk_per_contract_usd"]


def test_size_reflects_tighter_stop_than_atr_would_have_estimated(risk_module):
    """The core Tier 2.2 fix: a real stop tighter than ATR sizes UP
    (more contracts fit the same budget) instead of being stuck at
    whatever ATR would have guessed."""
    ra = risk_module(BASE_POSITION_SIZE="1")
    # A 5-point real stop (much tighter than a typical ~25pt ATR) ->
    # risk/contract = 5*2 = $10; budget $1000 -> up to 100 contracts,
    # but BASE_POSITION_SIZE=1 still approves at 1 (no auto-scale-up),
    # just confirms the real math used the real distance, not ATR.
    result = ra.size_position("TEST", "5m", entry_price=20000.0, stop_loss=19995.0)
    assert result.decision == "approve"
    assert result.key_data["risk_per_contract_usd"] == 10.0
    assert result.key_data["stop_distance_points"] == 5.0
