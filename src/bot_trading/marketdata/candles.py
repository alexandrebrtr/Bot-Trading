"""Chargement et agrégation de bougies, sans look-ahead.

``resample`` agrège des bougies d'un timeframe fin vers un plus large. La
dernière bougie agrégée n'est émise que si elle est **complète**, c'est-à-dire
si la bougie fine suivante appartient déjà au panier suivant ou si
``include_partial`` est explicitement demandé. Ainsi une bougie H1 n'est jamais
visible avant sa clôture.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot_trading.core.models import Candle, Timeframe


def bucket_start(t: datetime, tf: Timeframe) -> datetime:
    if tf == Timeframe.D1:
        return t.replace(hour=0, minute=0, second=0, microsecond=0)
    epoch = datetime(1970, 1, 1, tzinfo=t.tzinfo)
    minutes = int((t - epoch).total_seconds() // 60)
    start = minutes - minutes % tf.minutes
    return epoch + timedelta(minutes=start)


def resample(candles: Sequence[Candle], target: Timeframe, include_partial: bool = False) -> list[Candle]:
    out: list[Candle] = []
    current: list[Candle] = []
    current_key: datetime | None = None

    def flush() -> None:
        out.append(
            Candle(
                time=current_key,  # type: ignore[arg-type]
                open=current[0].open,
                high=max(c.high for c in current),
                low=min(c.low for c in current),
                close=current[-1].close,
                volume=sum(c.volume for c in current),
            )
        )

    for c in candles:
        key = bucket_start(c.time, target)
        if current_key is not None and key != current_key:
            flush()
            current = []
        current_key = key
        current.append(c)
    if current and include_partial:
        flush()
    return out


def load_csv(path: str | Path) -> list[Candle]:
    """CSV avec colonnes ``time,open,high,low,close[,volume]`` (ISO 8601, UTC)."""
    rows: list[Candle] = []
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            t = datetime.fromisoformat(r["time"].replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            rows.append(
                Candle(t, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), float(r.get("volume") or 0))
            )
    rows.sort(key=lambda c: c.time)
    return rows


def write_csv(path: str | Path, candles: Iterable[Candle]) -> None:
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["time", "open", "high", "low", "close", "volume"])
        for c in candles:
            w.writerow([c.time.isoformat(), c.open, c.high, c.low, c.close, c.volume])
