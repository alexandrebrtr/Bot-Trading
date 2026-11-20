"""Moteur de backtest événementiel (mission, section 17).

- ``engine`` : boucle chronologique, réutilise ``SimulatedBroker`` et ``RiskEngine`` ;
- ``strategies`` : benchmarks et filtre fondamental ;
- ``metrics`` : performance, risque, Sharpe déflaté, risque de ruine ;
- ``validation`` : walk-forward, réserve finale, stress tests, comparaison ;
- ``lookahead`` : tests de look-ahead applicables à toute stratégie.
"""

from bot_trading.backtest.engine import (  # noqa: F401
    BacktestConfig,
    Backtester,
    BacktestResult,
    BarContext,
    LookaheadError,
    Strategy,
)
