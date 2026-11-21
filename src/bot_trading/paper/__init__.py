"""Paper trading local : le moteur de décision sur des prix réels, des ordres simulés.

- ``trader.PaperTrader`` : boucle qui consomme chaque nouvelle bougie close
  avec ``backtest.engine.EventLoop`` (le même code que le backtester), persiste
  l'état et publie compte, positions, trades et décisions pour le dashboard ;
- ``feeds`` : bougies closes lues dans le store Parquet (``StoreBarFeed``),
  rafraîchi en lecture seule depuis Capital.com (``CapitalRefresher``) ;
- ``regime.RuntimeRegimeProvider`` : vue fondamentale depuis ``regime.json``
  (Market Regime Engine), jamais une évaluation publiée après l'instant de décision.

Aucun ordre réel : le seul broker utilisé est ``SimulatedBroker``. Ce paquet
n'importe pas le client Capital.com en dehors de ``feeds.CapitalRefresher``,
qui ne sait faire que des lectures (liste blanche de ``capital_readonly``).
"""

from bot_trading.paper.feeds import StoreBarFeed
from bot_trading.paper.regime import RuntimeRegimeProvider
from bot_trading.paper.trader import PaperConfig, PaperTrader

__all__ = ["PaperConfig", "PaperTrader", "RuntimeRegimeProvider", "StoreBarFeed"]
