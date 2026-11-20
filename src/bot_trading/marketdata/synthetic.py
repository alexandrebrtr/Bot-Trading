"""Générateur de bougies synthétiques US500, déterministe (graine), pour tests et démonstrations.

Ce n'est **pas** une donnée de marché : il sert à exercer le backtester
(sessions, gaps, régimes, spreads variables) sans réseau ni licence.

Modèle :
- marche aléatoire log-normale dont la dérive et la volatilité dépendent d'un
  régime de Markov (``bull``, ``bear``, ``chop``) ;
- séance CFD approximative : pause quotidienne 21:00–22:00 UTC, fermeture du
  vendredi 21:00 au dimanche 22:00 UTC ; à chaque réouverture, un gap ;
- spread plus large hors séance cash américaine (13:30–20:00 UTC).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from bot_trading.core.models import Candle, Timeframe
from bot_trading.marketdata.source import Bar, Observation


@dataclass(frozen=True)
class RegimeSpec:
    drift_annual: float
    vol_annual: float


DEFAULT_REGIMES: dict[str, RegimeSpec] = {
    "bull": RegimeSpec(0.20, 0.12),
    "bear": RegimeSpec(-0.20, 0.25),
    "chop": RegimeSpec(0.0, 0.16),
}


def is_session_open(t: datetime) -> bool:
    """Séance CFD US500 approximative (UTC). À recaler sur ``openingHours`` de Capital.com."""
    wd = t.weekday()
    h = t.hour
    if wd == 5:
        return False
    if wd == 6:
        return h >= 22
    if wd == 4 and h >= 21:
        return False
    return h != 21


def _cash_session(t: datetime) -> bool:
    m = t.hour * 60 + t.minute
    return t.weekday() < 5 and 13 * 60 + 30 <= m < 20 * 60


def generate_bars(
    start: datetime,
    end: datetime,
    timeframe: Timeframe = Timeframe.H1,
    seed: int = 1,
    start_price: float = 5000.0,
    regimes: dict[str, RegimeSpec] | None = None,
    regime_persistence_days: float = 90.0,
    gap_vol_multiplier: float = 4.0,
    spread_cash: float = 0.6,
    spread_off_hours: float = 1.2,
) -> tuple[list[Bar], dict[datetime, str]]:
    """Renvoie les bougies et le régime « vrai » de chacune (pour l'évaluation seulement)."""
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    regimes = regimes or DEFAULT_REGIMES
    names = list(regimes)
    rng = random.Random(seed)
    step = timedelta(minutes=timeframe.minutes)
    bars_per_year = 252 * 23 * 60 / timeframe.minutes
    switch_p = 1.0 / (regime_persistence_days * 23 * 60 / timeframe.minutes)

    regime = names[0]
    price = start_price
    t = start
    bars: list[Bar] = []
    labels: dict[datetime, str] = {}
    was_closed = False
    while t < end:
        if not is_session_open(t):
            was_closed = True
            t += step
            continue
        if rng.random() < switch_p:
            regime = rng.choice([n for n in names if n != regime])
        spec = regimes[regime]
        sigma = spec.vol_annual / math.sqrt(bars_per_year)
        mu = spec.drift_annual / bars_per_year - 0.5 * sigma * sigma
        o = price
        if was_closed:
            o = price * math.exp(rng.gauss(0.0, sigma * gap_vol_multiplier))
            was_closed = False
        c = o * math.exp(mu + rng.gauss(0.0, sigma))
        wick = abs(rng.gauss(0.0, sigma * 0.6)) * o
        h = max(o, c) + wick * rng.random()
        lo = min(o, c) - wick * rng.random()
        candle = Candle(t, round(o, 2), round(max(h, o, c), 2), round(min(lo, o, c), 2), round(c, 2))
        spread = spread_cash if _cash_session(t) else spread_off_hours
        bars.append(Bar(candle, timeframe, spread))
        labels[t] = regime
        price = c
        t += step
    return bars, labels


def synthetic_bias(
    labels: dict[datetime, str],
    lag: timedelta = timedelta(days=5),
    every: timedelta = timedelta(days=7),
    series: str = "bias/swing",
) -> list[Observation]:
    """Biais « fondamental » synthétique : le régime vrai, publié avec retard et à pas fixe.

    Uniquement pour exercer le chemin point-in-time dans les tests et la démo :
    ce n'est pas un signal. ``available_at`` = instant d'échantillonnage + ``lag``.
    """
    mapping = {"bull": "bullish", "bear": "bearish", "chop": "neutral"}
    out: list[Observation] = []
    times = sorted(labels)
    if not times:
        return out
    t = times[0]
    i = 0
    while t <= times[-1]:
        while i + 1 < len(times) and times[i + 1] <= t:
            i += 1
        reg = labels[times[i]]
        out.append(Observation(series, t, mapping.get(reg, "neutral"), t + lag, "synthetic"))
        out.append(Observation("regime", t, reg, t + lag, "synthetic"))
        t += every
    return out
