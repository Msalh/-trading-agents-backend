"""
LLM call cost/usage telemetry — Tier 3.15.

Three external review cycles (the original architecture review, and
both ChatGPT realignment packages since) have named the same gap:
this project has no visibility into what its own LLM calls actually
cost, and every prior tier deferred building it in favor of something
that looked more urgent at the time. The third review named this
directly as an "operational discipline" problem, not just a nice-to-
have — recommending it be a small, well-scoped task rather than
another large diagnostic tier. This module is that small task.

Wraps exactly one thing: every `client.messages.create()` call site in
app/analysis_agent.py, app/news_agent.py, app/macro_agent.py, and
app/execution_agent.py, via the `track_llm_call` context manager
below. Records ONE row per call, success or failure, to the new
llm_call_log table (see app/storage.py) — agent, model, a short
trigger_context string (symbol/timeframe or similar), latency,
input/output/cache token counts, web_search call count (for News/
Macro, which use Claude's hosted web_search tool), and an estimated
USD cost. A logging failure (e.g. a locked DB) is swallowed, never
propagated on top of whatever the actual agent call did or didn't do
— telemetry must never be able to break a real agent call.

Cost estimate: confirmed against https://platform.claude.com/docs/en/
about-claude/pricing on 2026-08-16 for claude-sonnet-5 — $2/MTok
input, $10/MTok output, cache write 1.25x base input rate, cache read
0.1x base input rate, web_search $10 per 1,000 searches ($0.01/call).
All five figures are env-configurable (TELEMETRY_INPUT_COST_PER_MTOK /
TELEMETRY_OUTPUT_COST_PER_MTOK / TELEMETRY_CACHE_WRITE_MULTIPLIER /
TELEMETRY_CACHE_READ_MULTIPLIER / TELEMETRY_WEB_SEARCH_COST_PER_SEARCH)
since pricing changes over time and this project has no billing-
account access to verify it automatically — `estimated_cost_usd`
throughout this project is exactly that, an estimate for relative
comparison and trend-watching, not an authoritative billing figure.
"""

import os
import time
from contextlib import contextmanager

from app import storage

INPUT_COST_PER_MTOK = float(os.environ.get("TELEMETRY_INPUT_COST_PER_MTOK", "2.0"))
OUTPUT_COST_PER_MTOK = float(os.environ.get("TELEMETRY_OUTPUT_COST_PER_MTOK", "10.0"))
CACHE_WRITE_MULTIPLIER = float(os.environ.get("TELEMETRY_CACHE_WRITE_MULTIPLIER", "1.25"))
CACHE_READ_MULTIPLIER = float(os.environ.get("TELEMETRY_CACHE_READ_MULTIPLIER", "0.1"))
WEB_SEARCH_COST_PER_SEARCH = float(os.environ.get("TELEMETRY_WEB_SEARCH_COST_PER_SEARCH", "0.01"))


def estimate_cost_usd(
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    web_search_requests: int = 0,
) -> float:
    cost = (input_tokens / 1_000_000) * INPUT_COST_PER_MTOK
    cost += (output_tokens / 1_000_000) * OUTPUT_COST_PER_MTOK
    cost += (cache_creation_input_tokens / 1_000_000) * INPUT_COST_PER_MTOK * CACHE_WRITE_MULTIPLIER
    cost += (cache_read_input_tokens / 1_000_000) * INPUT_COST_PER_MTOK * CACHE_READ_MULTIPLIER
    cost += web_search_requests * WEB_SEARCH_COST_PER_SEARCH
    return round(cost, 6)


def _extract_usage(response) -> dict:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    server_tool_use = getattr(usage, "server_tool_use", None)
    web_search_requests = getattr(server_tool_use, "web_search_requests", 0) if server_tool_use else 0
    return {
        "input_tokens": getattr(usage, "input_tokens", None) or 0,
        "output_tokens": getattr(usage, "output_tokens", None) or 0,
        "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", None) or 0,
        "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", None) or 0,
        "web_search_requests": web_search_requests or 0,
    }


class _CallHandle:
    """Passed into the `with` block so the caller can attach the
    response object once the API call actually returns — the context
    manager itself can't see it, since it only wraps the call, it
    doesn't make it."""

    def __init__(self):
        self.response = None

    def record(self, response) -> None:
        self.response = response


@contextmanager
def track_llm_call(agent: str, model: str, trigger_context: str | None = None):
    """Wraps one client.messages.create() call site:

        with track_llm_call("analysis", MODEL, trigger_context=f"{symbol}/{timeframe}") as call:
            response = client.messages.create(...)
            call.record(response)

    Records exactly one row to llm_call_log on exit, whether the call
    inside the `with` block succeeded or raised. An exception is
    logged as a failed call (its message truncated to 500 chars) and
    then re-raised completely unchanged — this wrapper never changes
    what an agent module does on success or failure, it only observes
    it. If `call.record()` is never reached (the API call itself
    raised before returning), token/cost fields are logged as null/0
    rather than guessed."""
    start = time.monotonic()
    handle = _CallHandle()
    success = True
    error_message = None
    try:
        yield handle
    except Exception as e:
        success = False
        error_message = str(e)[:500]
        raise
    finally:
        latency_ms = round((time.monotonic() - start) * 1000, 1)
        usage = _extract_usage(handle.response) if handle.response is not None else {}
        cost = (
            estimate_cost_usd(
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                cache_creation_input_tokens=usage.get("cache_creation_input_tokens", 0),
                cache_read_input_tokens=usage.get("cache_read_input_tokens", 0),
                web_search_requests=usage.get("web_search_requests", 0),
            )
            if usage
            else None
        )
        try:
            storage.record_llm_call(
                agent=agent,
                model=model,
                trigger_context=trigger_context,
                success=success,
                error_message=error_message,
                latency_ms=latency_ms,
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
                cache_creation_input_tokens=usage.get("cache_creation_input_tokens"),
                cache_read_input_tokens=usage.get("cache_read_input_tokens"),
                web_search_requests=usage.get("web_search_requests"),
                estimated_cost_usd=cost,
            )
        except Exception:
            # Telemetry must never be able to break a real agent call
            # or mask its actual error — swallow a logging failure
            # rather than raising on top of (or instead of) whatever
            # the `with` block itself raised.
            pass
