"""
Pydantic models matching the exact wire contract broadcast by the
MNQU6 Market State Broadcaster v1 Pine Script (TradingView webhook).

Field names and types mirror the JSON built in that script's
f_buildPayload() function one-to-one. If the script's payload ever
changes, this file needs to change with it.
"""

from typing import Literal, Optional

from pydantic import BaseModel, Field


class MarketStatePayload(BaseModel):
    schema_version: str
    event_id: str
    symbol: str
    source: str
    timeframe: Literal["1m", "5m", "15m", "1h"]
    timestamp: str  # ISO-8601 UTC, e.g. "2026-08-08T14:35:00Z"
    bar_status: Literal["closed"]
    event_type: Literal["bar_closed"]
    secret: str

    open: float
    high: float
    low: float
    close: float
    volume: Optional[float] = None

    session_name: Literal["RTH", "OVERNIGHT"]
    is_rth: bool
    trading_date: str  # "YYYY-MM-DD"

    rth_open: Optional[float] = None
    previous_day_high: Optional[float] = None
    previous_day_low: Optional[float] = None
    overnight_high: Optional[float] = None
    overnight_low: Optional[float] = None

    vwap: Optional[float] = None
    distance_from_vwap_points: Optional[float] = None
    atr: Optional[float] = None
    volume_ratio: Optional[float] = None

    nearest_liquidity_level: Optional[float] = None
    nearest_liquidity_type: Optional[str] = None
    distance_to_liquidity_ticks: Optional[float] = None

    overnight_high_status: Optional[str] = None
    overnight_low_status: Optional[str] = None
    previous_day_high_status: Optional[str] = None
    previous_day_low_status: Optional[str] = None

    trend_1m: Optional[str] = None
    trend_5m: Optional[str] = None
    trend_15m: Optional[str] = None
    trend_1h: Optional[str] = None

    liquidity_sweep: bool = False
    reclaim: bool = False
    rejection: bool = False
    displacement: bool = False
    volume_spike: bool = False


class MarketStateOut(BaseModel):
    """What we return to callers (Analysis Agent, dashboard, etc). Same
    shape as the input payload, minus the secret — it should never be
    echoed back out."""

    model_config = {"extra": "ignore"}

    event_id: str
    symbol: str
    source: str
    timeframe: str
    timestamp: str
    bar_status: str
    event_type: str

    open: float
    high: float
    low: float
    close: float
    volume: Optional[float] = None

    session_name: str
    is_rth: bool
    trading_date: str

    rth_open: Optional[float] = None
    previous_day_high: Optional[float] = None
    previous_day_low: Optional[float] = None
    overnight_high: Optional[float] = None
    overnight_low: Optional[float] = None

    vwap: Optional[float] = None
    distance_from_vwap_points: Optional[float] = None
    atr: Optional[float] = None
    volume_ratio: Optional[float] = None

    nearest_liquidity_level: Optional[float] = None
    nearest_liquidity_type: Optional[str] = None
    distance_to_liquidity_ticks: Optional[float] = None

    trend_1m: Optional[str] = None
    trend_5m: Optional[str] = None
    trend_15m: Optional[str] = None
    trend_1h: Optional[str] = None

    liquidity_sweep: bool = False
    reclaim: bool = False
    rejection: bool = False
    displacement: bool = False
    volume_spike: bool = False


class WebhookAck(BaseModel):
    status: Literal["stored", "duplicate"]
    event_id: str
