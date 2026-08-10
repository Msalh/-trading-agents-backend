"""
Independent scheduler — Sprint 4/5.

The roadmap draws a hard line between two trigger types:
  - Analysis Agent: event-driven, fires on the TradingView webhook
  - News / Macro Agents: time-based, fire on their own clock

This module is the time-based half. It runs in-process using
APScheduler rather than a separate worker/cron service — the
simplest thing that could work for now. If this backend ever needs
to scale beyond one instance, this in-process scheduler would need
to move to a dedicated worker (otherwise every instance would fire
its own copy of the job).

Disabled by default (ENABLE_SCHEDULER unset or false) so running the
backend locally for testing never triggers paid API calls on a timer
without asking for it explicitly.

Cost control: both jobs are session-gated using the same Timing
logic Analysis uses. Real usage showed News/Macro firing every 20
minutes around the clock, including outside London/NY hours, driving
up API spend for no benefit (no one's trading then anyway). Now each
job checks the current session before doing anything and simply
skips — no LLM call, no cost — when outside both sessions.
"""

import logging
import os
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from app.macro_agent import MacroAgentError, run_macro
from app.news_agent import NewsAgentError, run_news
from app.storage import save_opinion
from app.timing_agent import evaluate_timing, should_run_analysis

logger = logging.getLogger("scheduler")

NEWS_SYMBOL = os.environ.get("NEWS_SYMBOL", "MNQ1!")
NEWS_INTERVAL_MINUTES = int(os.environ.get("NEWS_INTERVAL_MINUTES", "20"))
NEWS_TIMEFRAME = "global"  # News/Macro aren't tied to any chart timeframe

MACRO_SYMBOL = os.environ.get("MACRO_SYMBOL", NEWS_SYMBOL)
MACRO_INTERVAL_MINUTES = int(os.environ.get("MACRO_INTERVAL_MINUTES", "20"))
MACRO_TIMEFRAME = "global"

_scheduler: BackgroundScheduler | None = None


def _in_active_session() -> bool:
    """Same London/NY session gate Analysis uses, evaluated against
    the current server time rather than a bar's timestamp."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    timing = evaluate_timing(now_iso)
    return should_run_analysis(timing)


def _news_job() -> None:
    if not _in_active_session():
        logger.info("news job skipped — outside London/NY session")
        return
    try:
        opinion = run_news(symbol=NEWS_SYMBOL)
        save_opinion(
            agent="news",
            symbol=NEWS_SYMBOL,
            timeframe=NEWS_TIMEFRAME,
            timestamp=opinion.timestamp,
            opinion=opinion.to_dict(),
        )
        logger.info(
            "news agent ran: direction=%s confidence=%s flags=%s",
            opinion.direction,
            opinion.confidence,
            opinion.flags,
        )
    except NewsAgentError as e:
        logger.error("news agent failed: %s", e)


def _macro_job() -> None:
    if not _in_active_session():
        logger.info("macro job skipped — outside London/NY session")
        return
    try:
        opinion = run_macro(symbol=MACRO_SYMBOL)
        save_opinion(
            agent="macro",
            symbol=MACRO_SYMBOL,
            timeframe=MACRO_TIMEFRAME,
            timestamp=opinion.timestamp,
            opinion=opinion.to_dict(),
        )
        logger.info(
            "macro agent ran: direction=%s confidence=%s flags=%s",
            opinion.direction,
            opinion.confidence,
            opinion.flags,
        )
    except MacroAgentError as e:
        logger.error("macro agent failed: %s", e)


def start_scheduler() -> BackgroundScheduler | None:
    global _scheduler
    if os.environ.get("ENABLE_SCHEDULER", "false").lower() != "true":
        logger.info("ENABLE_SCHEDULER is not 'true' — background scheduler not started")
        return None

    if _scheduler is not None:
        return _scheduler

    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(
        _news_job,
        "interval",
        minutes=NEWS_INTERVAL_MINUTES,
        id="news_agent_job",
        # No next_run_time override here — APScheduler computes the
        # first fire time itself (now + interval). An earlier version
        # of this code explicitly passed next_run_time=None intending
        # "first run waits one interval", but that value actually
        # means "pause this job" in APScheduler — the job was added
        # but would NEVER fire on its own. Confirmed and fixed.
    )
    _scheduler.add_job(
        _macro_job,
        "interval",
        minutes=MACRO_INTERVAL_MINUTES,
        id="macro_agent_job",
    )
    _scheduler.start()
    logger.info(
        "background scheduler started: news every %s min, macro every %s min",
        NEWS_INTERVAL_MINUTES,
        MACRO_INTERVAL_MINUTES,
    )
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None

