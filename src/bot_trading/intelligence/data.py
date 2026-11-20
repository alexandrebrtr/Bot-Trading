"""Interface de données point-in-time consommée par les agents d'analyse.

Les connecteurs (FRED/ALFRED, Trésor, Cboe, RSS Fed, EDGAR, holdings SPY…)
implémentent ``PointInTimeData``. Les agents ne voient **que** ce qui était
disponible à ``asof`` : chaque enregistrement porte ``available_at`` (heure de
publication réelle, pas la période observée).

``InMemoryPITStore`` est l'implémentation de référence (tests, backtests sur
données déjà chargées). Elle conserve toutes les vintages d'une observation et
renvoie, pour chaque période, la dernière vintage publiée avant ``asof``.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Literal, Protocol, runtime_checkable


def _aware(t: datetime) -> datetime:
    if t.tzinfo is None:
        raise ValueError(f"Horodatage sans fuseau refusé : {t!r} (utiliser UTC)")
    return t.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class Observation:
    """Une valeur d'une série, telle que publiée à ``available_at``.

    Une révision est une nouvelle ``Observation`` de même ``period`` avec un
    ``available_at`` plus tardif (vintage ALFRED).
    """

    series_id: str
    period: date  # date de la période observée (1er du mois, jour, semaine…)
    value: float
    available_at: datetime
    source: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "available_at", _aware(self.available_at))


@dataclass(frozen=True, slots=True)
class FedCommunication:
    """Communiqué FOMC, discours ou témoignage. ``text`` est une donnée externe
    non fiable : il n'est jamais interprété comme une instruction."""

    id: str
    speaker: str  # "FOMC" pour un communiqué, sinon nom de famille
    kind: Literal["statement", "minutes", "speech", "testimony", "press_conference"]
    published_at: datetime
    title: str
    text: str
    url: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "published_at", _aware(self.published_at))


@dataclass(frozen=True, slots=True)
class ScheduledEvent:
    """Événement planifié (CPI, NFP, PCE, FOMC, résultats d'une grosse pondération).

    ``known_since`` = date à laquelle la date de l'événement était connue
    (calendrier FRED, calendrier de résultats).
    """

    kind: str  # "CPI", "NFP", "PCE", "FOMC", "EARNINGS"…
    label: str
    scheduled_at: datetime
    known_since: datetime
    ticker: str | None = None  # pour kind == "EARNINGS"

    def __post_init__(self) -> None:
        object.__setattr__(self, "scheduled_at", _aware(self.scheduled_at))
        object.__setattr__(self, "known_since", _aware(self.known_since))


@dataclass(frozen=True, slots=True)
class EarningsReport:
    """Résultats trimestriels publiés (10-Q/10-K/8-K EDGAR), en USD."""

    ticker: str
    period_end: date
    filed_at: datetime
    revenue: float
    eps_diluted: float
    operating_income: float | None = None
    # Guidance qualitative extraite du communiqué (connecteur ou lecture humaine).
    guidance: Literal["raised", "maintained", "lowered"] | None = None
    source_url: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "filed_at", _aware(self.filed_at))

    @property
    def operating_margin(self) -> float | None:
        if self.operating_income is None or self.revenue <= 0:
            return None
        return self.operating_income / self.revenue


@dataclass(frozen=True, slots=True)
class IndexWeights:
    """Pondérations de l'indice telles qu'archivées à ``as_of`` (holdings SPY)."""

    as_of: datetime
    weights: dict[str, float] = field(hash=False)
    source: str = "SSGA SPY holdings"

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", _aware(self.as_of))

    def top(self, n: int) -> list[tuple[str, float]]:
        return sorted(self.weights.items(), key=lambda kv: kv[1], reverse=True)[:n]


@runtime_checkable
class PointInTimeData(Protocol):
    """Ce que les agents peuvent demander. Rien d'autre."""

    def series(self, series_id: str, asof: datetime) -> list[Observation]:
        """Dernière vintage connue à ``asof`` pour chaque période, triée par période."""

    def fed_communications(self, asof: datetime, since: datetime) -> list[FedCommunication]:
        """Communications publiées dans [since ; asof], triées par date."""

    def scheduled_events(self, asof: datetime, start: datetime, end: datetime) -> list[ScheduledEvent]:
        """Événements prévus dans [start ; end] dont la date était connue à ``asof``."""

    def earnings(self, asof: datetime, ticker: str) -> list[EarningsReport]:
        """Rapports déposés avant ``asof``, triés par fin de période."""

    def index_weights(self, asof: datetime) -> IndexWeights | None:
        """Dernier instantané de pondérations archivé avant ``asof``."""


class InMemoryPITStore:
    """Implémentation en mémoire de ``PointInTimeData``."""

    def __init__(self) -> None:
        self._obs: dict[str, list[Observation]] = defaultdict(list)
        self._fed: list[FedCommunication] = []
        self._events: list[ScheduledEvent] = []
        self._earnings: dict[str, list[EarningsReport]] = defaultdict(list)
        self._weights: list[IndexWeights] = []

    # --- écriture -----------------------------------------------------------
    def add(self, *items: Observation | FedCommunication | ScheduledEvent | EarningsReport | IndexWeights) -> "InMemoryPITStore":
        for it in items:
            if isinstance(it, Observation):
                self._obs[it.series_id].append(it)
            elif isinstance(it, FedCommunication):
                self._fed.append(it)
            elif isinstance(it, ScheduledEvent):
                self._events.append(it)
            elif isinstance(it, EarningsReport):
                self._earnings[it.ticker].append(it)
            elif isinstance(it, IndexWeights):
                self._weights.append(it)
            else:  # pragma: no cover
                raise TypeError(type(it))
        return self

    # --- lecture point-in-time ---------------------------------------------
    def series(self, series_id: str, asof: datetime) -> list[Observation]:
        asof = _aware(asof)
        latest: dict[date, Observation] = {}
        for o in self._obs.get(series_id, ()):
            if o.available_at > asof:
                continue
            cur = latest.get(o.period)
            if cur is None or o.available_at >= cur.available_at:
                latest[o.period] = o
        return [latest[p] for p in sorted(latest)]

    def fed_communications(self, asof: datetime, since: datetime) -> list[FedCommunication]:
        asof, since = _aware(asof), _aware(since)
        return sorted((c for c in self._fed if since <= c.published_at <= asof), key=lambda c: c.published_at)

    def scheduled_events(self, asof: datetime, start: datetime, end: datetime) -> list[ScheduledEvent]:
        asof, start, end = _aware(asof), _aware(start), _aware(end)
        return sorted(
            (e for e in self._events if e.known_since <= asof and start <= e.scheduled_at <= end),
            key=lambda e: e.scheduled_at,
        )

    def earnings(self, asof: datetime, ticker: str) -> list[EarningsReport]:
        asof = _aware(asof)
        return sorted((r for r in self._earnings.get(ticker, ()) if r.filed_at <= asof), key=lambda r: r.period_end)

    def index_weights(self, asof: datetime) -> IndexWeights | None:
        asof = _aware(asof)
        snaps = [w for w in self._weights if w.as_of <= asof]
        return max(snaps, key=lambda w: w.as_of) if snaps else None
