"""Jeu de données de **démonstration** pour le dashboard (synthétique, hors marché).

``python -m bot_trading dashboard --demo`` remplit ``data/demo/`` avec :

- des bougies M5 synthétiques (``marketdata.synthetic``) dans un store Parquet ;
- un backtest de la stratégie complète (biais synthétique + FVG) sur ces bougies,
  converti au format du dossier d'exécution (compte, equity, trades, décisions) ;
- un ``RegimeAssessment`` d'exemple, quelques essais d'amélioration et des battements.

Tout est marqué ``"demo": true`` et le dashboard affiche un bandeau. Ces chiffres
ne disent rien de la stratégie réelle.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot_trading.config import Settings, TradingMode
from bot_trading.core.decision import Action
from bot_trading.core.models import Timeframe
from bot_trading.dashboard.decision_feed import publish_decision
from bot_trading.dashboard.runtime import append_jsonl, write_json

DEMO_ROOT = Path("data/demo")
DEMO_SOURCE = "synthetic"


def _floor(t: datetime, minutes: int) -> datetime:
    return t.replace(second=0, microsecond=0, minute=t.minute - t.minute % minutes)


def demo_regime(now: datetime) -> dict:
    from bot_trading.intelligence.schemas import (
        Bias,
        EquityAssessment,
        EventWindow,
        Invalidation,
        MacroAssessment,
        Regime,
        RegimeAssessment,
        Scenario,
        SourceRef,
        SubScore,
    )

    src = SourceRef(source="DÉMO", identifier="synthétique", available_at=now - timedelta(hours=1), note="donnée fictive")
    scen = [
        Scenario(name="central", probability=0.6, description="Désinflation lente, Fed patiente", implication="Tendance haussière modérée"),
        Scenario(name="haussier", probability=0.25, description="Baisse de taux anticipée", implication="Accélération"),
        Scenario(name="baissier", probability=0.15, description="Reprise de l'inflation", implication="Correction"),
    ]
    inval = [Invalidation(metric="VIX", operator=">", threshold=28.0, description="Stress de volatilité")]
    event = EventWindow(
        kind="CPI", label="CPI (exemple)", scheduled_at=now + timedelta(days=2),
        blackout_start=now + timedelta(days=2, minutes=-30), blackout_end=now + timedelta(days=2, minutes=30),
    )
    macro = MacroAssessment(
        asof=now, score=0.35, bias=Bias.BULLISH, uncertainty=0.4,
        subscores=[
            SubScore(component="F1_inflation", score=0.3, weight=0.25, rationale="Core PCE en ralentissement (fictif)", sources=[src]),
            SubScore(component="F2_emploi", score=0.2, weight=0.2, rationale="Marché du travail qui se détend sans casser (fictif)"),
            SubScore(component="F3_fed", score=0.4, weight=0.25, rationale="Discours moins restrictif (fictif)"),
            SubScore(component="F5_taux", score=0.1, weight=0.15, rationale="Courbe 10a-2a positive (fictif)"),
        ],
        scenarios=scen, invalidations=inval, sources=[src], upcoming_events=[event],
    )
    equity = EquityAssessment(
        asof=now, score=0.25, bias=Bias.BULLISH, uncertainty=0.5,
        subscores=[SubScore(component="F7_resultats", score=0.25, weight=1.0, rationale="Guidance des grosses pondérations stable (fictif)")],
        scenarios=[], invalidations=[], sources=[src],
    )
    reg = RegimeAssessment(
        asof=now, score=0.3, bias=Bias.BULLISH, uncertainty=0.45,
        subscores=[
            SubScore(component="macro", score=0.35, weight=0.6, rationale="Agent macro"),
            SubScore(component="entreprises", score=0.25, weight=0.25, rationale="Agent entreprises"),
            SubScore(component="intermarchés", score=0.2, weight=0.15, rationale="VIX bas, crédit calme (fictif)", metrics={"VIX": 15.2}),
        ],
        scenarios=scen, invalidations=inval, sources=[src], warnings=["DÉMO : évaluation fictive"],
        regime=Regime.RISK_ON, previous_regime=Regime.NEUTRE, allow_new_entries=True, size_multiplier=0.8,
        min_setup_quality=0.5, event_blackout=False, macro=macro, equity=equity,
    )
    out = reg.model_dump(mode="json")
    out["demo"] = True
    return out


def build_demo(root: str | Path = DEMO_ROOT, days: int = 60, seed: int = 7, now: datetime | None = None) -> Path:
    """(Re)crée le jeu de démonstration. Renvoie ``root``."""
    from bot_trading.backtest.engine import Backtester, BacktestConfig
    from bot_trading.backtest.strategies import FundamentalFilter, FVGTechnical
    from bot_trading.marketdata.bars import bar_record
    from bot_trading.marketdata.common import Provenance
    from bot_trading.marketdata.engine import DataEngine
    from bot_trading.marketdata.source import Bar, InMemoryPointInTimeSource
    from bot_trading.marketdata.storage import ParquetStore
    from bot_trading.marketdata.candles import resample
    from bot_trading.marketdata.synthetic import generate_bars, synthetic_bias

    root = Path(root)
    runtime, store_dir = root / "runtime", root / "store"
    for d in (runtime, store_dir):
        if d.exists():
            shutil.rmtree(d)
    runtime.mkdir(parents=True)
    now = _floor(now or datetime.now(timezone.utc), 5)
    start = now - timedelta(days=days)

    bars, labels = generate_bars(start, now, Timeframe.M5, seed=seed, start_price=6700.0)
    settings = Settings(mode=TradingMode.BACKTEST)
    epic = settings.instrument.epic

    # Store Parquet (M5 seulement : les autres timeframes sont agrégés à l'affichage).
    engine = DataEngine(ParquetStore(store_dir))
    recs = []
    for b in bars:
        sp = b.spread or 0.6
        ask = type(b.bid)(b.bid.time, b.bid.open + sp, b.bid.high + sp, b.bid.low + sp, b.bid.close + sp)
        recs.append(bar_record(epic, Timeframe.M5, b.bid, ask))
    engine.ingest_candles(recs, Provenance(DEMO_SOURCE, f"candles.{DEMO_SOURCE}.{epic.lower()}.m5", "demo://synthetic", now, license_note="synthétique"), Timeframe.M5)

    # Backtest H1 de la stratégie complète.
    h1 = resample([b.bid for b in bars], Timeframe.H1)
    h1_bars = [Bar(c, Timeframe.H1, 0.6) for c in h1]
    pit = InMemoryPointInTimeSource(synthetic_bias(labels, lag=timedelta(days=2), every=timedelta(days=3)))
    cfg = BacktestConfig(settings=settings, default_risk_fraction=0.10)
    res = Backtester(cfg, pit).run(FundamentalFilter(FVGTechnical(settings)), h1_bars, trade_start=h1[0].time + timedelta(days=7))

    peak = max([v for _, v in res.equity_curve] + [res.initial_equity_eur])
    write_json(runtime / "account.json", {
        "equity_eur": res.final_equity_eur, "balance_eur": res.final_equity_eur, "peak_equity_eur": peak,
        "used_margin_eur": 0.0, "open_exposure_lots": 0.0, "updated_at": now, "mode": "paper", "demo": True,
    })
    step = max(1, len(res.equity_curve) // 600)
    for t, v in res.equity_curve[::step]:
        append_jsonl(runtime / "equity.jsonl", {"time": t, "equity_eur": v})
    for tr in res.trades:
        t = tr.trade
        append_jsonl(runtime / "trades.jsonl", {
            "deal_id": t.deal_id, "epic": t.epic, "direction": t.direction.value, "size": t.size,
            "entry_price": t.entry_price, "exit_price": t.exit_price, "opened_at": t.opened_at, "closed_at": t.closed_at,
            "exit_reason": t.exit_reason, "gross_pnl_eur": t.gross_pnl_eur, "funding_eur": t.funding_eur,
            "fees_eur": t.fees_eur, "net_pnl_eur": t.net_pnl_eur, "r_multiple": tr.r_multiple,
            "setup": tr.setup, "regime": tr.regime, "bias": tr.bias,
        })
    kept = [d for d in res.decisions if d.decision.action != Action.HOLD or d.outcome not in ("", "NONE")]
    for d in kept[-400:]:
        violations = tuple(d.outcome.split(":", 1)[1].replace(" ", "").split(",")) if d.outcome.startswith("VETO") else ()
        price = h1[d.index].close if d.index < len(h1) else None
        publish_decision(runtime / "decisions.jsonl", d.decision, d.time, outcome=d.outcome, violations=violations, price=price, timeframe="H1")
        if violations:
            append_jsonl(runtime / "risk_events.jsonl", {"time": d.time, "kind": "VETO", "violations": list(violations), "message": d.decision.reason})

    write_json(runtime / "regime.json", demo_regime(now))

    m = res.metrics()
    for i, (mode, tp) in enumerate([("touch", 2.0), ("half", 2.0), ("full_reject", 3.0)]):
        append_jsonl(runtime / "experiments.jsonl", {
            "time": now - timedelta(days=3 - i), "id": f"DEMO-{i + 1}", "loop": "paramètres FVG",
            "hypothesis": f"Entrée {mode}, take profit {tp} R", "params": {"mode": mode, "take_profit_r": tp},
            "in_sample": {"calmar": round(0.4 + 0.2 * i, 2), "trades": 30 + i},
            "out_of_sample": {"calmar": round(0.2 + 0.05 * i, 2), "trades": 8 + i},
            "status": ["rejeté", "en attente de revue", "en attente de revue"][i],
            "note": "DÉMO : chiffres fictifs", "demo": True,
        })
    append_jsonl(runtime / "experiments.jsonl", {
        "time": now, "id": "DEMO-BT", "loop": "backtest de référence", "hypothesis": "Stratégie complète (synthétique)",
        "in_sample": {"calmar": m.calmar, "trades": m.trades, "net_return": m.net_return, "max_drawdown": m.max_drawdown},
        "status": "information", "demo": True,
    })

    write_json(runtime / "heartbeat.json", {
        "paper_loop": {"time": now - timedelta(minutes=2), "status": "OK", "message": "DÉMO"},
        "data_engine": {"time": now - timedelta(minutes=7), "status": "OK", "message": "DÉMO"},
        "decision_engine": {"time": now - timedelta(minutes=40), "status": "OK", "message": "DÉMO : en retard volontairement"},
    })
    return root
