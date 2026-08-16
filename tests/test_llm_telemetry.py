"""
Unit tests for app.llm_telemetry — Tier 3.15 (LLM call cost/usage
telemetry). No real network/LLM calls: uses small fake objects that
mimic the shape of an anthropic SDK response's `.usage` field.

Run with: pytest tests/test_llm_telemetry.py -v
"""

import importlib
import os
import tempfile

import pytest


@pytest.fixture
def fresh_env(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setenv("DB_PATH", tmp.name)

    import app.storage as storage
    importlib.reload(storage)
    storage.init_db()

    import app.llm_telemetry as llm_telemetry
    importlib.reload(llm_telemetry)

    yield storage, llm_telemetry

    os.unlink(tmp.name)


class _FakeServerToolUse:
    def __init__(self, web_search_requests=0):
        self.web_search_requests = web_search_requests


class _FakeUsage:
    def __init__(self, input_tokens=0, output_tokens=0, cache_creation_input_tokens=0,
                 cache_read_input_tokens=0, server_tool_use=None):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens
        self.cache_read_input_tokens = cache_read_input_tokens
        self.server_tool_use = server_tool_use


class _FakeResponse:
    def __init__(self, usage):
        self.usage = usage


# ---------------------------------------------------------------------------
# estimate_cost_usd
# ---------------------------------------------------------------------------

def test_estimate_cost_usd_input_and_output_tokens(fresh_env):
    _, llm_telemetry = fresh_env
    # 1,000,000 input tokens @ $2/MTok + 1,000,000 output tokens @ $10/MTok
    cost = llm_telemetry.estimate_cost_usd(input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost == pytest.approx(12.0)


def test_estimate_cost_usd_zero_tokens_is_zero(fresh_env):
    _, llm_telemetry = fresh_env
    assert llm_telemetry.estimate_cost_usd() == 0.0


def test_estimate_cost_usd_includes_cache_and_web_search(fresh_env):
    _, llm_telemetry = fresh_env
    cost = llm_telemetry.estimate_cost_usd(
        cache_creation_input_tokens=1_000_000,  # 1.25x base input rate
        cache_read_input_tokens=1_000_000,      # 0.1x base input rate
        web_search_requests=100,                # $0.01 each
    )
    expected = (2.0 * 1.25) + (2.0 * 0.1) + (100 * 0.01)
    assert cost == pytest.approx(expected)


# ---------------------------------------------------------------------------
# _extract_usage
# ---------------------------------------------------------------------------

def test_extract_usage_reads_token_counts(fresh_env):
    _, llm_telemetry = fresh_env
    response = _FakeResponse(_FakeUsage(input_tokens=500, output_tokens=200))
    usage = llm_telemetry._extract_usage(response)
    assert usage["input_tokens"] == 500
    assert usage["output_tokens"] == 200
    assert usage["web_search_requests"] == 0


def test_extract_usage_reads_web_search_count_from_server_tool_use(fresh_env):
    _, llm_telemetry = fresh_env
    response = _FakeResponse(_FakeUsage(input_tokens=500, output_tokens=200, server_tool_use=_FakeServerToolUse(3)))
    usage = llm_telemetry._extract_usage(response)
    assert usage["web_search_requests"] == 3


def test_extract_usage_handles_missing_usage_gracefully(fresh_env):
    _, llm_telemetry = fresh_env

    class _NoUsage:
        pass

    assert llm_telemetry._extract_usage(_NoUsage()) == {}


# ---------------------------------------------------------------------------
# track_llm_call
# ---------------------------------------------------------------------------

def test_track_llm_call_records_a_successful_call(fresh_env):
    storage, llm_telemetry = fresh_env
    response = _FakeResponse(_FakeUsage(input_tokens=100, output_tokens=50))

    with llm_telemetry.track_llm_call("analysis", "claude-sonnet-5", trigger_context="MNQ1!/5m") as call:
        call.record(response)

    recent = storage.get_recent_llm_calls(limit=1)
    assert len(recent) == 1
    row = recent[0]
    assert row["agent"] == "analysis"
    assert row["success"] == 1
    assert row["input_tokens"] == 100
    assert row["output_tokens"] == 50
    assert row["trigger_context"] == "MNQ1!/5m"
    assert row["estimated_cost_usd"] > 0


def test_track_llm_call_records_a_failed_call_and_reraises(fresh_env):
    storage, llm_telemetry = fresh_env

    with pytest.raises(ValueError):
        with llm_telemetry.track_llm_call("execution", "claude-sonnet-5", trigger_context="MNQ1!/5m") as call:
            raise ValueError("boom")

    recent = storage.get_recent_llm_calls(limit=1)
    assert len(recent) == 1
    row = recent[0]
    assert row["success"] == 0
    assert row["error_message"] == "boom"
    assert row["input_tokens"] is None


def test_track_llm_call_logs_null_tokens_when_record_never_called(fresh_env):
    storage, llm_telemetry = fresh_env
    # The API call itself raised before response existed -- record()
    # never reached, but a row must still exist (as a failure).
    with pytest.raises(RuntimeError):
        with llm_telemetry.track_llm_call("news", "claude-sonnet-5") as call:
            raise RuntimeError("api unavailable")

    recent = storage.get_recent_llm_calls(limit=1)
    assert recent[0]["success"] == 0
    assert recent[0]["input_tokens"] is None
    assert recent[0]["estimated_cost_usd"] is None


# ---------------------------------------------------------------------------
# storage.record_llm_call / get_llm_call_summary / get_recent_llm_calls
# ---------------------------------------------------------------------------

def test_get_llm_call_summary_aggregates_by_agent(fresh_env):
    storage, _ = fresh_env
    storage.record_llm_call(
        agent="analysis", model="claude-sonnet-5", trigger_context="MNQ1!/5m",
        success=True, error_message=None, latency_ms=120.0,
        input_tokens=100, output_tokens=50, cache_creation_input_tokens=0,
        cache_read_input_tokens=0, web_search_requests=0, estimated_cost_usd=0.001,
    )
    storage.record_llm_call(
        agent="analysis", model="claude-sonnet-5", trigger_context="MNQ1!/5m",
        success=False, error_message="timeout", latency_ms=5000.0,
        input_tokens=None, output_tokens=None, cache_creation_input_tokens=None,
        cache_read_input_tokens=None, web_search_requests=None, estimated_cost_usd=None,
    )
    storage.record_llm_call(
        agent="news", model="claude-sonnet-5", trigger_context="MNQ1!",
        success=True, error_message=None, latency_ms=800.0,
        input_tokens=200, output_tokens=100, cache_creation_input_tokens=0,
        cache_read_input_tokens=0, web_search_requests=2, estimated_cost_usd=0.003,
    )

    summary = storage.get_llm_call_summary()
    assert summary["overall"]["total_calls"] == 3
    assert summary["overall"]["successful_calls"] == 2
    assert summary["overall"]["failed_calls"] == 1

    analysis = summary["by_agent"]["analysis"]
    assert analysis["total_calls"] == 2
    assert analysis["successful_calls"] == 1
    assert analysis["failed_calls"] == 1
    assert analysis["total_input_tokens"] == 100

    news = summary["by_agent"]["news"]
    assert news["total_web_search_requests"] == 2


def test_get_llm_call_summary_since_restricts_the_window(fresh_env):
    storage, _ = fresh_env
    storage.record_llm_call(
        agent="analysis", model="claude-sonnet-5", trigger_context=None,
        success=True, error_message=None, latency_ms=100.0,
        input_tokens=10, output_tokens=10, cache_creation_input_tokens=0,
        cache_read_input_tokens=0, web_search_requests=0, estimated_cost_usd=0.0001,
    )
    # A `since` far in the future should exclude everything recorded now.
    summary = storage.get_llm_call_summary(since="2099-01-01T00:00:00Z")
    assert summary["overall"]["total_calls"] == 0


def test_get_recent_llm_calls_filters_by_agent(fresh_env):
    storage, _ = fresh_env
    storage.record_llm_call(
        agent="macro", model="claude-sonnet-5", trigger_context="MNQ1!",
        success=True, error_message=None, latency_ms=500.0,
        input_tokens=10, output_tokens=10, cache_creation_input_tokens=0,
        cache_read_input_tokens=0, web_search_requests=1, estimated_cost_usd=0.001,
    )
    storage.record_llm_call(
        agent="news", model="claude-sonnet-5", trigger_context="MNQ1!",
        success=True, error_message=None, latency_ms=500.0,
        input_tokens=10, output_tokens=10, cache_creation_input_tokens=0,
        cache_read_input_tokens=0, web_search_requests=1, estimated_cost_usd=0.001,
    )

    macro_only = storage.get_recent_llm_calls(limit=10, agent="macro")
    assert len(macro_only) == 1
    assert macro_only[0]["agent"] == "macro"
