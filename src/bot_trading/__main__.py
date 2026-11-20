"""Point d'entrée : ``python -m bot_trading <commande>``.

Commandes :
- ``status`` : affiche le mode et les garde-fous actifs ;
- ``replay <fichier.csv>`` : rejoue des bougies M5 (bid) dans les moteurs FVG / OB
  et le broker simulé, sans aucun ordre (diagnostic des détections) ;
- ``backtest`` : compare les benchmarks (buy & hold, trend following, technique
  seule, fondamentale seule, complète, aléatoire) et la stratégie complète du
  Decision Engine sur un CSV bid ou sur des données synthétiques, et peut lancer
  un walk-forward ;
- ``decisions`` : rejoue la stratégie complète et affiche le journal de décision
  (étage de refus, justification, risque estimé).

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
    from bot_trading.strategy import DecisionParams, DecisionStrategy

    settings = Settings(mode=TradingMode.BACKTEST)
    # R19 (marge ≤ 50 % de l'equity) appliqué à toutes les stratégies : benchmarks comparables.
    config = BacktestConfig(settings=settings, default_risk_fraction=args.risk, max_margin_fraction=0.5)
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
        factories["Complète (Decision Engine)"] = lambda: DecisionStrategy(settings, decision=DecisionParams(base_risk_fraction=args.risk))
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
        if pit is not None:
            from bot_trading.strategy import TechnicalParams

            grid = {"entry_mode": ["touch", "half", "full_reject", "bos"]}
            rep = walk_forward(
                lambda p: DecisionStrategy(settings, technical=TechnicalParams(**p), decision=DecisionParams(base_risk_fraction=args.risk)),
                grid, bars, folds, config, pit,
            )
            print("\nWalk-forward (Decision Engine, variante d'entrée EXP-03) :")
            for k, v in rep.summary().items():
                print(f"  {k}: {v}")
        if holdout:
            print(f"Réserve finale non ouverte : {holdout[0].date()} → {holdout[1].date()}")
    return 0


def cmd_decisions(args) -> int:
    """Journal de décision de la stratégie complète (données synthétiques)."""
    import json
    from collections import Counter
    from datetime import datetime, timezone

    from bot_trading.backtest.engine import BacktestConfig, Backtester
    from bot_trading.config import TradingMode
    from bot_trading.core.models import Timeframe
    from bot_trading.marketdata.source import InMemoryPointInTimeSource
    from bot_trading.marketdata.synthetic import generate_bars, synthetic_bias
    from bot_trading.strategy import DecisionParams, DecisionStrategy

    settings = Settings(mode=TradingMode.BACKTEST)
    tf = Timeframe(args.timeframe)
    start = datetime(2018, 1, 1, tzinfo=timezone.utc)
    end = start.replace(year=start.year + args.years)
    bars, labels = generate_bars(start, end, tf, seed=args.seed)
    pit = InMemoryPointInTimeSource(synthetic_bias(labels))
    config = BacktestConfig(settings=settings, max_margin_fraction=0.5)
    res = Backtester(config, pit).run(DecisionStrategy(settings, decision=DecisionParams(base_risk_fraction=args.risk)), bars)
    print(f"Données SYNTHÉTIQUES (graine {args.seed}), {len(bars)} bougies {tf.value} : ce ne sont pas des résultats de marché.")
    stages = Counter((r.decision.action.value, r.decision.meta.get("stage", "")) for r in res.decisions)
    print("\nDécisions par action et étage :")
    for (action, stage), n in stages.most_common():
        print(f"  {action:9} {stage:13} {n}")
    shown = [r for r in res.decisions if r.decision.action.value != "NO_TRADE" or r.decision.meta.get("stage") not in ("fondamental", "D1", "H4")]
    print(f"\n{min(args.last, len(shown))} dernières décisions hors refus fondamental/D1/H4 :")
    for r in shown[-args.last:]:
        d = r.decision
        risk = f"{d.estimated_risk_eur:.2f} €" if d.estimated_risk_eur is not None else "—"
        print(f"  {r.time:%Y-%m-%d %H:%M} {d.action.value:8} {r.outcome[:24]:24} conf {d.confidence or 0:.2f} risque {risk:8} {d.setup:10} {d.reason[:110]}")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            for r in res.decisions:
                row = {"time": r.time.isoformat(), "outcome": r.outcome, **r.decision.to_dict()}
                fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        print(f"\nJournal complet : {args.out}")
    m = res.metrics()
    print(f"\nTrades {m.trades}, P&L net {m.net_pnl_eur:.2f} €, DD max {m.max_drawdown:.1%}")
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
    dc = sub.add_parser("decisions")
    dc.add_argument("--timeframe", default="H1", choices=["M5", "M15", "H1"])
    dc.add_argument("--years", type=int, default=2)
    dc.add_argument("--seed", type=int, default=2)
    dc.add_argument("--risk", type=float, default=0.10)
    dc.add_argument("--last", type=int, default=15)
    dc.add_argument("--out", help="écrit le journal complet en JSONL")
    args = parser.parse_args(argv)
    if args.cmd == "backtest":
        return cmd_backtest(args)
    if args.cmd == "decisions":
        return cmd_decisions(args)
    settings = Settings.from_env()
    if args.cmd == "status":
        return cmd_status(settings)
    return cmd_replay(args.csv)


if __name__ == "__main__":
    sys.exit(main())
