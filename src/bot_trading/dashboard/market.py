"""Lecture des bougies et séries intermarchés depuis le store Parquet (lecture seule).

Aucun appel réseau : le dashboard n'affiche que ce que le Data Engine a déjà
collecté (``python -m bot_trading.marketdata refresh …``). Les lectures
utilisent ``as_of`` : une bougie encore en cours n'est jamais affichée.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from bot_trading.core.models import Candle, Timeframe
from bot_trading.marketdata.bars import candles_dataset, to_candles
from bot_trading.marketdata.candles import resample

PRICE_SOURCES = ("capital", "dukascopy")
DEFAULT_STORE = Path("data/store")
# Profondeur lue par timeframe (préchauffage ATR et structure compris).
LOOKBACK = {
    Timeframe.M5: timedelta(days=5),
    Timeframe.M15: timedelta(days=12),
    Timeframe.H1: timedelta(days=45),
    Timeframe.H4: timedelta(days=180),
    Timeframe.D1: timedelta(days=900),
}


@dataclass(frozen=True)
class LoadedCandles:
    candles: list[Candle]
    source: str  # ex. "capital (H1)" ou "dukascopy (M5 agrégé)"


def open_store(root: str | Path = DEFAULT_STORE):
    """``ParquetStore`` si le dossier existe, sinon ``None`` (pas de création)."""
    root = Path(root)
    if not root.is_dir():
        return None
    from bot_trading.marketdata.storage import ParquetStore

    return ParquetStore(root)


def load_candles(store, epic: str, tf: Timeframe, now: datetime, side: str = "bid", sources: tuple[str, ...] = PRICE_SOURCES) -> LoadedCandles:
    """Bougies closes à ``now`` : dataset natif d'abord, sinon agrégation d'un timeframe plus fin."""
    if store is None:
        return LoadedCandles([], "")
    start = now - LOOKBACK[tf]
    available = set(store.datasets())
    for source in sources:
        ds = candles_dataset(source, epic, tf)
        if ds in available:
            rows = store.read(ds, start=start, as_of=now)
            if rows:
                return LoadedCandles(to_candles(rows, side), f"{source} ({tf.value})")
    for source in sources:
        for finer in (Timeframe.M5, Timeframe.M15, Timeframe.H1, Timeframe.H4):
            if finer.minutes >= tf.minutes:
                break
            ds = candles_dataset(source, epic, finer)
            if ds not in available:
                continue
            rows = store.read(ds, start=start, as_of=now)
            if rows:
                # Dernier seau émis seulement s'il est clos à ``now`` (jamais de bougie en cours).
                agg = [c for c in resample(to_candles(rows, side), tf, include_partial=True) if c.time + timedelta(minutes=tf.minutes) <= now]
                return LoadedCandles(agg, f"{source} ({finer.value} agrégé)")
    return LoadedCandles([], "")


def latest_series(store, dataset: str, column: str, now: datetime, lookback: timedelta = timedelta(days=400)) -> list[tuple[datetime, float]]:
    """Série ``(event_time, valeur)`` connue à ``now``. Vide si absente."""
    if store is None or dataset not in set(store.datasets()):
        return []
    rows = store.read(dataset, start=now - lookback, as_of=now)
    return [(r["event_time"], float(r[column])) for r in rows if r.get(column) is not None]


INTERMARKET = (
    ("VIX", "market.cboe.vix", "close"),
    ("Taux 2 ans (%)", "rates.treasury.par_curve", "y_2y"),
    ("Taux 10 ans (%)", "rates.treasury.par_curve", "y_10y"),
    ("Pente 10 ans − 2 ans", "rates.treasury.par_curve", "spread_10y_2y"),
)
