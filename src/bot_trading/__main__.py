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
  (étage de refus, justification, risque estimé) ; ``--publish`` l'écrit au
  format du dashboard ;
- ``dashboard`` : lance le dashboard Streamlit en lecture seule (``--demo`` pour
  des données synthétiques) ;
- ``kill-switch`` : affiche, engage ou lève (confirmation humaine) le kill switch ;
- ``paper`` : paper trading local sur les bougies réelles (``run``, ``status``,
  ``reset``). Ordres simulés uniquement ;
- ``improve`` : un cycle des boucles d'amélioration (journal décisions/résultats,
  analyse post-trade, ré-évaluation walk-forward) ; propose, n'applique rien.
  ``--review ID --reviewer NOM --approve|--reject`` trace une revue humaine.

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
    if args.publish:
        from bot_trading.dashboard.decision_feed import publish_decision

        for r in res.decisions:
            violations = r.outcome.removeprefix("VETO: ").split(", ") if r.outcome.startswith("VETO") else ()
            publish_decision(args.publish, r.decision, time=r.time, outcome=r.outcome, violations=violations, timeframe=tf.value)
        print(f"Décisions publiées pour le dashboard : {args.publish}")
    m = res.metrics()
    print(f"\nTrades {m.trades}, P&L net {m.net_pnl_eur:.2f} €, DD max {m.max_drawdown:.1%}")
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


def cmd_paper(args) -> int:
    """Paper trading : moteur de décision sur bougies réelles, broker simulé, aucun ordre réel."""
    import json
    from datetime import timedelta
    from pathlib import Path

    from bot_trading.core.models import Timeframe
    from bot_trading.marketdata.common import load_dotenv

    runtime = Path(args.runtime_dir)
    if args.paper_cmd == "status":
        p = runtime / "account.json"
        if not p.exists():
            print(f"Aucun compte paper dans {runtime} (lancer : python -m bot_trading paper run)")
            return 0
        a = json.loads(p.read_text(encoding="utf-8"))
        pos = json.loads((runtime / "positions.json").read_text(encoding="utf-8")) if (runtime / "positions.json").exists() else []
        print(f"Mode paper (aucun ordre réel) · {a.get('timeframe', '?')} · mis à jour {a.get('updated_at')}")
        print(f"Equity {a['equity_eur']:.2f} € · solde {a.get('balance_eur', 0):.2f} € · plus haut {a.get('peak_equity_eur', 0):.2f} €")
        print(f"Dernière bougie {a.get('last_bar_time')} · ordre en attente {a.get('pending_order') or 'aucun'} · kill switch {'ENGAGÉ' if a.get('kill_switch') else 'inactif'}")
        for x in pos:
            print(f"  {x['deal_id']} {x['size']:g} lot @ {x['entry_price']:.1f} stop {x['stop_level']:.1f} P&L latent {x.get('unrealized_pnl_eur') or 0:.2f} €")
        return 0
    if args.paper_cmd == "reset":
        from bot_trading.paper.trader import archive_runtime
        from bot_trading.risk.kill_switch import CONFIRMATION

        if args.confirm != CONFIRMATION:
            print(f"Archivage du compte paper refusé : ajouter --confirm {CONFIRMATION}", file=sys.stderr)
            return 1
        dest = archive_runtime(runtime)
        print(f"Compte paper archivé dans {dest}" if dest else "Rien à archiver.")
        return 0

    from bot_trading.marketdata.engine import DataEngine
    from bot_trading.marketdata.storage import ParquetStore
    from bot_trading.paper import PaperConfig, PaperTrader, StoreBarFeed

    load_dotenv(args.env)
    settings = Settings.from_env()
    tf = Timeframe(args.timeframe)
    store = ParquetStore(args.store_dir)
    feed = StoreBarFeed(store, settings.instrument.epic, tf, tuple(args.sources))
    cfg = PaperConfig(runtime_dir=runtime, timeframe=tf, risk_fraction=args.risk, warmup=timedelta(days=args.warmup_days))
    trader = PaperTrader(feed, settings, cfg)
    refresher = None
    if args.refresh_capital:
        from bot_trading.paper.feeds import CapitalRefresher

        refresher = CapitalRefresher(DataEngine(store), settings.instrument.epic, (tf,))

    def report(r) -> None:
        print(f"[{r.time:%Y-%m-%d %H:%M:%S} UTC] {r.message}")
        for d in r.decisions:
            print(f"    {d.time:%Y-%m-%d %H:%M} {d.decision.action.value:8} {d.outcome[:28]:28} {d.decision.reason[:100]}")

    print("Paper trading : ordres SIMULÉS uniquement (aucun ordre réel n'existe dans ce dépôt).")
    try:
        trader.run(
            interval_seconds=args.interval,
            max_cycles=1 if args.once else None,
            before_cycle=refresher.refresh if refresher else None,
            on_report=report,
        )
    except KeyboardInterrupt:
        print("Arrêt demandé : état sauvegardé après chaque bougie.")
    finally:
        if refresher:
            refresher.close()
    return 0


def _parse_grid(items: list[str]) -> dict:
    def conv(v: str):
        if v.lower() == "none":
            return None
        for t in (int, float):
            try:
                return t(v)
            except ValueError:
                pass
        return v

    grid: dict = {}
    for it in items:
        k, _, vals = it.partition("=")
        grid[k.strip()] = [conv(v.strip()) for v in vals.split(",") if v.strip()]
    return grid


def cmd_improve(args) -> int:
    from datetime import datetime, timezone
    from pathlib import Path

    from bot_trading.backtest.engine import Backtester, BacktestConfig
    from bot_trading.config import TradingMode
    from bot_trading.core.models import Timeframe
    from bot_trading.improvement import ExperimentRegistry, ProtectedParameterError, from_backtest, from_runtime, run_cycle
    from bot_trading.improvement.reevaluation import decision_factory, validate_grid
    from bot_trading.marketdata.source import InMemoryPointInTimeSource
    from bot_trading.marketdata.synthetic import generate_bars, synthetic_bias

    out_dir = Path(args.out_dir)
    registry = ExperimentRegistry(out_dir / "experiments.jsonl")
    if args.review:
        try:
            rec = registry.review(args.review, args.reviewer or "", approve=args.approve, note=args.note)
        except (ValueError, KeyError) as e:
            print(e, file=sys.stderr)
            return 1
        print(f"Revue enregistrée : {rec['status']} (aucun paramètre appliqué)")
        return 0

    settings = Settings(mode=TradingMode.BACKTEST)
    current = _parse_grid(args.current) if args.current else {}
    current = {k: v[0] for k, v in current.items()}
    grid = _parse_grid(args.grid) if args.grid else {"t.entry_mode": ["touch", "half", "full_reject", "bos"]}
    try:
        validate_grid(grid)
        validate_grid({k: [v] for k, v in current.items()})
    except ProtectedParameterError as e:
        print(f"Refusé : {e}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    tf = Timeframe(args.timeframe)
    start = datetime(2018, 1, 1, tzinfo=timezone.utc)
    end = start.replace(year=start.year + args.years)
    bars, labels = generate_bars(start, end, tf, seed=args.seed)
    pit = InMemoryPointInTimeSource(synthetic_bias(labels))
    print(f"Données de prix : SYNTHÉTIQUES (graine {args.seed}), {len(bars)} bougies {tf.value}. Ce ne sont pas des résultats de marché.")
    asof = datetime.fromisoformat(args.asof).replace(tzinfo=timezone.utc) if args.asof else end
    if args.runtime:
        journal = from_runtime(args.runtime)
        print(f"Journal : {args.runtime} ({len(journal.entries)} décisions, {len(journal.trades)} trades)")
        if args.asof is None:
            asof = datetime.now(timezone.utc)
    else:
        config = BacktestConfig(settings=settings, max_margin_fraction=0.5)
        res = Backtester(config, pit).run(decision_factory(settings, current)({}), [b for b in bars if b.time < asof])
        journal = from_backtest(res)
        print(f"Journal : backtest des paramètres actuels ({len(journal.entries)} décisions, {len(journal.trades)} trades)")
    result = run_cycle(
        journal, asof, settings, bars=bars, pit=pit, current=current, grid=None if args.no_reevaluation else grid,
        registry=registry, out_dir=out_dir, min_n=args.min_n,
        train_months=args.train_months, test_months=args.test_months, holdout_months=args.holdout_months,
    )
    print(result.to_markdown())
    print(f"\nRapport : {result.report_path}")
    if result.proposal is not None:
        print(f"Proposition enregistrée dans {registry.path} : {result.proposal.status}. Rien n'a été appliqué.")
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
    dc.add_argument("--publish", help="publie les décisions pour le dashboard (ex. data/runtime/decisions.jsonl)")
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
    pp = sub.add_parser("paper", help="paper trading local (ordres simulés)")
    psub = pp.add_subparsers(dest="paper_cmd", required=True)
    pr = psub.add_parser("run", help="boucle de paper trading")
    pr.add_argument("--once", action="store_true", help="un seul cycle (planificateur externe)")
    pr.add_argument("--interval", type=float, default=60.0, help="secondes entre deux cycles")
    pr.add_argument("--timeframe", default="H1", choices=["M15", "H1"])
    pr.add_argument("--risk", type=float, default=0.10, help="fraction de risque de base (R9)")
    pr.add_argument("--warmup-days", type=int, default=400)
    pr.add_argument("--sources", nargs="+", default=["capital"], help="datasets de bougies lus dans le store")
    pr.add_argument("--refresh-capital", action="store_true", help="collecte Capital.com (lecture seule) avant chaque cycle")
    pr.add_argument("--store-dir", default="data/store")
    pr.add_argument("--env", default=".env")
    psub.add_parser("status", help="compte, positions, kill switch")
    prs = psub.add_parser("reset", help="archive le compte paper et repart de zéro")
    prs.add_argument("--confirm", default="")
    pp.add_argument("--runtime-dir", default="data/runtime")
    im = sub.add_parser("improve", help="cycle d'amélioration : journal, post-trade, walk-forward (propose seulement)")
    im.add_argument("--runtime", help="dossier du paper trading (decisions.jsonl, trades.jsonl) ; sinon backtest synthétique")
    im.add_argument("--asof", help="date de connaissance (ISO, UTC) ; défaut : fin des données")
    im.add_argument("--timeframe", default="H1", choices=["M15", "H1", "H4"])
    im.add_argument("--years", type=int, default=5)
    im.add_argument("--seed", type=int, default=1)
    im.add_argument("--current", action="append", help="paramètre actuel, ex. t.entry_mode=half")
    im.add_argument("--grid", action="append", help="grille, ex. t.entry_mode=touch,half (répétable)")
    im.add_argument("--no-reevaluation", action="store_true", help="journal et post-trade seulement")
    im.add_argument("--train-months", type=int, default=24)
    im.add_argument("--test-months", type=int, default=6)
    im.add_argument("--holdout-months", type=int, default=12)
    im.add_argument("--min-n", type=int, default=10, help="trades minimum pour commenter un groupe")
    im.add_argument("--out-dir", default="data/runtime")
    im.add_argument("--review", help="identifiant de la proposition à revoir")
    im.add_argument("--reviewer", help="nom du relecteur humain")
    rv = im.add_mutually_exclusive_group()
    rv.add_argument("--approve", action="store_true")
    rv.add_argument("--reject", action="store_true")
    im.add_argument("--note", default="")
    args = parser.parse_args(argv)
    if args.cmd == "paper":
        return cmd_paper(args)
    if args.cmd == "backtest":
        return cmd_backtest(args)
    if args.cmd == "decisions":
        return cmd_decisions(args)
    if args.cmd == "dashboard":
        return cmd_dashboard(args)
    if args.cmd == "improve":
        return cmd_improve(args)
    settings = Settings.from_env()
    if args.cmd == "kill-switch":
        return cmd_kill_switch(args, settings)
    if args.cmd == "status":
        return cmd_status(settings)
    return cmd_replay(args.csv)


if __name__ == "__main__":
    sys.exit(main())
