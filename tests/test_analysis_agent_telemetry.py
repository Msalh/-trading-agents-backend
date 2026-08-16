"""
Integration test for Tier 3.15's telemetry wiring inside
app.analysis_agent.run_analysis — proves the `with track_llm_call(...)
as call: ... call.record(response)` wiring added to the real function
actually logs a row, not just that the telemetry module works in
isolation (see tests/test_llm_telemetry.py for the module's own unit
tests). Only analysis_agent is covered here as a representative case
— execution_agent/news_agent/macro_agent follow the identical pattern.

No real network/LLM calls: anthropic.Anthropic is monkeypatched to a
fake client returning a canned response.

Run with: pytest tests/test_analysis_agent_telemetry.py -v
"""

import importlib
import json
import os
import tempfile

import pytest


@pytest.fixture
def fresh_env(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setenv("DB_PATH", tmp.name)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    import app.storage as storage
    importlib.reload(storage)
    storage.init_db()

    import app.llm_telemetry as llm_telemetry
    importlib.reload(llm_telemetry)

    import app.analysis_agent as analysis_agent
    importlib.reload(analysis_agent)

    yield storage, analysis_agent

    os.unlink(tmp.name)


class _FakeTextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeServerToolUse:
    web_search_requests = 0


class _FakeUsage:
    def __init__(self):
        self.input_tokens = 321
        self.output_tokens = 87
        self.cache_creation_input_tokens = 0
        self.cache_read_input_tokens = 0
        self.server_tool_use = _FakeServerToolUse()


class _FakeResponse:
    def __init__(self, payload: dict):
        self.content = [_FakeTextBlock(json.dumps(payload))]
        self.usage = _FakeUsage()


class _FakeMessages:
    def __init__(self, payload):
        self._payload = payload

    def create(self, **kwargs):
        return _FakeResponse(self._payload)


class _FakeClient:
    def __init__(self, payload):
        self.messages = _FakeMessages(payload)


def test_run_analysis_logs_telemetry_on_success(fresh_env, monkeypatch):
    storage, analysis_agent = fresh_env
    payload = {
        "direction": "bullish", "confidence": 70, "reasoning": "test",
        "key_data": {"key_levels": [], "pattern": None, "trend_alignment": None}, "flags": [],
    }
    monkeypatch.setattr(analysis_agent.anthropic, "Anthropic", lambda api_key: _FakeClient(payload))

    bars = [{"timestamp": "2026-08-16T14:00:00Z", "open": 19990.0, "high": 20010.0, "low": 19980.0, "close": 20000.0}]
    opinion = analysis_agent.run_analysis("MNQ1!", "5m", bars)

    assert opinion.direction == "bullish"

    recent = storage.get_recent_llm_calls(limit=1)
    assert len(recent) == 1
    row = recent[0]
    assert row["agent"] == "analysis"
    assert row["success"] == 1
    assert row["input_tokens"] == 321
    assert row["output_tokens"] == 87
    assert row["trigger_context"] == "MNQ1!/5m"


def test_run_analysis_logs_telemetry_on_malformed_response(fresh_env, monkeypatch):
    storage, analysis_agent = fresh_env

    class _BadMessages:
        def create(self, **kwargs):
            response = _FakeResponse({})
            response.content = [_FakeTextBlock("not valid json")]
            return response

    class _BadClient:
        def __init__(self, api_key):
            self.messages = _BadMessages()

    monkeypatch.setattr(analysis_agent.anthropic, "Anthropic", _BadClient)

    bars = [{"timestamp": "2026-08-16T14:00:00Z", "open": 19990.0, "high": 20010.0, "low": 19980.0, "close": 20000.0}]
    with pytest.raises(analysis_agent.AnalysisAgentError):
        analysis_agent.run_analysis("MNQ1!", "5m", bars)

    # The API call itself succeeded (it's the JSON parsing afterward
    # that failed, outside the `with track_llm_call(...)` block) --
    # so telemetry must show a SUCCESSFUL call, not a failed one; the
    # parse error is a separate, already-existing AnalysisAgentError
    # concern, not something this tier changes.
    recent = storage.get_recent_llm_calls(limit=1)
    assert recent[0]["success"] == 1
