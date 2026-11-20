"""Suivi causal d'une zone de prix (FVG, order block) après sa confirmation.

Une zone haussière (support) est attaquée par le haut : la pénétration se mesure
depuis ``top`` vers ``bottom``. Une zone baissière (résistance) est attaquée par
le bas : la pénétration se mesure depuis ``bottom`` vers ``top``.

Seules les bougies **strictement postérieures** à ``confirmed_index`` mettent à
jour l'état, ce qui interdit tout look-ahead.
"""

from __future__ import annotations

from dataclasses import dataclass

from bot_trading.core.models import Candle


@dataclass
class ZoneState:
    bullish: bool
    top: float
    bottom: float
    confirmed_index: int
    max_mitigation: float = 0.0  # 0 = intacte, 1 = entièrement traversée
    touches: int = 0  # nombre d'entrées distinctes dans la zone (retests)
    first_touch_index: int | None = None
    half_mitigated_index: int | None = None
    full_mitigated_index: int | None = None
    invalidated_index: int | None = None
    last_update_index: int | None = None
    _inside: bool = False

    @property
    def size(self) -> float:
        return self.top - self.bottom

    @property
    def midpoint(self) -> float:
        return (self.top + self.bottom) / 2

    @property
    def is_active(self) -> bool:
        return self.invalidated_index is None

    def age(self, current_index: int) -> int:
        return current_index - self.confirmed_index

    def penetration(self, candle: Candle) -> float:
        if self.size <= 0:
            return 0.0
        if self.bullish:
            raw = (self.top - candle.low) / self.size
        else:
            raw = (candle.high - self.bottom) / self.size
        return min(max(raw, 0.0), 1.0)

    def update(self, index: int, candle: Candle) -> None:
        if index <= self.confirmed_index:
            return  # pas de mise à jour avec la bougie de formation ou antérieure
        if self.last_update_index is not None and index <= self.last_update_index:
            raise ValueError("Mises à jour non chronologiques")
        self.last_update_index = index
        if self.invalidated_index is not None:
            return
        pen = self.penetration(candle)
        touching = pen > 0.0
        if touching and not self._inside:
            self.touches += 1
            if self.first_touch_index is None:
                self.first_touch_index = index
        self._inside = touching
        self.max_mitigation = max(self.max_mitigation, pen)
        if pen >= 0.5 and self.half_mitigated_index is None:
            self.half_mitigated_index = index
        if pen >= 1.0 and self.full_mitigated_index is None:
            self.full_mitigated_index = index
        # Invalidation : clôture au-delà de la zone, du mauvais côté.
        if (self.bullish and candle.close < self.bottom) or (not self.bullish and candle.close > self.top):
            self.invalidated_index = index
