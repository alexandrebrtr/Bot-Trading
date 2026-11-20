"""Structure de marché causale : swings confirmés et Break of Structure (BOS).

Un swing high à l'indice ``p`` exige ``left`` bougies avant et ``right`` bougies
après avec un plus haut strictement inférieur. Il n'est donc **connu qu'à
l'indice ``p + right``** (``confirmed_index``). Aucun swing n'est utilisé avant
sa confirmation.

Un BOS haussier se produit à l'indice ``i`` quand la clôture de la bougie ``i``
dépasse le dernier swing high confirmé à ``i`` et pas encore cassé.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from bot_trading.core.models import Candle


class SwingKind(str, Enum):
    HIGH = "HIGH"
    LOW = "LOW"


@dataclass(frozen=True)
class Swing:
    kind: SwingKind
    index: int
    price: float
    confirmed_index: int


@dataclass(frozen=True)
class BreakOfStructure:
    bullish: bool
    index: int  # bougie dont la clôture casse le niveau (connu à sa clôture)
    level: float
    swing_index: int


def detect_swings(candles: Sequence[Candle], left: int = 2, right: int = 2) -> list[Swing]:
    swings: list[Swing] = []
    n = len(candles)
    for p in range(left, n - right):
        c = candles[p]
        window = list(range(p - left, p)) + list(range(p + 1, p + right + 1))
        if all(candles[j].high < c.high for j in window):
            swings.append(Swing(SwingKind.HIGH, p, c.high, p + right))
        if all(candles[j].low > c.low for j in window):
            swings.append(Swing(SwingKind.LOW, p, c.low, p + right))
    swings.sort(key=lambda s: (s.confirmed_index, s.index))
    return swings


def detect_bos(candles: Sequence[Candle], left: int = 2, right: int = 2) -> list[BreakOfStructure]:
    swings = detect_swings(candles, left, right)
    by_conf: dict[int, list[Swing]] = {}
    for s in swings:
        by_conf.setdefault(s.confirmed_index, []).append(s)

    events: list[BreakOfStructure] = []
    active_high: Swing | None = None
    active_low: Swing | None = None
    for i, c in enumerate(candles):
        # BOS évalué avec les swings confirmés strictement avant i
        if active_high is not None and c.close > active_high.price:
            events.append(BreakOfStructure(True, i, active_high.price, active_high.index))
            active_high = None
        if active_low is not None and c.close < active_low.price:
            events.append(BreakOfStructure(False, i, active_low.price, active_low.index))
            active_low = None
        # swings confirmés à la clôture de i, utilisables à partir de i+1
        for s in by_conf.get(i, []):
            if s.kind == SwingKind.HIGH:
                active_high = s
            else:
                active_low = s
    return events


class StructureTracker:
    """Version incrémentale de ``detect_swings`` + ``detect_bos``.

    ``update`` est appelé à la clôture de chaque bougie et renvoie les BOS
    survenus sur cette bougie. Résultats identiques à la version batch.
    """

    def __init__(self, left: int = 2, right: int = 2) -> None:
        self.left = left
        self.right = right
        self.candles: list[Candle] = []
        self.swings: list[Swing] = []
        self.bos: list[BreakOfStructure] = []
        self._active_high: Swing | None = None
        self._active_low: Swing | None = None

    def update(self, c: Candle) -> list[BreakOfStructure]:
        self.candles.append(c)
        i = len(self.candles) - 1
        events: list[BreakOfStructure] = []
        if self._active_high is not None and c.close > self._active_high.price:
            events.append(BreakOfStructure(True, i, self._active_high.price, self._active_high.index))
            self._active_high = None
        if self._active_low is not None and c.close < self._active_low.price:
            events.append(BreakOfStructure(False, i, self._active_low.price, self._active_low.index))
            self._active_low = None
        p = i - self.right
        if p >= self.left:
            cp = self.candles[p]
            window = list(range(p - self.left, p)) + list(range(p + 1, i + 1))
            if all(self.candles[j].high < cp.high for j in window):
                s = Swing(SwingKind.HIGH, p, cp.high, i)
                self.swings.append(s)
                self._active_high = s
            if all(self.candles[j].low > cp.low for j in window):
                s = Swing(SwingKind.LOW, p, cp.low, i)
                self.swings.append(s)
                self._active_low = s
        self.bos.extend(events)
        return events
