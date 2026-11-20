"""Indicateurs causaux : la valeur à l'indice i n'utilise que les bougies 0..i."""

from __future__ import annotations

from collections.abc import Sequence

from bot_trading.core.models import Candle


def true_ranges(candles: Sequence[Candle]) -> list[float]:
    out: list[float] = []
    for i, c in enumerate(candles):
        if i == 0:
            out.append(c.high - c.low)
        else:
            pc = candles[i - 1].close
            out.append(max(c.high - c.low, abs(c.high - pc), abs(c.low - pc)))
    return out


def atr(candles: Sequence[Candle], period: int = 14) -> list[float | None]:
    """ATR de Wilder. ``None`` tant que ``period`` bougies ne sont pas disponibles."""
    if period < 1:
        raise ValueError("period >= 1")
    trs = true_ranges(candles)
    out: list[float | None] = []
    value: float | None = None
    for i, tr in enumerate(trs):
        if i + 1 < period:
            out.append(None)
            continue
        if value is None:
            value = sum(trs[: period]) / period
        else:
            value = (value * (period - 1) + tr) / period
        out.append(value)
    return out


def sma(values: Sequence[float], period: int) -> list[float | None]:
    out: list[float | None] = []
    acc = 0.0
    for i, v in enumerate(values):
        acc += v
        if i >= period:
            acc -= values[i - period]
        out.append(acc / period if i + 1 >= period else None)
    return out


class IncrementalATR:
    """ATR de Wilder mis à jour bougie par bougie (mêmes valeurs que ``atr``)."""

    def __init__(self, period: int = 14) -> None:
        if period < 1:
            raise ValueError("period >= 1")
        self.period = period
        self.values: list[float | None] = []
        self._trs: list[float] = []
        self._prev_close: float | None = None
        self._value: float | None = None

    def update(self, c: Candle) -> float | None:
        if self._prev_close is None:
            tr = c.high - c.low
        else:
            pc = self._prev_close
            tr = max(c.high - c.low, abs(c.high - pc), abs(c.low - pc))
        self._prev_close = c.close
        n = len(self.values) + 1
        if self._value is None:
            self._trs.append(tr)
            if n >= self.period:
                self._value = sum(self._trs[: self.period]) / self.period
                self._trs.clear()
        else:
            self._value = (self._value * (self.period - 1) + tr) / self.period
        self.values.append(self._value)
        return self._value
