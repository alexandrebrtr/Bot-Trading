"""Filtre des obstacles avant une position longue (mission §7, STRATEGY T4 et E6).

Obstacles recherchés au-dessus du prix d'entrée :
- FVG baissiers actifs (H1, H4, D1) ;
- order blocks vendeurs actifs (H1, H4) ;
- résistances majeures : swings hauts H4/D1 confirmés et jamais dépassés en clôture.

La distance jusqu'au premier obstacle est exprimée en **R** (risque initial :
entrée − stop), en **ATR** du timeframe du setup et en **spreads**. L'entrée est
refusée si la place disponible est inférieure à ``min_room_r`` R (🎛️ E6 ∈ [1,5 ; 4]).
Si le prix d'entrée est déjà dans une zone vendeuse, la place est nulle.
"""

from __future__ import annotations

from dataclasses import dataclass

from bot_trading.core.models import Timeframe
from bot_trading.strategy.technical import TechnicalEngine


@dataclass(frozen=True)
class Obstacle:
    kind: str  # FVG_BEAR, OB_BEAR, SWING_HIGH
    timeframe: Timeframe
    bottom: float
    top: float

    @property
    def name(self) -> str:
        return f"{self.kind}_{self.timeframe.value}"


@dataclass(frozen=True)
class ObstacleReport:
    entry: float
    risk_points: float
    first: Obstacle | None
    room_points: float | None  # None : aucun obstacle identifié
    room_r: float | None
    room_atr: float | None
    room_spreads: float | None
    min_room_r: float
    passed: bool
    obstacles: tuple[Obstacle, ...]

    def describe(self) -> str:
        if self.first is None:
            return "Aucun obstacle identifié au-dessus de l'entrée"
        return (
            f"Premier obstacle {self.first.name} à {self.first.bottom:.1f} : "
            f"{self.room_points:.1f} pts = {self.room_r:.2f} R"
            + (f" = {self.room_atr:.2f} ATR" if self.room_atr is not None else "")
            + (f" = {self.room_spreads:.0f} spreads" if self.room_spreads is not None else "")
            + f" (minimum {self.min_room_r:g} R)"
        )


@dataclass(frozen=True)
class ObstacleParams:
    min_room_r: float = 1.5
    fvg_timeframes: tuple[Timeframe, ...] = (Timeframe.H1, Timeframe.H4, Timeframe.D1)
    ob_timeframes: tuple[Timeframe, ...] = (Timeframe.H1, Timeframe.H4)
    swing_timeframes: tuple[Timeframe, ...] = (Timeframe.H4, Timeframe.D1)


def collect_obstacles(tech: TechnicalEngine, entry: float, p: ObstacleParams) -> list[Obstacle]:
    out: list[Obstacle] = []
    for tf in p.fvg_timeframes:
        st = tech.state(tf)
        if st is None:
            continue
        out += [Obstacle("FVG_BEAR", tf, f.bottom, f.top) for f in st.fvg.active(False) if f.top > entry]
    for tf in p.ob_timeframes:
        st = tech.state(tf)
        if st is None:
            continue
        out += [Obstacle("OB_BEAR", tf, o.bottom, o.top) for o in st.ob.active(False) if o.top > entry]
    for tf in p.swing_timeframes:
        st = tech.state(tf)
        if st is None:
            continue
        out += [Obstacle("SWING_HIGH", tf, px, px) for _, px in st.unbroken_highs if px > entry]
    out.sort(key=lambda o: o.bottom)
    return out


def check_obstacles(tech: TechnicalEngine, entry: float, stop: float, spread: float, atr: float | None, p: ObstacleParams | None = None) -> ObstacleReport:
    p = p or ObstacleParams()
    risk = entry - stop
    obstacles = collect_obstacles(tech, entry, p)
    first = obstacles[0] if obstacles else None
    if first is None:
        return ObstacleReport(entry, risk, None, None, None, None, None, p.min_room_r, risk > 0, ())
    room = max(first.bottom - entry, 0.0)
    room_r = room / risk if risk > 0 else 0.0
    return ObstacleReport(
        entry=entry,
        risk_points=risk,
        first=first,
        room_points=room,
        room_r=room_r,
        room_atr=room / atr if atr else None,
        room_spreads=room / spread if spread > 0 else None,
        min_room_r=p.min_room_r,
        passed=risk > 0 and room_r >= p.min_room_r,
        obstacles=tuple(obstacles),
    )
