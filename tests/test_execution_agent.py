"""
Unit tests for app.execution_agent's deterministic trade-geometry
validation — no LLM, no network. This is the check added after an
external review pointed out that "valid JSON" was being treated as
"valid trade": the model's stop/target prices were never checked
against basic trade geometry before being displayed as a normal plan.

Run with: pytest tests/test_execution_agent.py -v
"""

from app.execution_agent import _validate_trade_geometry


def test_valid_long_passes():
    assert _validate_trade_geometry("bullish", 100.0, 95.0, [110.0]) is None


def test_valid_short_passes():
    assert _validate_trade_geometry("bearish", 100.0, 105.0, [90.0]) is None


def test_long_rejects_stop_above_entry():
    reason = _validate_trade_geometry("bullish", 100.0, 105.0, [110.0])
    assert reason is not None
    assert "stop_loss" in reason


def test_short_rejects_stop_below_entry():
    reason = _validate_trade_geometry("bearish", 100.0, 95.0, [90.0])
    assert reason is not None
    assert "stop_loss" in reason


def test_long_rejects_target_below_entry():
    reason = _validate_trade_geometry("bullish", 100.0, 95.0, [90.0])
    assert reason is not None
    assert "targets" in reason


def test_short_rejects_target_above_entry():
    reason = _validate_trade_geometry("bearish", 100.0, 105.0, [110.0])
    assert reason is not None
    assert "targets" in reason


def test_rejects_poor_reward_risk_ratio():
    # risk = 100-90 = 10, reward = 105-100 = 5 -> ratio 0.5, below the 1.0 default minimum
    reason = _validate_trade_geometry("bullish", 100.0, 90.0, [105.0])
    assert reason is not None
    assert "reward:risk" in reason


def test_uses_nearest_target_as_the_conservative_check():
    # Two targets: one clears the R:R minimum, one doesn't. The nearest
    # (smallest reward) must be the one checked — the conservative case.
    # risk = 10; nearest target reward = 5 (ratio 0.5, fails); farther
    # target reward = 20 (ratio 2.0, would pass if checked instead).
    reason = _validate_trade_geometry("bullish", 100.0, 90.0, [105.0, 120.0])
    assert reason is not None  # must fail on the nearer/weaker target


def test_rejects_zero_risk():
    reason = _validate_trade_geometry("bullish", 100.0, 100.0, [110.0])
    assert reason is not None


def test_rejects_no_targets():
    reason = _validate_trade_geometry("bullish", 100.0, 95.0, [])
    assert reason == "no targets provided"


def test_rejects_unrecognized_direction():
    reason = _validate_trade_geometry("sideways", 100.0, 95.0, [110.0])
    assert reason is not None
    assert "unrecognized direction" in reason
