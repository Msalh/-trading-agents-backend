"""
Unit tests for app.risk_agent — deterministic risk math only. No
LLM, no network, no database. Env vars are read at module import
time, so tests reload the module after monkeypatching them.

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


def test_no_action_when_coordinator_says_no_trade(risk_module):
    ra = risk_module()
    result = ra.evaluate_risk("TEST", "5m", {"decision": "no_trade"}, {"atr": 25.0})
    assert result.decision == "no_action"


def test_no_action_when_coordinator_says_insufficient_data(risk_module):
    ra = risk_module()
    result = ra.evaluate_risk("TEST", "5m", {"decision": "insufficient_data"}, {"atr": 25.0})
    assert result.decision == "no_action"


def test_approve_within_budget(risk_module):
    ra = risk_module(BASE_POSITION_SIZE="1")
    # atr=25 -> risk/contract = 25*2 = $50; budget = 50% * 2000 = $1000
    result = ra.evaluate_risk("TEST", "5m", {"decision": "enter_long"}, {"atr": 25.0})
    assert result.decision == "approve"
    assert result.suggested_size == 1


def test_approve_at_exact_budget_edge(risk_module):
    # remaining=500, budget=250; 5 contracts * $50 = $250 exactly
    ra = risk_module(CURRENT_DRAWDOWN_USED="1500", BASE_POSITION_SIZE="5")
    result = ra.evaluate_risk("TEST", "5m", {"decision": "enter_long"}, {"atr": 25.0})
    assert result.decision == "approve"
    assert result.suggested_size == 5


def test_modify_reduces_size_to_fit_budget(risk_module):
    # remaining=500, budget=250; 10 contracts would be $500, over budget
    ra = risk_module(CURRENT_DRAWDOWN_USED="1500", BASE_POSITION_SIZE="10")
    result = ra.evaluate_risk("TEST", "5m", {"decision": "enter_long"}, {"atr": 25.0})
    assert result.decision == "modify"
    assert result.original_size == 10
    assert result.suggested_size == 5  # floor(250 / 50)


def test_reject_when_drawdown_exhausted(risk_module):
    ra = risk_module(CURRENT_DRAWDOWN_USED="2000")  # equals MAX_DRAWDOWN
    result = ra.evaluate_risk("TEST", "5m", {"decision": "enter_long"}, {"atr": 25.0})
    assert result.decision == "reject"
    assert "drawdown_exhausted" in result.flags


def test_reject_when_max_positions_reached(risk_module):
    ra = risk_module(MAX_OPEN_POSITIONS="1", CURRENT_OPEN_POSITIONS="1")
    result = ra.evaluate_risk("TEST", "5m", {"decision": "enter_long"}, {"atr": 25.0})
    assert result.decision == "reject"
    assert "max_positions_reached" in result.flags
    assert result.suggested_size is None


def test_reject_when_no_atr_available(risk_module):
    ra = risk_module()
    result = ra.evaluate_risk("TEST", "5m", {"decision": "enter_long"}, {"atr": None})
    assert result.decision == "reject"
    assert "insufficient_data" in result.flags


def test_reject_when_no_market_state_at_all(risk_module):
    ra = risk_module()
    result = ra.evaluate_risk("TEST", "5m", {"decision": "enter_long"}, None)
    assert result.decision == "reject"
    assert "insufficient_data" in result.flags


def test_reject_when_budget_too_small_for_one_contract(risk_module):
    # remaining=10, budget=5; even 1 contract at $50 risk is unaffordable
    ra = risk_module(CURRENT_DRAWDOWN_USED="1990", BASE_POSITION_SIZE="1")
    result = ra.evaluate_risk("TEST", "5m", {"decision": "enter_long"}, {"atr": 25.0})
    assert result.decision == "reject"
    assert "budget_too_small_for_min_size" in result.flags


def test_max_positions_takes_priority_over_drawdown_check(risk_module):
    # both conditions true; max_positions check runs first
    ra = risk_module(MAX_OPEN_POSITIONS="1", CURRENT_OPEN_POSITIONS="1", CURRENT_DRAWDOWN_USED="2000")
    result = ra.evaluate_risk("TEST", "5m", {"decision": "enter_long"}, {"atr": 25.0})
    assert "max_positions_reached" in result.flags
