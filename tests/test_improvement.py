"""Boucles d'amélioration : journal causal, analyse post-trade, ré-évaluation qui propose sans appliquer."""

from __future__ import annotations

import json
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone

import pytest

from bot_trading.__main__ import main
from bot_trading.backtest.engine import Backtester, BacktestConfig
from bot_trading.config import RiskLimits, Settings, TradingMode
from bot_trading.core.decision import Action
from bot_trading.core.models import Direction, Position, Timeframe
from bot_trading.dashboard.runtime import append_jsonl
from bot_trading.improvement import (
    DecisionJournal,
    ExperimentRegistry,
    JournalEntry,
    ProtectedParameterError,
    TradeOutcome,
    analyze,
    attach_forward_moves,
    from_backtest,
    from_runtime,
    reevaluate,
    run_cycle,
    validate_grid,
)
from bot_trading.improvement.reevaluation import PENDING, REJECTED
from bot_trading.marketdata.source import InMemoryPointInTimeSource
from bot_trading.marketdata.synthetic import generate_bars, synthetic_bias
from bot_trading.strategy import DecisionStrategy, ExitParams, FundamentalView, PositionManager, TechnicalEngine

UTC = timezone.utc
START = datetime(2018, 1, 1, tzinfo=UTC)
SETTINGS = Settings(mode=TradingMode.BACKTEST)
CONFIG = BacktestConfig(settings=SETTINGS, max_margin_fraction=0.5)


@pytest.fixture(scope="module")
def market():
    bars, labels = generate_bars(START, START.replace(year=2020, month=7), Timeframe.H1, seed=6)
    return bars, InMemoryPointInTimeSource(synthetic_bias(labels))


@pytest.fixture(scope="module")
def backtest(market):
    bars, pit = market
    return Backtester(CONFIG, pit).run(DecisionStrategy(SETTINGS), bars)


# ------------------------------------------------------------------ journal


def test_journal_links_every_trade_to_its_decision(backtest):
    j = from_backtest(backtest)
    assert backtest.trades
    assert len(j.trades) == len(backtest.trades)
    for e in j.trades:
        assert e.action == "LONG" and e.outcome == "FILLED"
        assert e.time <= e.trade.opened_at  # décision avant exécution
        assert e.stage == "LONG"
    assert {e.stage for e in j.no_trades} - {""}, "les NO_TRADE gardent leur étage de refus"


def test_journal_as_of_hides_unknown_outcomes(backtest):
    j = from_backtest(backtest)
    first = j.trades[0].trade
    mid = first.opened_at + (first.closed_at - first.opened_at) / 2
    view = j.as_of(mid)
    assert all(e.time <= mid for e in view.entries)
    assert all(e.trade is None or e.trade.closed_at <= mid for e in view.entries)
    assert not any(e.trade is not None and e.trade.deal_id == first.deal_id for e in view.entries)


def test_forward_moves_are_prefix_invariant(market, backtest):
    bars, _ = market
    j = from_backtest(backtest)
    full = attach_forward_moves(j, bars, timedelta(days=3))
    cut = bars[len(bars) // 2].time
    part = attach_forward_moves(j, [b for b in bars if b.time < cut], timedelta(days=3))
    known_full = {e.time: e.forward_move for e in full.as_of(cut).entries if e.forward_move is not None}
    known_part = {e.time: e.forward_move for e in part.entries if e.forward_move is not None and e.time <= cut}
    assert known_full and known_full == known_part
    assert all(e.forward_known_at <= cut for e in full.as_of(cut).entries if e.forward_move is not None)


def test_from_runtime_links_by_decided_at_then_by_time(tmp_path):
    t0 = datetime(2026, 11, 2, 10, tzinfo=UTC)
    for i, (action, stage) in enumerate([("NO_TRADE", "setup"), ("LONG", "LONG"), ("NO_TRADE", "H4"), ("LONG", "LONG")]):
        append_jsonl(tmp_path / "decisions.jsonl", {"time": t0 + timedelta(hours=i), "action": action, "stage": stage, "outcome": "FILLED" if action == "LONG" else action, "setup": "FVG_H1", "regime": "RISK_ON", "quality": 0.7})
    (tmp_path / "decisions.jsonl").open("a").write("{corrompu\n")
    append_jsonl(tmp_path / "trades.jsonl", {"deal_id": "A", "opened_at": t0 + timedelta(hours=1, minutes=1), "closed_at": t0 + timedelta(hours=5), "net_pnl_eur": 1.0, "initial_risk_eur": 0.5, "exit_reason": "TAKE_PROFIT", "decided_at": t0 + timedelta(hours=1)})
    append_jsonl(tmp_path / "trades.jsonl", {"deal_id": "B", "opened_at": t0 + timedelta(hours=3, minutes=1), "closed_at": t0 + timedelta(hours=9), "gross_pnl_eur": -0.4, "funding_eur": 0.1, "exit_reason": "STOP", "r_multiple": -1.0})
    j = from_runtime(tmp_path)
    assert [e.trade.deal_id for e in j.trades] == ["A", "B"]
    a = j.trades[0]
    assert a.trade.r_multiple == pytest.approx(2.0) and a.quality == 0.7
    assert j.trades[1].trade.net_pnl_eur == pytest.approx(-0.5)
    assert len(j.no_trades) == 2


# ------------------------------------------------------------------ post-trade


def _entry(t, r, setup="FVG_H1", regime="RISK_ON", quality=0.7, funding=0.0, reason=None, hours=30):
    risk = 1.0
    tr = TradeOutcome(f"D{t:%j%H}", t, t + timedelta(hours=hours), 5000, 5000, r * risk, r, reason or ("TAKE_PROFIT" if r > 0 else "STOP"), funding, 0.0, r * risk + funding, risk)
    return JournalEntry(t, "LONG", "LONG", "FILLED", setup=setup, regime=regime, quality=quality, room_r=2.0, trade=tr)


def test_post_trade_flags_a_significantly_losing_setup():
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    entries = [_entry(t0 + timedelta(days=i), 1.5 if i % 2 else 0.8) for i in range(20)]
    entries += [_entry(t0 + timedelta(days=30 + i), -1.0 if i % 5 else 0.2, setup="OB_H4", regime="NEUTRE") for i in range(15)]
    rep = analyze(DecisionJournal(entries), min_n=10)
    ob = next(g for g in rep.groups["setup"] if g.key == "OB_H4")
    assert ob.n == 15 and ob.significant_sign == -1
    assert any(f.topic == "setup" and "OB_H4" in f.text for f in rep.findings)
    assert not any("FVG_H1" in f.text and "sous zéro" in f.text for f in rep.findings)
    assert "Analyse post-trade" in rep.to_markdown()


def test_post_trade_small_sample_and_costs():
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    entries = [_entry(t0 + timedelta(days=i), 0.5, funding=0.4) for i in range(4)]
    rep = analyze(DecisionJournal(entries), min_n=10)
    topics = {f.topic for f in rep.findings}
    assert "échantillon" in topics and "coûts" in topics


def test_post_trade_as_of_equals_truncated_journal():
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    entries = [_entry(t0 + timedelta(days=i), (-1) ** i * 1.2) for i in range(30)]
    asof = t0 + timedelta(days=15)
    a = analyze(DecisionJournal(entries), asof=asof)
    known = [e for e in entries if e.trade.closed_at <= asof]
    b = analyze(DecisionJournal(known), asof=asof)
    assert a.overall == b.overall


# ------------------------------------------------------------------ ré-évaluation


@pytest.mark.parametrize("name", [f.name for f in fields(RiskLimits)] + ["tiers", "max_margin_fraction"])
def test_risk_limits_can_never_be_searched(name):
    with pytest.raises(ProtectedParameterError):
        validate_grid({name: [1]})
    with pytest.raises(ProtectedParameterError):
        validate_grid({f"d.{name}": [1]})


def test_grid_values_must_stay_in_their_range():
    validate_grid({"t.entry_mode": ["touch", "bos"], "d.base_risk_fraction": [0.05, 0.2]})
    with pytest.raises(ValueError):
        validate_grid({"d.base_risk_fraction": [0.25]})  # R9 : jamais au-delà de 20 %
    with pytest.raises(ValueError):
        validate_grid({"t.entry_mode": ["magic"]})
    with pytest.raises(ValueError):
        validate_grid({"t.atr_period": [10]})  # hors espace déclaré


WF = dict(train_months=12, test_months=6, holdout_months=3, warmup_bars=200)


@pytest.fixture(scope="module")
def proposal(market):
    bars, pit = market
    asof = datetime(2020, 4, 1, tzinfo=UTC)
    return reevaluate({}, {"t.entry_mode": ["half", "touch"]}, bars, asof, SETTINGS, pit, **WF), asof


def test_reevaluation_proposes_and_never_applies(proposal):
    p, asof = proposal
    assert p.applied is False and p.to_experiment()["applied"] is False
    assert p.status in (PENDING, REJECTED)
    assert p.current == {"t.entry_mode": "half"}  # défaut du code
    assert p.proposed["t.entry_mode"] in ("half", "touch")
    assert p.holdout is not None and p.holdout[1] == asof
    assert p.n_trials == 2 * (p.folds + 1)
    assert SETTINGS.risk == RiskLimits()  # aucune limite modifiée
    if p.status == REJECTED:
        assert any(r.startswith("échec") for r in p.reasons)


def test_reevaluation_ignores_data_after_asof(market, proposal):
    bars, pit = market
    p, asof = proposal
    truncated = [b for b in bars if b.time < asof]
    q = reevaluate({}, {"t.entry_mode": ["half", "touch"]}, truncated, asof, SETTINGS, pit, **WF)
    assert q.baseline_oos == p.baseline_oos
    assert q.candidate_oos == p.candidate_oos
    assert q.proposed == p.proposed


def test_cycle_writes_report_and_registry(tmp_path, market, backtest):
    bars, pit = market
    reg = ExperimentRegistry(tmp_path / "experiments.jsonl")
    asof = datetime(2020, 4, 1, tzinfo=UTC)
    res = run_cycle(from_backtest(backtest), asof, SETTINGS, bars=bars, pit=pit, grid={"t.entry_mode": ["half"]}, registry=reg, out_dir=tmp_path, min_n=3, **WF)
    assert res.report_path is not None and res.report_path.exists()
    assert "Ré-évaluation walk-forward" in res.report_path.read_text(encoding="utf-8")
    recs = reg.records()
    assert len(recs) == 1 and recs[0]["applied"] is False and recs[0]["loop"] == "ré-évaluation walk-forward"
    assert reg.total_trials() == res.proposal.n_trials
    assert res.report.asof == asof


def test_registry_review_needs_a_human_and_a_known_proposal(tmp_path):
    reg = ExperimentRegistry(tmp_path / "experiments.jsonl")
    append_jsonl(reg.path, {"id": "WF-X", "n_trials": 4, "status": PENDING})
    append_jsonl(reg.path, {"id": "DEMO-1", "n_trials": 9, "demo": True})
    assert reg.total_trials() == 4
    with pytest.raises(ValueError):
        reg.review("WF-X", "  ", approve=True)
    with pytest.raises(KeyError):
        reg.review("WF-Y", "Alexandre", approve=True)
    rec = reg.review("WF-X", "Alexandre", approve=True)
    assert rec["applied"] is False and "manuellement" in rec["status"]
    assert len(reg.records()) == 3  # ajout seul


def test_cli_refuses_risk_limits_and_records_reviews(tmp_path, capsys):
    assert main(["improve", "--grid", "max_account_drawdown=0.6", "--out-dir", str(tmp_path)]) == 2
    assert "limite de risque" in capsys.readouterr().err
    append_jsonl(tmp_path / "experiments.jsonl", {"id": "WF-Z", "n_trials": 1})
    assert main(["improve", "--review", "WF-Z", "--reviewer", "Alexandre", "--reject", "--out-dir", str(tmp_path)]) == 0
    last = json.loads((tmp_path / "experiments.jsonl").read_text().splitlines()[-1])
    assert last["review_of"] == "WF-Z" and last["status"].startswith("refusé")


# ------------------------------------------------------------------ régime (choix validés)

T0 = datetime(2026, 11, 3, 10, tzinfo=UTC)


def _manage(fund, bid=5010.0):
    pos = Position("SIM-1", "US500", Direction.BUY, 0.05, 5000.0, T0, 4980.0, True, 10.0)
    return PositionManager(SETTINGS, ExitParams()).manage(pos, bid, T0 + timedelta(hours=5), 20.0, fund, TechnicalEngine(base=Timeframe.H1))


def test_unfavourable_regime_tightens_without_forced_close():
    # Étiquette de régime seule : le biais déduit ne force pas la sortie (Q2 validée).
    d = _manage(FundamentalView.from_labels("RISK_OFF"))
    assert d.action == Action.HOLD and d.stop_level is not None and d.stop_level > 4980.0
    # Un biais baissier explicite reste une invalidation fondamentale.
    assert _manage(FundamentalView.from_labels("RISK_OFF", "bearish")).action == Action.CLOSE


def test_unfavourable_regime_blocks_entries_and_reduces_size():
    off = FundamentalView.from_labels("RISK_OFF")
    neutral = FundamentalView.from_labels("NEUTRE")
    on = FundamentalView.from_labels("RISK_ON")
    assert not off.allow_new_entries and off.size_multiplier == 0.0
    assert neutral.allow_new_entries and neutral.size_multiplier == 0.5 * on.size_multiplier
    assert neutral.min_setup_quality > on.min_setup_quality
    assert replace(ExitParams()).on_risk_off == "tighten"
