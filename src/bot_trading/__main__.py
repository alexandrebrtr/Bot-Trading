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
import math
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
    from bot_trading.backtest.validation import compare, fixed_out_of_sample, metrics_table, walk_forward, walk_forward_folds
    from bot_trading.config import TradingMode
    from bot_trading.core.models import Timeframe
    from bot_trading.marketdata.source import CsvPriceSource, InMemoryPointInTimeSource
    from bot_trading.marketdata.synthetic import generate_bars, synthetic_bias
    from bot_trading.strategy import DecisionParams, DecisionStrategy

    settings = _risk_scenario(Settings(mode=TradingMode.BACKTEST), args)
    # R19 (marge ≤ 50 % de l'equity) appliqué à toutes les stratégies : benchmarks comparables.
    config = BacktestConfig(settings=settings, default_risk_fraction=args.risk, max_margin_fraction=0.5)
    tf = Timeframe(args.timeframe)
    pit = None
    holdout_note = None
    if args.store:
        bars, pit, holdout_note = _store_backtest_data(args, settings, tf)
        if not bars:
            return 1
    elif args.csv:
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

    studies = []
    if args.extension_grid:
        studies.append((_extension_variants(_extension_grid(args.extension_grid)), "Entrée tardive (T3)", "Extension max (ATR D1)"))
    if args.entry_study:
        studies.append((ENTRY_VARIANTS, "Entrées plus fréquentes (EXP-14)", "Variante"))
    if studies and pit is None:
        print("--extension-grid / --entry-study : biais fondamental requis (mode --store ou synthétique).", file=sys.stderr)
        return 1
    if args.walk_forward or studies:
        # Mode store : la réserve est déjà retirée de ``bars`` (jamais lue ici).
        hold_m = 0 if args.store else args.holdout_months
        folds, holdout = walk_forward_folds(bars[0].time, bars[-1].time, args.train_months, args.test_months, args.test_months, hold_m)
        if not folds:
            print("Walk-forward : historique trop court pour une fenêtre.")
            return 0
    for variants, title, column in studies:
        _variant_study(variants, title, column, settings, args, bars, folds, config, pit, fixed_out_of_sample, walk_forward)
    if args.walk_forward:
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
    if holdout_note:
        print(holdout_note)
    return 0


def _extension_grid(text: str) -> list[float]:
    """``"3,4,6,off"`` → [3.0, 4.0, 6.0, inf] (``off`` : filtre d'entrée tardive T3 désactivé)."""
    out = []
    for tok in (t.strip().lower() for t in text.split(",")):
        if tok:
            out.append(math.inf if tok in ("off", "inf", "aucun") else float(tok))
    return out


# EXP-14 : variantes d'entrée plus fréquentes (techniques seulement ; RiskLimits intactes).
ENTRY_VARIANTS: dict[str, dict] = {
    "actuel": {},
    "H4 non exigé": {"decision": {"require_h4_bullish": False}},
    "confirmation touch": {"technical": {"entry_mode": "touch"}},
    "H4 non exigé + touch": {"decision": {"require_h4_bullish": False}, "technical": {"entry_mode": "touch"}},
}


def _extension_variants(values) -> dict[str, dict]:
    return {(f"{v:g}" if math.isfinite(v) else "désactivé"): {"technical": {"max_extension_atr": v}} for v in values}


def _variant_study(variants, title, column, settings, args, bars, folds, config, pit, fixed_out_of_sample, walk_forward) -> None:
    """Chaque variante sur les mêmes fenêtres de test (sans sélection), buy & hold en repère, puis sélection walk-forward."""
    from bot_trading.backtest.strategies import BuyAndHold
    from bot_trading.strategy import DecisionParams, DecisionStrategy, TechnicalParams

    def factory(p):
        v = variants[p["variante"]]
        return DecisionStrategy(
            settings,
            technical=TechnicalParams(**v.get("technical", {})),
            decision=DecisionParams(base_risk_fraction=args.risk, **v.get("decision", {})),
        )

    def fmt(v, nd=2):
        return "—" if v is None else f"{v:+.{nd}f}" if isinstance(v, float) else str(v)

    tf = bars[0].timeframe.value if bars else "?"
    print(f"\n{title} ({tf}) : {len(variants)} variantes × {len(folds)} fenêtres de test "
          f"({folds[0].test_start.date()} → {folds[-1].test_end.date()}), mêmes coûts et même Risk Engine.")
    print(f"| {column} | Trades OOS | Espérance R | Profit factor | P&L net € | Fenêtres > 0 | Pire DD |")
    print("|---|---|---|---|---|---|---|")
    rows = [(name, fixed_out_of_sample(factory, {"variante": name}, bars, folds, config, pit)) for name in variants]
    rows.append(("Buy & hold (repère)", fixed_out_of_sample(lambda p: BuyAndHold(settings), {}, bars, folds, config, pit)))
    for name, rep in rows:
        s = rep.summary()
        dd = s["oos_worst_drawdown"]
        pf = "—" if s["oos_profit_factor"] is None else format(s["oos_profit_factor"], ".2f")
        print(f"| {name} | {s['oos_trades']} | {fmt(s['oos_expectancy_r'])} | {pf} | "
              f"{s['oos_net_pnl_eur']:+.2f} | {s['oos_positive_folds']}/{s['folds']} | {'—' if dd is None else f'{dd:.1%}'} |")
    if args.no_selection:
        print(f"Sélection walk-forward non lancée (--no-selection). Essais à reporter dans EXPERIMENTS.md : {len(variants) * len(folds)}.")
        return
    rep = walk_forward(factory, {"variante": list(variants)}, bars, folds, config, pit)
    s = rep.summary()
    print(f"Sélection walk-forward (variante choisie sur chaque fenêtre d'entraînement) : {s['oos_trades']} trades OOS, "
          f"espérance {fmt(s['oos_expectancy_r'])} R, P&L {s['oos_net_pnl_eur']:+.2f} €, "
          f"choix {[p['variante'] for p in s['params_by_fold']]}")
    print(f"Essais à reporter dans EXPERIMENTS.md : {len(variants) * len(folds) * 2} (variantes fixes + sélection).")


def _store_backtest_data(args, settings, tf):
    """Bougies réelles du store (avant la réserve finale) et régime point-in-time recalculé jour par jour."""
    from dataclasses import replace
    from datetime import datetime, timedelta, timezone

    from bot_trading.backtest.validation import add_months
    from bot_trading.marketdata.pit import regime_history, regime_observations
    from bot_trading.marketdata.source import InMemoryPointInTimeSource
    from bot_trading.marketdata.storage import ParquetStore
    from bot_trading.paper.feeds import StoreBarFeed
    from bot_trading.paper.regime import data_coverage

    store = ParquetStore(args.store)
    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc) if args.start else None
    feed = StoreBarFeed(store, settings.instrument.epic, tf, [s.strip() for s in args.source.split(",") if s.strip()])
    bars = feed.bars(start, datetime.now(timezone.utc))
    if not bars:
        print(f"Aucune bougie {tf.value} dans {args.store} pour {args.source}.", file=sys.stderr)
        return [], None, None
    last_close = bars[-1].time + timedelta(minutes=tf.minutes)
    holdout_start = add_months(last_close, -args.holdout_months) if args.holdout_months else last_close
    kept = [b for b in bars if b.time < holdout_start]
    floor = settings.costs.default_spread_points
    if args.spread == "capital":
        # Coût Capital.com : jamais moins que le spread par défaut du broker simulé.
        kept = [replace(b, spread=max(floor, b.spread or 0.0)) for b in kept]
    print(f"Données : RÉELLES, {feed.last_source}, {len(kept)} bougies du {kept[0].time:%Y-%m-%d} au {holdout_start:%Y-%m-%d} "
          f"(spread {'max(source, ' + str(floor) + ' pts)' if args.spread == 'capital' else 'de la source'}).")
    note = (f"Réserve finale non ouverte : {holdout_start:%Y-%m-%d} → {last_close:%Y-%m-%d} "
            f"({len(bars) - len(kept)} bougies jamais lues par le backtest)") if args.holdout_months else None
    if args.no_fundamental:
        print("Fondamental désactivé (--no-fundamental) : stratégies fondamentales non évaluées.")
        return kept, None, note
    n = [0]

    def progress(t, a):
        n[0] += 1
        if n[0] % 250 == 0:
            print(f"  régime {t:%Y-%m-%d} : {a.regime.value}", flush=True)

    print("Régime point-in-time : une évaluation par jour à 21:00 UTC (peut prendre quelques minutes)…", flush=True)
    macro_config = None
    if args.inflation_scoring == "niveau":
        from bot_trading.intelligence.macro import MacroConfig

        macro_config = MacroConfig.level_first()
    print(f"Notation de l'inflation : {args.inflation_scoring}"
          f"{' (utiliser un cache des régimes propre à cette notation)' if args.regime_cache else ''}")
    hist = regime_history(store, kept[0].time, holdout_start, cache_path=args.regime_cache, progress=progress, macro_config=macro_config)
    if hist:
        from collections import Counter

        cov = Counter(data_coverage(a) for a in hist)
        regs = Counter(a.regime.value for a in hist)
        allowed = sum(a.allow_new_entries for a in hist)
        print(f"  {len(hist)} évaluations : {dict(regs)} ; entrées autorisées {allowed / len(hist):.0%} des jours")
        print(f"  couverture (macro, intermarchés) la plus fréquente : {cov.most_common(1)[0][0]}")
    return kept, InMemoryPointInTimeSource(regime_observations(hist)), note


def _risk_scenario(settings, args):
    """Scénario de risque : backtest uniquement. La politique de risque (RiskLimits, paper trading) ne change pas."""
    overrides = {k: v for k, v in (("max_leverage", getattr(args, "leverage", None)),
                                   ("max_loss_fraction_of_margin", getattr(args, "max_margin_loss", None)),
                                   ("initial_capital_eur", getattr(args, "capital", None))) if v is not None}
    if not overrides:
        return settings
    from dataclasses import replace as _replace

    settings = _replace(settings, risk=_replace(settings.risk, **overrides))
    r = settings.risk
    print(f"SCÉNARIO DE RISQUE (backtest seulement, pas la politique) : levier {r.max_leverage:g}:1, "
          f"perte max {r.max_loss_fraction_of_margin:.0%} de la marge → stop max ≈ {r.max_loss_fraction_of_margin / r.max_leverage:.1%} du prix, "
          f"capital {r.initial_capital_eur:g} €")
    return settings


def cmd_decisions(args) -> int:
    """Journal de décision de la stratégie complète : synthétique, ou réel avec ``--store`` (motifs des NO_TRADE)."""
    import json
    from collections import Counter
    from datetime import datetime, timezone

    from bot_trading.backtest.engine import BacktestConfig, Backtester
    from bot_trading.config import TradingMode
    from bot_trading.core.models import Timeframe
    from bot_trading.marketdata.source import InMemoryPointInTimeSource
    from bot_trading.marketdata.synthetic import generate_bars, synthetic_bias
    from bot_trading.strategy import DecisionParams, DecisionStrategy

    settings = _risk_scenario(Settings(mode=TradingMode.BACKTEST), args)
    tf = Timeframe(args.timeframe)
    if args.store:
        bars, pit, note = _store_backtest_data(args, settings, tf)
        if not bars:
            return 1
    else:
        start = datetime(2018, 1, 1, tzinfo=timezone.utc)
        end = start.replace(year=start.year + args.years)
        bars, labels = generate_bars(start, end, tf, seed=args.seed)
        pit = InMemoryPointInTimeSource(synthetic_bias(labels))
        note = None
        print(f"Données SYNTHÉTIQUES (graine {args.seed}), {len(bars)} bougies {tf.value} : ce ne sont pas des résultats de marché.")
    config = BacktestConfig(settings=settings, max_margin_fraction=0.5)
    res = Backtester(config, pit).run(DecisionStrategy(settings, decision=DecisionParams(base_risk_fraction=args.risk)), bars)
    stages = Counter((r.decision.action.value, r.decision.meta.get("stage", "")) for r in res.decisions)
    total = sum(stages.values()) or 1
    print("\nDécisions par action et étage (un refus est compté à l'étage qui l'a bloqué) :")
    for (action, stage), n in stages.most_common():
        print(f"  {action:9} {stage:13} {n:>7}  {n / total:6.1%}")
    import re

    def _motif(reason: str) -> str:
        return re.sub(r"[-+]?\d+(?:[.,]\d+)?", "#", reason)[:90]

    refusals = Counter((r.decision.meta.get("stage", ""), _motif(r.decision.reason)) for r in res.decisions if r.decision.action.value == "NO_TRADE")
    print("\nMotifs de NO_TRADE les plus fréquents (chiffres remplacés par #) :")
    for (stage, motif), n in refusals.most_common(args.top):
        print(f"  {n:>7}  {stage:11} {motif}")
    vetoes = Counter(r.outcome for r in res.decisions if r.outcome.startswith("VETO"))
    if vetoes:
        print("\nVetos du Risk Engine :")
        for o, n in vetoes.most_common(args.top):
            print(f"  {n:>7}  {o[:100]}")
    if note:
        print(note)
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
    if args.paper_cmd == "compare":
        from datetime import datetime

        from bot_trading.paper import compare as cmp

        try:
            manual = cmp.load_manual(args.journal)
        except (OSError, RuntimeError) as e:
            print(e, file=sys.stderr)
            return 1
        since = datetime.fromisoformat(args.since) if args.since else None
        if since is not None and since.tzinfo is None:
            from datetime import timezone

            since = since.replace(tzinfo=timezone.utc)
        rep_ = cmp.compare(cmp.paper_trades(runtime), manual, start=since, window=timedelta(hours=args.window_hours))
        print(cmp.to_markdown(rep_))
        print(f"\nRapport : {cmp.write_report(runtime, rep_)}")
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
    from bot_trading.notify import notifier_from_env

    notifier = notifier_from_env()
    trader = PaperTrader(feed, settings, cfg, notifier=notifier)
    refresher = None
    if args.refresh_capital:
        from bot_trading.paper.feeds import CapitalRefresher

        refresher = CapitalRefresher(DataEngine(store), settings.instrument.epic, (tf,))
    publisher = None
    if not args.no_regime:
        from bot_trading.paper.regime import RegimePublisher

        publisher = RegimePublisher(store, runtime, minutes=tf.minutes)
    last_regime: list[str] = []

    def before_cycle(now) -> None:
        try:
            if refresher is not None:
                refresher.refresh(now)
        finally:
            publish(now)

    def publish(now) -> None:
        if publisher is not None:
            a = publisher.maybe_publish(now)
            if a is not None:
                label = f"{a.regime.value} (score {a.score:+.2f}, entrées {'autorisées' if a.allow_new_entries else 'bloquées'})"
                print(f"Régime recalculé à {a.asof:%Y-%m-%d %H:%M} UTC : {label}")
                if last_regime and last_regime[-1] != a.regime.value:
                    notifier("RÉGIME", f"Changement de régime : {last_regime[-1]} → {label}")
                last_regime.append(a.regime.value)

    def report(r) -> None:
        print(f"[{r.time:%Y-%m-%d %H:%M:%S} UTC] {r.message}")
        for d in r.decisions:
            print(f"    {d.time:%Y-%m-%d %H:%M} {d.decision.action.value:8} {d.outcome[:28]:28} {d.decision.reason[:100]}")

    print("Paper trading : ordres SIMULÉS uniquement (aucun ordre réel n'existe dans ce dépôt).")
    print(f"Alertes Telegram : {'activées' if notifier.enabled else 'désactivées (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID absents)'}")
    try:
        trader.run(
            interval_seconds=args.interval,
            max_cycles=1 if args.once else None,
            before_cycle=before_cycle,
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


def cmd_regime(args) -> int:
    """Market Regime Engine sur les données collectées ; ``--publish`` écrit regime.json pour le paper trading."""
    from datetime import datetime, timezone

    from bot_trading.marketdata.pit import assess_regime, floor_time
    from bot_trading.marketdata.storage import ParquetStore
    from bot_trading.paper.regime import data_coverage, guard_coverage

    asof = datetime.fromisoformat(args.asof) if args.asof else floor_time(datetime.now(timezone.utc), 60)
    if asof.tzinfo is None:
        asof = asof.replace(tzinfo=timezone.utc)
    a = guard_coverage(assess_regime(ParquetStore(args.store_dir), asof))
    macro, inter = data_coverage(a)
    print(f"Régime à {asof:%Y-%m-%d %H:%M} UTC : {a.regime.value}, score {a.score:+.2f}, incertitude {a.uncertainty:.2f}")
    print(f"Entrées {'autorisées' if a.allow_new_entries else 'bloquées'}, multiplicateur de taille {a.size_multiplier:g}, qualité min {a.min_setup_quality:g}")
    print(f"Couverture : {macro} composante(s) macro, {inter} intermarché(s)")
    for s_ in a.subscores:
        print(f"  {s_.component:16} {'—' if not s_.available else f'{s_.score:+.2f}'}  {s_.rationale[:110]}")
        detail = {"macro": a.macro.subscores, "entreprises": a.equity.subscores}.get(s_.component, [])
        for d in detail:
            print(f"    {d.component:14} {'—' if not d.available else f'{d.score:+.2f}'}  {d.rationale[:104]}")
    for o in a.overrides:
        print(f"  ! {o}")
    if args.publish:
        from pathlib import Path

        from bot_trading.dashboard.runtime import append_jsonl, write_json

        data = a.model_dump(mode="json")
        write_json(Path(args.runtime_dir) / "regime.json", data)
        append_jsonl(Path(args.runtime_dir) / "regime_history.jsonl", data)
        print(f"Publié : {Path(args.runtime_dir) / 'regime.json'}")
    return 0


def _store_args(p) -> None:
    """Options communes à backtest et decisions pour les données réelles du store."""
    p.add_argument("--store", help="données RÉELLES : dossier du store Parquet (ex. data/store)")
    p.add_argument("--source", default="dukascopy,capital", help="sources de bougies du store, par ordre de préférence")
    p.add_argument("--start", help="début (AAAA-MM-JJ), mode --store")
    p.add_argument("--spread", choices=["capital", "source"], default="capital", help="mode --store : coût Capital.com (défaut) ou spread de la source")
    p.add_argument("--regime-cache", help="mode --store : JSONL des régimes déjà calculés (réutilisés)")
    p.add_argument("--no-fundamental", action="store_true", help="mode --store : techniques et benchmarks seulement")
    p.add_argument("--leverage", type=float, help="scénario : levier (défaut de la politique : 10)")
    p.add_argument("--max-margin-loss", type=float, help="scénario : perte max par position en fraction de la marge (politique : 0.5)")
    p.add_argument("--capital", type=float, help="scénario : capital initial en € (politique : 20)")
    p.add_argument("--inflation-scoring", choices=["tendance", "niveau"], default="tendance",
                    help="mode --store : F1 noté surtout sur la tendance (défaut) ou sur le niveau (ancienne notation)")
    if not any(a.dest == 'holdout_months' for a in p._actions):
        p.add_argument("--holdout-months", type=int, default=12)


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
    bt.add_argument("--entry-study", action="store_true",
                    help="Decision Engine : compare hors échantillon des variantes d'entrée plus fréquentes (H4 non exigé, confirmation touch) ; avec --timeframe M15 pour confirmer en M15")
    bt.add_argument("--no-selection", action="store_true", help="études de variantes : tableau hors échantillon seulement (bien plus rapide, surtout en M15)")
    bt.add_argument("--extension-grid", help="Decision Engine : valeurs d'extension max D1 en ATR à comparer hors échantillon (ex. 3,4,6,off)")
    _store_args(bt)
    dc = sub.add_parser("decisions")
    dc.add_argument("--timeframe", default="H1", choices=["M5", "M15", "H1"])
    dc.add_argument("--years", type=int, default=2)
    dc.add_argument("--seed", type=int, default=2)
    dc.add_argument("--risk", type=float, default=0.10)
    dc.add_argument("--last", type=int, default=15)
    dc.add_argument("--out", help="écrit le journal complet en JSONL")
    dc.add_argument("--publish", help="publie les décisions pour le dashboard (ex. data/runtime/decisions.jsonl)")
    dc.add_argument("--top", type=int, default=15, help="nombre de motifs de refus affichés")
    _store_args(dc)
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
    pr.add_argument("--no-regime", action="store_true", help="ne pas recalculer regime.json depuis le store (publié par ailleurs)")
    pr.add_argument("--store-dir", default="data/store")
    pr.add_argument("--env", default=".env")
    psub.add_parser("status", help="compte, positions, kill switch")
    pc = psub.add_parser("compare", help="compare le paper trading aux trades manuels (journal .xlsx ou CSV)")
    pc.add_argument("--journal", required=True, help="TJ_*.xlsx (onglet Trades) ou CSV date,direction,entry_price,exit_price,pnl")
    pc.add_argument("--since", help="début de la période (ISO) ; défaut : premier trade paper")
    pc.add_argument("--window-hours", type=float, default=24.0)
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
    rg = sub.add_parser("regime", help="régime de marché calculé sur les données collectées")
    rg.add_argument("--asof", help="instant d'évaluation (ISO, UTC) ; défaut : dernière heure pleine")
    rg.add_argument("--store-dir", default="data/store")
    rg.add_argument("--runtime-dir", default="data/runtime")
    rg.add_argument("--publish", action="store_true", help="écrit regime.json et regime_history.jsonl")
    args = parser.parse_args(argv)
    if args.cmd == "regime":
        return cmd_regime(args)
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
