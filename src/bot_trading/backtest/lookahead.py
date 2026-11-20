"""Tests de look-ahead applicables à n'importe quelle stratégie.

``check_prefix_invariance`` : les décisions prises jusqu'à la bougie ``cut`` sont
identiques que la série s'arrête à ``cut`` ou continue.

``check_future_independence`` : remplacer toutes les bougies après ``cut`` par un
autre futur ne change aucune décision ni aucune equity jusqu'à ``cut``.

Une stratégie qui lirait une bougie future (même indirectement, via un
indicateur centré ou un tableau précalculé) échoue à au moins un des deux.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Sequence

from bot_trading.backtest.engine import Backtester, BacktestConfig, BacktestResult
from bot_trading.marketdata.source import Bar, PointInTimeSource


def _make(factory: Callable, bars: Sequence[Bar]):
    """La fabrique peut recevoir les bougies du run : cela permet de détecter les
    stratégies qui précalculent un indicateur sur toute la série."""
    try:
        n = len(inspect.signature(factory).parameters)
    except (TypeError, ValueError):
        n = 0
    return factory(bars) if n >= 1 else factory()


def _signature(res: BacktestResult, last_index: int) -> list[tuple]:
    """Décisions d'indice ≤ ``last_index``. Le résultat d'exécution de la décision
    ``last_index`` dépend de la bougie suivante : il est exclu."""
    sig = []
    for d in res.decisions:
        if d.index <= last_index:
            x = d.decision
            outcome = d.outcome if d.index < last_index else None
            sig.append((d.index, x.action, x.stop_level, x.take_profit_level, x.size, x.risk_fraction, x.bias, outcome))
    return sig


def _equity(res: BacktestResult, until) -> list[tuple]:
    return [(t, round(e, 9)) for t, e in res.equity_curve if t < until]


def check_prefix_invariance(factory: Callable[[], object], bars: Sequence[Bar], cut: int, config: BacktestConfig | None = None, pit: PointInTimeSource | None = None) -> None:
    bt = Backtester(config, pit)
    full = bt.run(_make(factory, bars), bars)
    part = bt.run(_make(factory, bars[: cut + 1]), bars[: cut + 1])
    # La dernière décision de la série tronquée n'est jamais exécutée : on compare avant.
    if _signature(full, cut - 1) != _signature(part, cut - 1):
        raise AssertionError(f"Décisions différentes avant la bougie {cut} : look-ahead probable")
    until = bars[cut - 1].time
    if _equity(full, until) != _equity(part, until):
        raise AssertionError(f"Equity différente avant {until} : look-ahead probable")


def check_future_independence(factory: Callable[[], object], bars: Sequence[Bar], alt_future: Sequence[Bar], cut: int, config: BacktestConfig | None = None, pit: PointInTimeSource | None = None) -> None:
    if alt_future and alt_future[0].time <= bars[cut].time:
        raise ValueError("Le futur alternatif doit commencer après la bougie cut")
    bt = Backtester(config, pit)
    other = list(bars[: cut + 1]) + list(alt_future)
    a = bt.run(_make(factory, bars), bars)
    b = bt.run(_make(factory, other), other)
    if _signature(a, cut) != _signature(b, cut):
        raise AssertionError(f"Un autre futur change les décisions avant la bougie {cut} : look-ahead")
    until = bars[cut].time
    if _equity(a, until) != _equity(b, until):
        raise AssertionError(f"Un autre futur change l'equity avant {until} : look-ahead")
