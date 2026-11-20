"""Contrôle qualité : données obsolètes, manquantes, dupliquées ou incohérentes.

Heures de cotation du CFD US500 : modèle **à vérifier** sur
``GET /api/v1/markets/US500`` (champ ``instrument.openingHours``). Hypothèse par
défaut, alignée sur les futures CME : ouverture dimanche 18:00 New York,
pause quotidienne 17:00–18:00 New York, fermeture vendredi 17:00 New York.
Les jours fériés ne sont pas modélisés : un trou un jour férié est signalé avec
``in_session=True`` et doit être lu à la lumière du calendrier.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from enum import Enum

from bot_trading.core.models import Timeframe
from bot_trading.marketdata.common import NEW_YORK, ensure_utc


class Freshness(str, Enum):
    FRESH = "FRESH"
    STALE = "STALE"
    MISSING = "MISSING"


@dataclass(frozen=True)
class MarketHours:
    """Session hebdomadaire exprimée en heure de New York."""

    daily_close: time = time(17, 0)
    daily_open: time = time(18, 0)
    week_open_weekday: int = 6  # dimanche
    week_close_weekday: int = 4  # vendredi

    def is_open(self, t: datetime) -> bool:
        local = ensure_utc(t).astimezone(NEW_YORK)
        wd, tod = local.weekday(), local.time()
        if wd == 5:  # samedi
            return False
        if wd == self.week_open_weekday:
            return tod >= self.daily_open
        if wd == self.week_close_weekday and tod >= self.daily_close:
            return False
        return not (self.daily_close <= tod < self.daily_open)

    def bar_in_session(self, start: datetime, tf: Timeframe) -> bool:
        """Une bougie est attendue si sa période recoupe une plage d'ouverture."""
        if tf == Timeframe.D1:
            local = ensure_utc(start).astimezone(NEW_YORK)
            return local.weekday() != 5
        step = timedelta(minutes=5)
        t = start
        end = start + timedelta(minutes=tf.minutes)
        while t < end:
            if self.is_open(t):
                return True
            t += step
        return False


@dataclass(frozen=True)
class Gap:
    after: datetime
    before: datetime
    missing_bars: int
    in_session: bool


@dataclass
class QualityReport:
    dataset: str
    rows: int = 0
    duplicates: list[datetime] = field(default_factory=list)
    out_of_order: list[datetime] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    freshness: Freshness = Freshness.MISSING
    last_available_at: datetime | None = None
    age: timedelta | None = None

    @property
    def session_gaps(self) -> list[Gap]:
        return [g for g in self.gaps if g.in_session]

    @property
    def ok(self) -> bool:
        return (
            self.freshness == Freshness.FRESH
            and not self.duplicates
            and not self.out_of_order
            and not self.invalid
            and not self.session_gaps
        )

    def summary(self) -> str:
        parts = [f"{self.dataset}: {self.rows} lignes, {self.freshness.value}"]
        if self.age is not None:
            parts.append(f"âge {self.age}")
        if self.session_gaps:
            parts.append(f"{len(self.session_gaps)} trou(s) en séance")
        if self.duplicates:
            parts.append(f"{len(self.duplicates)} doublon(s)")
        if self.out_of_order:
            parts.append(f"{len(self.out_of_order)} hors ordre")
        if self.invalid:
            parts.append(f"{len(self.invalid)} invalide(s)")
        return ", ".join(parts)


def check_freshness(last_available_at: datetime | None, now: datetime, max_age: timedelta) -> tuple[Freshness, timedelta | None]:
    if last_available_at is None:
        return Freshness.MISSING, None
    age = ensure_utc(now) - ensure_utc(last_available_at)
    return (Freshness.FRESH if age <= max_age else Freshness.STALE), age


def find_gaps(times: Sequence[datetime], tf: Timeframe, hours: MarketHours | None = None) -> list[Gap]:
    """Trous entre bougies consécutives, en distinguant séance et fermeture."""
    hours = hours or MarketHours()
    step = timedelta(minutes=tf.minutes)
    gaps: list[Gap] = []
    for a, b in zip(times, times[1:]):
        if b - a <= step:
            continue
        missing = []
        t = a + step
        while t < b:
            missing.append(t)
            t += step
        expected = [m for m in missing if hours.bar_in_session(m, tf)]
        if tf == Timeframe.D1 or expected:
            gaps.append(Gap(a, b, len(expected) if tf != Timeframe.D1 else len(missing), bool(expected)))
    return gaps


def candle_quality(
    dataset: str,
    rows: Sequence[dict],
    tf: Timeframe,
    now: datetime,
    *,
    max_age: timedelta | None = None,
    hours: MarketHours | None = None,
) -> QualityReport:
    """Rapport qualité pour des bougies stockées (colonnes ``event_time``,
    ``available_at`` et ``bid_*``/``ask_*`` ou ``open/high/low/close``)."""
    hours = hours or MarketHours()
    rep = QualityReport(dataset, rows=len(rows))
    seen: set[datetime] = set()
    prev: datetime | None = None
    for r in rows:
        t = r["event_time"]
        if t in seen:
            rep.duplicates.append(t)
        seen.add(t)
        if prev is not None and t < prev:
            rep.out_of_order.append(t)
        prev = t
        for side in ("bid_", "ask_", ""):
            o, h, lo, c = (r.get(f"{side}{k}") for k in ("open", "high", "low", "close"))
            if None in (o, h, lo, c):
                continue
            if not (lo <= min(o, c) and h >= max(o, c) and h >= lo):
                rep.invalid.append(f"{t.isoformat()} OHLC {side or 'mid'} incohérent")
        if r.get("bid_close") is not None and r.get("ask_close") is not None and r["ask_close"] < r["bid_close"]:
            rep.invalid.append(f"{t.isoformat()} ask < bid")
    rep.gaps = find_gaps(sorted(seen), tf, hours)
    last = max((r["available_at"] for r in rows), default=None)
    rep.last_available_at = last
    # Marché fermé : une bougie ancienne n'est pas « obsolète ».
    max_age = max_age or timedelta(minutes=tf.minutes * 2)
    if last is not None and not hours.is_open(now):
        rep.freshness, rep.age = Freshness.FRESH, ensure_utc(now) - last
    else:
        rep.freshness, rep.age = check_freshness(last, now, max_age)
    return rep


# Âge maximal toléré de la dernière donnée connue, par fréquence de publication.
MAX_AGE_BY_FREQUENCY = {
    "intraday": timedelta(minutes=15),
    "daily": timedelta(days=4),  # week-end + jour férié
    "weekly": timedelta(days=9),
    "monthly": timedelta(days=40),
    "quarterly": timedelta(days=100),
    "feed": timedelta(days=14),  # flux RSS : rien de neuf depuis 2 semaines = suspect
}


def series_quality(dataset: str, rows: Sequence[dict], now: datetime, frequency: str) -> QualityReport:
    rep = QualityReport(dataset, rows=len(rows))
    last = max((r["available_at"] for r in rows), default=None)
    rep.last_available_at = last
    rep.freshness, rep.age = check_freshness(last, now, MAX_AGE_BY_FREQUENCY[frequency])
    return rep
