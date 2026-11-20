"""Point d'entrée : ``python -m bot_trading <commande>``.

Commandes :
- ``status`` : affiche le mode et les garde-fous actifs ;
- ``replay <fichier.csv>`` : rejoue des bougies M5 (bid) dans les moteurs FVG / OB
  et le broker simulé, sans aucun ordre (diagnostic des détections) ;
- ``backtest`` : compare les benchmarks (buy & hold, trend following, technique
  seule, fondamentale seule, complète, aléatoire) sur un CSV bid ou sur des
  données synthétiques, et peut lancer un walk-forward.

Il n'existe aucune commande d'exécution réelle.
"""

from __future__ import annotations

import argparse
import sys

from bot_trading.analysis.fvg import FVGEngine
from bot_trading.analysis.order_blocks import OrderBlockEngine
from bot_trading.broker.capital_readonly import ALLOWED_ENDPOINTS
from bot_trading.config import Settings
from bot_trading.marketdata.candles import load_csv


def cmd_status(settings: Settings) -> int:
    r = settings.risk
    print(f"Mode               : {settings.mode.value} (aucun mode réel n'existe)")
    print(f"Instrument         : {settings.instrument.epic}, taille min {settings.instrument.min_size} lot")
    print(f"Capital initial    : {r.initial_capital_eur} EUR, levier max {r.max_leverage:g}:1")
    print(f"Exposition max     : {r.max_total_exposure_lots} lot")
    print(f"Perte max/position : {r.max_loss_fraction_of_margin:.0%} de la marge engagée")
    print(f"Risque max/trade   : {r.max_risk_fraction_of_equity:.0%} de l'equity")
    print(f"Drawdown max       : {r.max_account_drawdown:.0%}")
    print(f"Shorts             : {'autorisés' if r.allow_shorts else 'désactivés'}")
    print(f"Stop garanti       : {'obligatoire' if r.require_guaranteed_stop else 'facultatif'}")
    print("Capital.com        : lecture seule, endpoints autorisés :")
    for m, rx in ALLOWED_ENDPOINTS:
        print(f"  {m:6} {rx.pattern}")
    return 0


def cmd_replay(path: str) -> int:
    candles = load_csv(path)
    fvg = FVGEngine()
    ob = OrderBlockEngine()
    for c in candles:
        fvg.update(c)
        ob.update(c)
    print(f"{len(candles)} bougies, {len(fvg.fvgs)} FVG, {len(ob.blocks)} order blocks")
    print(f"FVG haussiers actifs : {len(fvg.active(True))}, OB haussiers actifs : {len(ob.active(True))}")
    return 0


def cmd_backtest(args) -> int:
    from datetime import datetime, timezone

    from bot_trading.backtest.engine import BacktestConfig
    from bot_trading.backtest.strategies import (
        BuyAndHold,
        FundamentalFilter,
        FundamentalOnly,
        FVGEntryParams,
        FVGTechnical,
        RandomEntry,
        SimpleTrendFollowing,
    )
    from bot_trading.backtest.validation import compare, metrics_table, walk_forward, walk_forward_folds
    from bot_trading.config import TradingMode
    from bot_trading.core.models import Timeframe
    from bot_trading.marketdata.source import CsvPriceSource, InMemoryPointInTimeSource
    from bot_trading.marketdata.synthetic import generate_bars, synthetic_bias

    settings = Settings(mode=TradingMode.BACKTEST)
    config = BacktestConfig(settings=settings, default_risk_fraction=args.risk)
    tf = Timeframe(args.timeframe)
    pit = None
    if args.csv:
        bars = CsvPriceSource(args.csv, tf).get_bars(settings.instrument.epic, tf)
        print("Données : CSV (aucun biais fondamental point-in-time fourni : stratégies fondamentales non évaluées)")
    else:
        start = datetime(2018, 1, 1, tzinfo=timezone.utc)
        end = start.replace(year=start.year + args.years)
        bars, labels = generate_bars(start, end, tf, seed=args.seed)
        pit = InMemoryPointInTimeSource(synthetic_bias(labels))
        print(f"Données : SYNTHÉTIQUES (graine {args.seed}), {len(bars)} bougies {tf.value}. Ce ne sont pas des résultats de marché.")

    factories = {
        "Buy & hold": lambda: BuyAndHold(settings),
        "Trend following": lambda: SimpleTrendFollowing(settings),
        "Technique seule (FVG)": lambda: FVGTechnical(settings),
        "Entrées aléatoires": lambda: RandomEntry(settings),
    }
    if pit is not None:
        factories["Fondamentale seule"] = lambda: FundamentalOnly(settings)
        factories["Complète (biais + FVG)"] = lambda: FundamentalFilter(FVGTechnical(settings))
    results = compare(factories, bars, config, pit)
    print(metrics_table(results))

    if args.walk_forward:
        folds, holdout = walk_forward_folds(bars[0].time, bars[-1].time, args.train_months, args.test_months, args.test_months, args.holdout_months)
        if not folds:
            print("Walk-forward : historique trop court pour une fenêtre.")
            return 0
        grid = {"mode": ["touch", "half", "full_reject"], "take_profit_r": [1.5, 2.0, 3.0]}
        rep = walk_forward(lambda p: FVGTechnical(settings, params=FVGEntryParams(**p)), grid, bars, folds, config, pit)
        print("\nWalk-forward (technique FVG) :")
        for k, v in rep.summary().items():
            print(f"  {k}: {v}")
        if holdout:
            print(f"Réserve finale non ouverte : {holdout[0].date()} → {holdout[1].date()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bot_trading")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    rp = sub.add_parser("replay")
    rp.add_argument("csv")
    bt = sub.add_parser("backtest")
    bt.add_argument("--csv", help="CSV bid time,open,high,low,close[,volume][,spread]")
    bt.add_argument("--timeframe", default="H1", choices=["M5", "M15", "H1", "H4", "D1"])
    bt.add_argument("--years", type=int, default=4, help="durée des données synthétiques")
    bt.add_argument("--seed", type=int, default=1)
    bt.add_argument("--risk", type=float, default=0.10, help="fraction de l'equity risquée par trade")
    bt.add_argument("--walk-forward", action="store_true")
    bt.add_argument("--train-months", type=int, default=24)
    bt.add_argument("--test-months", type=int, default=6)
    bt.add_argument("--holdout-months", type=int, default=12)
    args = parser.parse_args(argv)
    if args.cmd == "backtest":
        return cmd_backtest(args)
    settings = Settings.from_env()
    if args.cmd == "status":
        return cmd_status(settings)
    return cmd_replay(args.csv)


if __name__ == "__main__":
    sys.exit(main())
