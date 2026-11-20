"""Order Block Engine déterministe et causal.

Définition (mission, section 7) :
- Order block haussier : dernière bougie baissière précédant une impulsion
  haussière significative ayant provoqué une cassure de structure confirmée.
- Order block baissier : symétrique (dernière bougie haussière avant une
  impulsion baissière qui casse un swing low).

Algorithme, à la clôture de la bougie ``b`` qui produit un BOS haussier :
1. Remonter depuis ``b`` (au plus ``max_lookback`` bougies) jusqu'à la dernière
   bougie baissière ``k`` : c'est l'order block candidat.
2. Déplacement = plus haut de ``k+1..b`` − plus bas de ``k``, exprimé en ATR
   (ATR connu à ``b``). L'OB n'est retenu que si le déplacement atteint
   ``min_displacement_atr``.
3. L'OB est confirmé à ``b`` (``confirmed_index``) : il n'existe pas avant.

Qualification : déplacement/ATR, présence d'un FVG dans l'impulsion, timeframe.
La mitigation, les retests et l'invalidation sont suivis par ``ZoneState`` avec
les bougies postérieures uniquement.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from bot_trading.analysis.indicators import IncrementalATR
from bot_trading.analysis.structure import BreakOfStructure, StructureTracker
from bot_trading.analysis.zones import ZoneState
from bot_trading.core.models import Candle, Timeframe


@dataclass(frozen=True)
class OBParams:
    atr_period: int = 14
    swing_left: int = 2
    swing_right: int = 2
    max_lookback: int = 20
    min_displacement_atr: float = 1.0  # à optimiser par backtest


@dataclass
class OrderBlock:
    bullish: bool
    timeframe: Timeframe | None
    index: int  # bougie de l'order block
    confirmed_index: int  # bougie du BOS
    top: float
    bottom: float
    body_top: float
    body_bottom: float
    displacement_atr: float | None
    has_fvg: bool
    bos_level: float
    state: ZoneState = field(repr=False)


class OrderBlockEngine:
    def __init__(self, params: OBParams | None = None, timeframe: Timeframe | None = None) -> None:
        self.params = params or OBParams()
        self.timeframe = timeframe
        self.candles: list[Candle] = []
        self.blocks: list[OrderBlock] = []
        self.atr = IncrementalATR(self.params.atr_period)
        self.structure = StructureTracker(self.params.swing_left, self.params.swing_right)

    def update(self, candle: Candle) -> list[OrderBlock]:
        if self.candles and candle.time <= self.candles[-1].time:
            raise ValueError("Bougies non chronologiques")
        self.candles.append(candle)
        i = len(self.candles) - 1
        self.atr.update(candle)
        events = self.structure.update(candle)
        for ob in self.blocks:
            ob.state.update(i, candle)

        new: list[OrderBlock] = []
        for e in events:
            ob = self._build(e)
            if ob is not None:
                new.append(ob)
        self.blocks.extend(new)
        return new

    def _build(self, e: BreakOfStructure) -> OrderBlock | None:
        b = e.index
        cs = self.candles
        lo = max(0, b - self.params.max_lookback)
        k = None
        for j in range(b - 1, lo - 1, -1):
            if (e.bullish and cs[j].is_bearish) or (not e.bullish and cs[j].is_bullish):
                k = j
                break
        if k is None:
            return None
        ob_c = cs[k]
        leg = cs[k + 1 : b + 1]
        if e.bullish:
            displacement = max(c.high for c in leg) - ob_c.low
        else:
            displacement = ob_c.high - min(c.low for c in leg)
        a = self.atr.values[b]
        disp_atr = displacement / a if a else None
        if disp_atr is not None and disp_atr < self.params.min_displacement_atr:
            return None
        has_fvg = False
        for t in range(k + 2, b + 1):  # FVG dont c1 >= k et c3 <= b
            c1, c3 = cs[t - 2], cs[t]
            if e.bullish and c3.low > c1.high:
                has_fvg = True
            if not e.bullish and c3.high < c1.low:
                has_fvg = True
        return OrderBlock(
            bullish=e.bullish,
            timeframe=self.timeframe,
            index=k,
            confirmed_index=b,
            top=ob_c.high,
            bottom=ob_c.low,
            body_top=max(ob_c.open, ob_c.close),
            body_bottom=min(ob_c.open, ob_c.close),
            displacement_atr=disp_atr,
            has_fvg=has_fvg,
            bos_level=e.level,
            state=ZoneState(bullish=e.bullish, top=ob_c.high, bottom=ob_c.low, confirmed_index=b),
        )

    def active(self, bullish: bool | None = None) -> list[OrderBlock]:
        return [o for o in self.blocks if o.state.is_active and (bullish is None or o.bullish == bullish)]


def detect_order_blocks(candles: Sequence[Candle], params: OBParams | None = None, timeframe: Timeframe | None = None) -> list[OrderBlock]:
    engine = OrderBlockEngine(params, timeframe)
    for c in candles:
        engine.update(c)
    return engine.blocks
