"""Fair Value Gap Engine déterministe et causal.

Définitions (mission, section 6) :
- FVG haussier : bougies consécutives c1, c2, c3 avec ``c3.low > c1.high``.
  Zone = [c1.high, c3.low].
- FVG baissier : ``c3.high < c1.low``. Zone = [c3.high, c1.low].
- Un FVG n'existe qu'**après la clôture de c3** : ``confirmed_index`` = indice de c3.

Qualification calculée uniquement avec des données disponibles à ``confirmed_index`` :
taille / ATR, impulsion de c2 / ATR, présence d'un BOS dans c1..c3, timeframe.
Le suivi de mitigation et de retests (``ZoneState``) n'utilise que les bougies
postérieures.

Les seuils (taille minimale, impulsion, mitigation d'entrée…) sont des paramètres
à optimiser par backtest puis à valider hors échantillon : aucune valeur n'est
présentée ici comme optimale.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from bot_trading.analysis.indicators import IncrementalATR
from bot_trading.analysis.structure import StructureTracker
from bot_trading.analysis.zones import ZoneState
from bot_trading.core.models import Candle, Timeframe


@dataclass
class FairValueGap:
    bullish: bool
    timeframe: Timeframe | None
    first_index: int  # c1
    confirmed_index: int  # c3
    top: float
    bottom: float
    atr_at_confirmation: float | None
    size_atr: float | None
    impulse_atr: float | None
    with_bos: bool
    state: ZoneState = field(repr=False)

    @property
    def size(self) -> float:
        return self.top - self.bottom


@dataclass(frozen=True)
class FVGParams:
    atr_period: int = 14
    min_size_atr: float = 0.0  # 0 = pas de filtre, à optimiser
    swing_left: int = 2
    swing_right: int = 2


class FVGEngine:
    """Moteur incrémental. ``update`` est appelé à la clôture de chaque bougie."""

    def __init__(self, params: FVGParams | None = None, timeframe: Timeframe | None = None) -> None:
        self.params = params or FVGParams()
        self.timeframe = timeframe
        self.candles: list[Candle] = []
        self.fvgs: list[FairValueGap] = []
        self.atr = IncrementalATR(self.params.atr_period)
        self.structure = StructureTracker(self.params.swing_left, self.params.swing_right)

    def update(self, candle: Candle) -> list[FairValueGap]:
        if self.candles and candle.time <= self.candles[-1].time:
            raise ValueError("Bougies non chronologiques")
        self.candles.append(candle)
        i = len(self.candles) - 1
        self.atr.update(candle)
        self.structure.update(candle)

        for f in self.fvgs:
            f.state.update(i, candle)

        if i < 2:
            return []
        c1, c2, c3 = self.candles[i - 2], self.candles[i - 1], self.candles[i]
        new: list[FairValueGap] = []
        a = self.atr.values[i]
        a2 = self.atr.values[i - 1]
        recent = [e for e in self.structure.bos[-6:] if i - 2 <= e.index <= i]
        bos_bull = any(e.bullish for e in recent)
        bos_bear = any(not e.bullish for e in recent)

        if c3.low > c1.high:
            new.append(self._make(True, i, top=c3.low, bottom=c1.high, atr_val=a, impulse=c2.body, atr_mid=a2, bos=bos_bull))
        if c3.high < c1.low:
            new.append(self._make(False, i, top=c1.low, bottom=c3.high, atr_val=a, impulse=c2.body, atr_mid=a2, bos=bos_bear))

        kept = [f for f in new if f.size_atr is None or f.size_atr >= self.params.min_size_atr]
        self.fvgs.extend(kept)
        return kept

    def _make(self, bullish: bool, i: int, top: float, bottom: float, atr_val, impulse: float, atr_mid, bos: bool) -> FairValueGap:
        size = top - bottom
        return FairValueGap(
            bullish=bullish,
            timeframe=self.timeframe,
            first_index=i - 2,
            confirmed_index=i,
            top=top,
            bottom=bottom,
            atr_at_confirmation=atr_val,
            size_atr=(size / atr_val) if atr_val else None,
            impulse_atr=(impulse / atr_mid) if atr_mid else None,
            with_bos=bos,
            state=ZoneState(bullish=bullish, top=top, bottom=bottom, confirmed_index=i),
        )

    def active(self, bullish: bool | None = None) -> list[FairValueGap]:
        return [f for f in self.fvgs if f.state.is_active and (bullish is None or f.bullish == bullish)]


def detect_fvgs(candles: Sequence[Candle], params: FVGParams | None = None, timeframe: Timeframe | None = None) -> list[FairValueGap]:
    """Rejoue les bougies une à une dans le moteur incrémental (pas de vue globale)."""
    engine = FVGEngine(params, timeframe)
    for c in candles:
        engine.update(c)
    return engine.fvgs
