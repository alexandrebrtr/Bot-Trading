"""Format commun des bougies bid/ask stockées, et conversion vers ``Candle``."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from bot_trading.core.models import Candle, Timeframe

CANDLE_KEYS = ("epic", "timeframe", "event_time")


def candles_dataset(source: str, epic: str, tf: Timeframe) -> str:
    return f"candles.{source}.{epic.lower()}.{tf.value.lower()}"


def bar_record(epic: str, tf: Timeframe, bid: Candle, ask: Candle | None, volume: float | None = None) -> dict:
    """Une bougie close. ``available_at`` = clôture de la bougie (jamais avant)."""
    ask = ask or bid
    rec = {
        "epic": epic,
        "timeframe": tf.value,
        "event_time": bid.time,
        "available_at": bid.time + timedelta(minutes=tf.minutes),
        "bid_open": bid.open, "bid_high": bid.high, "bid_low": bid.low, "bid_close": bid.close,
        "ask_open": ask.open, "ask_high": ask.high, "ask_low": ask.low, "ask_close": ask.close,
        "open": (bid.open + ask.open) / 2,
        "high": (bid.high + ask.high) / 2,
        "low": (bid.low + ask.low) / 2,
        "close": (bid.close + ask.close) / 2,
        "volume": float(volume if volume is not None else bid.volume),
    }
    return rec


def closed_only(records: Sequence[dict], now: datetime) -> list[dict]:
    """Retire les bougies encore en cours à ``now`` (anti look-ahead)."""
    return [r for r in records if r["available_at"] <= now]


def to_candles(records: Sequence[dict], side: str = "bid") -> list[Candle]:
    """Bougies pour les moteurs d'analyse. ``side`` : ``bid``, ``ask`` ou ``mid``."""
    prefix = "" if side == "mid" else f"{side}_"
    return [
        Candle(r["event_time"], r[f"{prefix}open"], r[f"{prefix}high"], r[f"{prefix}low"], r[f"{prefix}close"], r.get("volume") or 0.0)
        for r in records
    ]
