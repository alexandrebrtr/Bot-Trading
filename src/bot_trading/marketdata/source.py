"""Interfaces de sources de données, communes au backtest, au paper trading et aux connecteurs.

Deux familles :

``PriceSource``
    Bougies d'un instrument sur un timeframe, en prix **bid**, avec le spread
    connu à la clôture de chaque bougie (``Bar``). Un connecteur (Capital.com,
    Dukascopy, CSV…) n'a qu'à implémenter ``get_bars``.

``PointInTimeSource``
    Séries datées par leur **heure de disponibilité** (``available_at``), et non
    par la période qu'elles décrivent. ``as_of(series, t)`` ne renvoie jamais
    une valeur publiée ou révisée après ``t`` : c'est la protection contre le
    biais de révision (ALFRED) et le data leakage. Les agents macro publient
    leur biais par la même interface (``series = "bias/swing"`` par exemple).

Aucune implémentation ici n'accède au réseau.
"""

from __future__ import annotations

import bisect
import csv
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from bot_trading.core.models import Candle, Timeframe


@dataclass(frozen=True, slots=True)
class Bar:
    """Bougie bid clôturée et spread (points) observé sur cette bougie.

    ``spread`` à ``None`` : le consommateur applique le spread par défaut de
    ``BrokerCosts``. ``tradable`` à ``False`` signale une bougie pendant
    laquelle le marché était fermé ou suspendu (aucun ordre ne peut y être exécuté).
    """

    bid: Candle
    timeframe: Timeframe
    spread: float | None = None
    tradable: bool = True

    @property
    def time(self) -> datetime:
        return self.bid.time


@runtime_checkable
class PriceSource(Protocol):
    def get_bars(self, epic: str, timeframe: Timeframe, start: datetime | None = None, end: datetime | None = None) -> list[Bar]:
        """Bougies d'ouverture dans [start, end), triées et sans doublon."""
        ...


@dataclass(frozen=True, slots=True)
class Observation:
    """Valeur d'une série telle qu'elle était connue à ``available_at``.

    ``period`` : date de référence de la donnée (ex. mois du CPI).
    ``available_at`` : premier instant où la valeur pouvait être lue (publication,
    révision, sortie d'un agent). C'est la seule date utilisée pour filtrer.
    """

    series: str
    period: datetime
    value: Any
    available_at: datetime
    source: str = ""
    meta: dict = field(default_factory=dict, compare=False, hash=False)


@runtime_checkable
class PointInTimeSource(Protocol):
    def as_of(self, series: str, t: datetime) -> Observation | None:
        """Dernière observation disponible à ``t`` (la plus récente ``available_at`` ≤ t)."""
        ...


# ---------------------------------------------------------------- implémentations


def _check_bars(bars: Sequence[Bar]) -> None:
    for a, b in zip(bars, bars[1:]):
        if b.time <= a.time:
            raise ValueError(f"Bougies non chronologiques ou en double : {a.time} puis {b.time}")


class InMemoryPriceSource:
    """Source en mémoire (fixtures de test, données déjà chargées)."""

    def __init__(self, bars: Iterable[Bar], epic: str = "US500") -> None:
        self.epic = epic
        self._by_tf: dict[Timeframe, list[Bar]] = {}
        for b in bars:
            self._by_tf.setdefault(b.timeframe, []).append(b)
        for tf, lst in self._by_tf.items():
            lst.sort(key=lambda b: b.time)
            _check_bars(lst)

    @classmethod
    def from_candles(cls, candles: Iterable[Candle], timeframe: Timeframe, spread: float | None = None, epic: str = "US500") -> "InMemoryPriceSource":
        return cls((Bar(c, timeframe, spread) for c in candles), epic)

    def get_bars(self, epic: str, timeframe: Timeframe, start: datetime | None = None, end: datetime | None = None) -> list[Bar]:
        if epic != self.epic:
            raise KeyError(f"Epic inconnu : {epic}")
        bars = self._by_tf.get(timeframe, [])
        return [b for b in bars if (start is None or b.time >= start) and (end is None or b.time < end)]


class CsvPriceSource(InMemoryPriceSource):
    """CSV ``time,open,high,low,close[,volume][,spread]`` en prix bid (UTC)."""

    def __init__(self, path: str | Path, timeframe: Timeframe, epic: str = "US500") -> None:
        bars: list[Bar] = []
        with open(path, newline="") as fh:
            for r in csv.DictReader(fh):
                t = datetime.fromisoformat(r["time"].replace("Z", "+00:00"))
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                c = Candle(t, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), float(r.get("volume") or 0))
                sp = r.get("spread")
                bars.append(Bar(c, timeframe, float(sp) if sp not in (None, "") else None))
        super().__init__(bars, epic)


class InMemoryPointInTimeSource:
    """Séries point-in-time en mémoire. Refuse toute observation sans ``available_at``."""

    def __init__(self, observations: Iterable[Observation] = ()) -> None:
        self._data: dict[str, list[Observation]] = {}
        self._keys: dict[str, list[datetime]] = {}
        for o in observations:
            self.add(o)

    def add(self, obs: Observation) -> None:
        if obs.available_at is None or obs.available_at.tzinfo is None:
            raise ValueError("available_at obligatoire et en UTC (aware) : sinon look-ahead impossible à contrôler")
        if obs.available_at < obs.period and obs.meta.get("forecast") is not True:
            # Une donnée ne peut pas être publiée avant la période qu'elle décrit,
            # sauf prévision explicite (nowcast, biais d'agent).
            raise ValueError(f"{obs.series}: available_at {obs.available_at} antérieur à la période {obs.period}")
        lst = self._data.setdefault(obs.series, [])
        keys = self._keys.setdefault(obs.series, [])
        i = bisect.bisect_right(keys, obs.available_at)
        lst.insert(i, obs)
        keys.insert(i, obs.available_at)

    def as_of(self, series: str, t: datetime) -> Observation | None:
        keys = self._keys.get(series)
        if not keys:
            return None
        i = bisect.bisect_right(keys, t)
        return self._data[series][i - 1] if i else None

    def series(self) -> list[str]:
        return sorted(self._data)
