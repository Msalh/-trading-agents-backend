"""
Tier 3.44 (sixteenth external review item #5) — app.macro_agent_v2, the
exploratory four-axis Macro shadow schema. Same no-real-network mocking
pattern as tests/test_analysis_agent_telemetry.py: anthropic.Anthropic
is monkeypatched to a fake client returning a canned response.

Run with: pytest tests/test_macro_agent_v2.py -v
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

    import app.macro_agent as macro_agent
    importlib.reload(macro_agent)

    import app.macro_agent_v2 as macro_agent_v2
    importlib.reload(macro_agent_v2)

    yield storage, macro_agent, macro_agent_v2

    os.unlink(tmp.name)


class _FakeTextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeServerToolUse:
    web_search_requests = 0


class _FakeUsage:
    def __init__(self):
        self.input_tokens = 200
        self.output_tokens = 60
        self.cache_creation_input_tokens = 0
        self.cache_read_input_tokens = 0
        self.server_tool_use = _FakeServerToolUse()


class _FakeResponse:
    def __init__(self, payload_or_text):
        if isinstance(payload_or_text, str):
            self.content = [_FakeTextBlock(payload_or_text)]
        else:
            self.content = [_FakeTextBlock(json.dumps(payload_or_text))]
        self.usage = _FakeUsage()


class _FakeMessages:
    def __init__(self, payload_or_text):
        self._payload_or_text = payload_or_text

    def create(self, **kwargs):
        return _FakeResponse(self._payload_or_text)


class _FakeClient:
    def __init__(self, payload_or_text):
        self.messages = _FakeMessages(payload_or_text)


def _valid_payload(**overrides):
    payload = {
        "directional_bias": "bullish",
        "tradeability": "favorable",
        "risk_cause": "none",
        "risk_cause_detail": None,
        "data_quality": "fresh",
        "confidence": 65,
        "reasoning": "DXY weak, yields flat, SPX/NDX in sync.",
        "key_data": {
            "dxy_read": "weak, drifting lower",
            "yields_read": "flat around 4.1%",
            "spx_ndx_correlation": "in_sync",
            "notes": None,
        },
    }
    payload.update(overrides)
    return payload


def test_run_macro_shadow_v2_happy_path(fresh_env, monkeypatch):
    storage, macro_agent, macro_agent_v2 = fresh_env
    monkeypatch.setattr(macro_agent_v2.anthropic, "Anthropic", lambda api_key: _FakeClient(_valid_payload()))

    opinion = macro_agent_v2.run_macro_shadow_v2("MNQ1!", candidate_id="cand-1")

    assert opinion.schema_version == macro_agent_v2.MACRO_V2_SCHEMA_VERSION == "2"
    assert opinion.model == macro_agent_v2.MODEL
    assert opinion.symbol == "MNQ1!"
    assert opinion.candidate_id == "cand-1"
    assert opinion.directional_bias == "bullish"
    assert opinion.tradeability == "favorable"
    assert opinion.risk_cause == "none"
    assert opinion.data_quality == "fresh"
    assert opinion.confidence == 65


def test_run_macro_shadow_v2_logs_telemetry_under_its_own_agent_name(fresh_env, monkeypatch):
    """Separate agent name in llm_call_log ("macro_shadow_v2", not
    "macro") so shadow-schema cost/telemetry is trivially separable
    from the live Macro agent's own cost tracking."""
    storage, macro_agent, macro_agent_v2 = fresh_env
    monkeypatch.setattr(macro_agent_v2.anthropic, "Anthropic", lambda api_key: _FakeClient(_valid_payload()))

    macro_agent_v2.run_macro_shadow_v2("MNQ1!")

    recent = storage.get_recent_llm_calls(limit=1)
    assert len(recent) == 1
    assert recent[0]["agent"] == "macro_shadow_v2"
    assert recent[0]["success"] == 1


def test_run_macro_shadow_v2_candidate_id_is_optional(fresh_env, monkeypatch):
    storage, macro_agent, macro_agent_v2 = fresh_env
    monkeypatch.setattr(macro_agent_v2.anthropic, "Anthropic", lambda api_key: _FakeClient(_valid_payload()))

    opinion = macro_agent_v2.run_macro_shadow_v2("MNQ1!")
    assert opinion.candidate_id is None


@pytest.mark.parametrize("field", ["directional_bias", "tradeability", "risk_cause", "data_quality"])
def test_run_macro_shadow_v2_rejects_invalid_enum_value(fresh_env, monkeypatch, field):
    storage, macro_agent, macro_agent_v2 = fresh_env
    bad_payload = _valid_payload(**{field: "not-a-real-value"})
    monkeypatch.setattr(macro_agent_v2.anthropic, "Anthropic", lambda api_key: _FakeClient(bad_payload))

    with pytest.raises(macro_agent_v2.MacroAgentV2Error, match=field):
        macro_agent_v2.run_macro_shadow_v2("MNQ1!")


def test_run_macro_shadow_v2_rejects_missing_field(fresh_env, monkeypatch):
    storage, macro_agent, macro_agent_v2 = fresh_env
    payload = _valid_payload()
    del payload["risk_cause_detail"]
    monkeypatch.setattr(macro_agent_v2.anthropic, "Anthropic", lambda api_key: _FakeClient(payload))

    with pytest.raises(macro_agent_v2.MacroAgentV2Error, match="missing required fields"):
        macro_agent_v2.run_macro_shadow_v2("MNQ1!")


def test_run_macro_shadow_v2_rejects_malformed_json(fresh_env, monkeypatch):
    storage, macro_agent, macro_agent_v2 = fresh_env
    monkeypatch.setattr(macro_agent_v2.anthropic, "Anthropic", lambda api_key: _FakeClient("not valid json"))

    with pytest.raises(macro_agent_v2.MacroAgentV2Error, match="did not return valid JSON"):
        macro_agent_v2.run_macro_shadow_v2("MNQ1!")


def test_run_macro_shadow_v2_requires_api_key(fresh_env, monkeypatch):
    storage, macro_agent, macro_agent_v2 = fresh_env
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(macro_agent_v2.MacroAgentV2Error, match="ANTHROPIC_API_KEY"):
        macro_agent_v2.run_macro_shadow_v2("MNQ1!")


def test_macro_agent_v1_module_untouched_by_v2_import(fresh_env):
    """Sanity check that importing/using macro_agent_v2 never mutates
    the live v1 module's constants — belt-and-suspenders alongside the
    git diff --stat check that app/macro_agent.py has zero changes."""
    storage, macro_agent, macro_agent_v2 = fresh_env
    assert macro_agent.PROMPT_VERSION == "1"
    assert macro_agent_v2.MACRO_V2_SCHEMA_VERSION == "2"
    assert macro_agent.SYSTEM_PROMPT != macro_agent_v2.SYSTEM_PROMPT_V2
