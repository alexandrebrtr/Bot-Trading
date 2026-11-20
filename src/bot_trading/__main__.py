"""Point d'entrée : ``python -m bot_trading <commande>``.

Commandes :
- ``status`` : affiche le mode et les garde-fous actifs ;
- ``replay <fichier.csv>`` : rejoue des bougies M5 (bid) dans les moteurs FVG / OB
  et le broker simulé, sans aucun ordre (diagnostic des détections) ;
- ``backtest`` : compare les benchmarks (buy & hold, trend following, technique
  seule, fondamentale seule, complète, aléatoire) sur un CSV bid ou sur des
  données synthétiques, et peut lancer un walk-forward ;
- ``dashboard`` : lance le dashboard Streamlit en lecture seule (``--demo`` pour
  des données synthétiques) ;
- ``kill-switch`` : affiche, engage ou lève (confirmation humaine) le kill switch.

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


def cmd_dashboard(args) -> int:
    import importlib.util
    import os
    import subprocess
    from pathlib import Path

    if importlib.util.find_spec("streamlit") is None or importlib.util.find_spec("plotly") is None:
        print("Le dashboard demande Streamlit et Plotly : pip install -e \".[dashboard]\"", file=sys.stderr)
        return 2
    runtime, store = Path(args.runtime_dir), Path(args.store_dir)
    env = dict(os.environ)
    if args.demo:
        from bot_trading.dashboard.demo import build_demo

        root = build_demo(args.demo_dir)
        runtime, store = root / "runtime", root / "store"
        env["BOT_DASHBOARD_DEMO"] = "1"
        print(f"Données de DÉMO (synthétiques) écrites dans {root}")
    env["BOT_RUNTIME_DIR"] = str(runtime.resolve())
    env["BOT_STORE_DIR"] = str(store.resolve())
    env["BOT_REPO_ROOT"] = str(Path.cwd())
    app = Path(__file__).resolve().parent / "dashboard" / "app.py"
    cmd = [
        sys.executable, "-m", "streamlit", "run", str(app),
        "--server.address", "127.0.0.1",  # jamais exposé sur le réseau local
        "--server.port", str(args.port),
        "--browser.gatherUsageStats", "false",
        "--server.headless", "true" if args.no_browser else "false",
    ]
    print(f"Dashboard : http://127.0.0.1:{args.port}  (Ctrl+C pour arrêter)")
    try:
        return subprocess.call(cmd, env=env)
    except KeyboardInterrupt:
        return 0


def cmd_kill_switch(args, settings: Settings) -> int:
    from bot_trading.risk.engine import RiskEngine
    from bot_trading.risk.kill_switch import KillSwitchStore

    store = KillSwitchStore(args.path)
    engine = RiskEngine(settings.risk, settings.instrument, settings.costs)
    store.apply(engine)
    if args.engage:
        state = store.engage(engine, args.reason or "arrêt manuel (CLI)", actor="cli")
    elif args.reset:
        try:
            state = store.reset(engine, args.confirm or "", actor="cli")
        except PermissionError as e:
            print(e, file=sys.stderr)
            return 1
    else:
        state = store.load()
    print(f"Kill switch : {'ENGAGÉ' if state.engaged else 'inactif'}" + (f" ({state.reason}, {state.changed_at})" if state.changed_at or state.reason else ""))
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
    db = sub.add_parser("dashboard", help="dashboard Streamlit en lecture seule")
    db.add_argument("--demo", action="store_true", help="données synthétiques (data/demo)")
    db.add_argument("--demo-dir", default="data/demo")
    db.add_argument("--runtime-dir", default="data/runtime")
    db.add_argument("--store-dir", default="data/store")
    db.add_argument("--port", type=int, default=8501)
    db.add_argument("--no-browser", action="store_true", help="ne pas ouvrir le navigateur")
    ks = sub.add_parser("kill-switch", help="état, engagement ou reprise du kill switch")
    ks.add_argument("--path", default="data/runtime/kill_switch.json")
    g = ks.add_mutually_exclusive_group()
    g.add_argument("--engage", action="store_true")
    g.add_argument("--reset", action="store_true")
    ks.add_argument("--reason", default="")
    ks.add_argument("--confirm", default="", help="JE_CONFIRME_LA_REPRISE pour --reset")
    args = parser.parse_args(argv)
    if args.cmd == "backtest":
        return cmd_backtest(args)
    if args.cmd == "dashboard":
        return cmd_dashboard(args)
    settings = Settings.from_env()
    if args.cmd == "kill-switch":
        return cmd_kill_switch(args, settings)
    if args.cmd == "status":
        return cmd_status(settings)
    return cmd_replay(args.csv)


if __name__ == "__main__":
    sys.exit(main())
