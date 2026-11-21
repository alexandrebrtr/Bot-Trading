"""Sources de bougies du paper trading.

``StoreBarFeed`` lit les bougies **closes** (``available_at <= now``) du store
Parquet alimenté par le Data Engine. Le spread de chaque bougie est celui de la
clôture (``ask_close - bid_close``), comme dans le backtest.

``CapitalRefresher`` rafraîchit ce store depuis Capital.com avant chaque cycle,
par le client en lecture seule (``GET /api/v1/prices`` uniquement). Il est
facultatif : sans identifiants, la boucle consomme ce que
``python -m bot_trading.marketdata refresh capital`` a déjà collecté.
"""

from __future__ import annotations

import bisect
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Protocol

from bot_trading.core.models import Candle, Timeframe
from bot_trading.marketdata.bars import candles_dataset
from bot_trading.marketdata.source import Bar


class BarFeed(Protocol):
    def bars(self, start: datetime | None, now: datetime) -> list[Bar]:
        """Bougies closes à ``now`` dont l'ouverture est ≥ ``start``, triées."""
        ...


def _spread(row: dict) -> float | None:
    ask, bid = row.get("ask_close"), row.get("bid_close")
    if ask is None or bid is None:
        return None
    s = float(ask) - float(bid)
    return s if s >= 0 else None


class StoreBarFeed:
    def __init__(self, store, epic: str = "US500", timeframe: Timeframe = Timeframe.H1, sources: Sequence[str] = ("capital",)) -> None:
        self.store = store
        self.epic = epic
        self.timeframe = timeframe
        self.sources = tuple(sources)
        self.last_source = ""

    def bars(self, start: datetime | None, now: datetime) -> list[Bar]:
        if self.store is None:
            return []
        available = set(self.store.datasets())
        for source in self.sources:
            ds = candles_dataset(source, self.epic, self.timeframe)
            if ds not in available:
                continue
            rows = self.store.read(ds, start=start, as_of=now)
            out: list[Bar] = []
            seen: set[datetime] = set()
            for r in rows:
                t = r["event_time"]
                if t in seen:  # dernière révision gagne (lecture triée par available_at)
                    out = [b for b in out if b.time != t]
                seen.add(t)
                out.append(Bar(Candle(t, r["bid_open"], r["bid_high"], r["bid_low"], r["bid_close"], r.get("volume") or 0.0), self.timeframe, _spread(r)))
            if out:
                self.last_source = f"{source} ({self.timeframe.value})"
                return out
        return []


class InMemoryBarFeed:
    """Flux en mémoire (tests, rejeu) : ne renvoie que les bougies closes à ``now``."""

    def __init__(self, bars: Sequence[Bar]) -> None:
        self._bars = sorted(bars, key=lambda b: b.time)
        self._closes = [b.time + timedelta(minutes=b.timeframe.minutes) for b in self._bars]
        self._opens = [b.time for b in self._bars]

    def bars(self, start: datetime | None, now: datetime) -> list[Bar]:
        lo = 0 if start is None else bisect.bisect_left(self._opens, start)
        hi = bisect.bisect_right(self._closes, now)
        return self._bars[lo:hi]


class CapitalRefresher:
    """Collecte en lecture seule les dernières bougies Capital.com dans le store."""

    def __init__(self, engine, epic: str = "US500", timeframes: Sequence[Timeframe] = (Timeframe.H1,), lookback: timedelta = timedelta(days=3)) -> None:
        from bot_trading.broker.capital_readonly import CapitalCredentials, CapitalReadOnlyClient
        from bot_trading.marketdata.sources.capital import CapitalCandleSource

        self.engine = engine
        self.timeframes = tuple(timeframes)
        self.lookback = lookback
        self.client = CapitalReadOnlyClient(CapitalCredentials.from_env())
        self.source = CapitalCandleSource(self.client, epic)

    def refresh(self, now: datetime) -> int:
        new = 0
        for tf in self.timeframes:
            rows, prov = self.source.fetch(tf, now - self.lookback, now)
            new += self.engine.ingest_candles(rows, prov, tf).rows_new
        return new

    def close(self) -> None:
        try:
            self.client.logout()
        except Exception:  # noqa: BLE001 — la déconnexion ne doit jamais faire tomber la boucle
            pass
