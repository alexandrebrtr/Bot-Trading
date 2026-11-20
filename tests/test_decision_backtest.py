"""Stratégie complète (Decision Engine) branchée dans le backtester, sur données synthétiques.

Ces tests vérifient le **comportement** (fondamental dominant, veto, stop jamais
retiré ni éloigné, causalité), pas la rentabilité : les données synthétiques ne
disent rien de la stratégie.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

import pytest

from bot_trading.backtest.engine import Backtester, BacktestConfig, PointInTimeView
from bot_trading.backtest.lookahead import check_future_independence, check_prefix_invariance
from bot_trading.config import Settings, TradingMode
from bot_trading.core import contract
from bot_trading.core.decision import Action
from bot_trading.core.models import Timeframe
from bot_trading.marketdata.source import InMemoryPointInTimeSource
from bot_trading.marketdata.synthetic import generate_bars, synthetic_bias
from bot_trading.strategy import DecisionStrategy, FundamentalView

UTC = timezone.utc
START = datetime(2018, 1, 1, tzinfo=UTC)
SETTINGS = Settings(mode=TradingMode.BACKTEST)
CONFIG = BacktestConfig(settings=SETTINGS, max_margin_fraction=0.5)


@pytest.fixture(scope="module")
def run():
    bars, labels = generate_bars(START, START.replace(year=2022), Timeframe.H1, seed=6)
    pit = InMemoryPointInTimeSource(synthetic_bias(labels))
    res = Backtester(CONFIG, pit).run(DecisionStrategy(SETTINGS), bars)
    return bars, pit, res


def test_full_strategy_trades_and_every_decision_is_complete(run):
    _, _, res = run
    assert res.trades, "la stratégie complète doit produire au moins un trade sur cette série"
    actions = {d.decision.action for d in res.decisions}
    assert {Action.NO_TRADE, Action.LONG, Action.HOLD} <= actions
    for rec in res.decisions:
        assert rec.decision.missing_fields() == [], (rec.time, rec.decision.action, rec.decision.missing_fields())


def test_no_long_without_bullish_fundamental_at_decision_time(run):
    _, pit, res = run
    longs = [d for d in res.decisions if d.decision.action == Action.LONG]
    assert longs
    for d in longs:
        view = FundamentalView.from_pit(PointInTimeView(pit, d.time))
        assert view.available and view.regime != "RISK_OFF" and view.bias != "bearish"


def test_every_trade_respects_risk_caps(run):
    _, _, res = run
    eurusd = SETTINGS.default_eurusd
    for t in res.trades:
        tr = t.trade
        margin = contract.margin_eur(tr.size, tr.entry_price, SETTINGS.instrument, eurusd, SETTINGS.risk.max_leverage)
        assert t.initial_risk_eur <= 0.5 * margin + 1e-6  # R8
        assert margin <= 0.5 * 25.0  # R19 (equity bornée par le scénario)
        assert tr.size >= 0.01 and tr.size <= 1.0
        if tr.exit_reason == "STOP":
            # Le stop garanti sort au niveau : la perte de prix ne dépasse jamais le risque initial.
            assert tr.gross_pnl_eur >= -t.initial_risk_eur - 1e-6


def test_stop_only_moves_up(run):
    _, _, res = run
    raised = [d for d in res.decisions if d.outcome == "STOP_RAISED"]
    assert raised, "au moins une remontée de stop attendue sur cette série"
    by_trade: dict[datetime, list[float]] = defaultdict(list)
    opens = sorted(t.trade.opened_at for t in res.trades)
    for d in raised:
        key = max(o for o in opens if o <= d.time)
        by_trade[key].append(d.decision.stop_level)
    for levels in by_trade.values():
        assert levels == sorted(levels) and len(set(levels)) == len(levels)


def test_long_executed_at_next_open_and_journal_has_stage(run):
    _, _, res = run
    filled = [d for d in res.decisions if d.decision.action == Action.LONG and d.outcome == "FILLED"]
    assert filled
    for d in filled:
        assert d.executed_at > d.time - timedelta(hours=1)
        assert d.decision.meta["stage"] == "LONG"
    stages = {d.decision.meta.get("stage") for d in res.decisions if d.decision.action == Action.NO_TRADE}
    assert {"fondamental", "D1"} <= stages


def test_decision_engine_has_no_lookahead():
    bars, labels = generate_bars(START, START + timedelta(days=150), Timeframe.H1, seed=6)
    pit = InMemoryPointInTimeSource(synthetic_bias(labels))
    cut = len(bars) * 2 // 3
    check_prefix_invariance(lambda: DecisionStrategy(SETTINGS), bars, cut, CONFIG, pit)
    alt, _ = generate_bars(bars[cut].time + timedelta(hours=1), bars[-1].time + timedelta(days=10), Timeframe.H1, seed=99, start_price=bars[cut].bid.close)
    check_future_independence(lambda: DecisionStrategy(SETTINGS), bars, alt, cut, CONFIG, pit)


def test_without_fundamental_data_the_engine_never_trades():
    bars, _ = generate_bars(START, START + timedelta(days=200), Timeframe.H1, seed=6)
    res = Backtester(CONFIG, None).run(DecisionStrategy(SETTINGS), bars)
    assert not res.trades
    assert all(d.decision.meta.get("stage") == "fondamental" for d in res.decisions)


def test_m15_base_uses_m15_confirmation():
    bars, labels = generate_bars(START, START + timedelta(days=120), Timeframe.M15, seed=5)
    pit = InMemoryPointInTimeSource(synthetic_bias(labels))
    strat = DecisionStrategy(SETTINGS)
    Backtester(CONFIG, pit).run(strat, bars)
    assert strat.tech.confirmation_tf == Timeframe.M15
    assert set(strat.tech.states) == {Timeframe.M15, Timeframe.H1, Timeframe.H4, Timeframe.D1}


def test_margin_cap_in_backtester_sizing():
    from bot_trading.core.decision import Decision

    class OneLong:
        name = "one_long"

        def on_bar(self, ctx):
            if ctx.index == 1:
                return Decision(Action.LONG, stop_level=ctx.candle.close - 20, risk_fraction=0.2)
            return Decision.hold() if ctx.in_position else Decision.no_trade()

    bars, _ = generate_bars(START + timedelta(days=1), START + timedelta(days=3), Timeframe.H1, seed=3)
    free = Backtester(BacktestConfig(settings=SETTINGS), None).run(OneLong(), bars)
    capped = Backtester(CONFIG, None).run(OneLong(), bars)
    # Sans plafond : la taille visée dépasse la marge disponible et le Risk Engine oppose son veto.
    assert not free.trades and any("INSUFFICIENT_MARGIN" in d.outcome for d in free.decisions)
    assert capped.trades
    m = contract.margin_eur(capped.trades[0].trade.size, capped.trades[0].trade.entry_price, SETTINGS.instrument, SETTINGS.default_eurusd, 20)
    assert m <= 10.0 + 1e-9


def test_cli_publishes_decisions_for_dashboard(tmp_path):
    from bot_trading.__main__ import main
    from bot_trading.dashboard.decision_feed import JsonlDecisionFeed
    from bot_trading.dashboard.runtime import RuntimeReader

    out = tmp_path / "decisions.jsonl"
    assert main(["decisions", "--years", "1", "--seed", "6", "--last", "1", "--publish", str(out)]) == 0
    feed = JsonlDecisionFeed(RuntimeReader(tmp_path))
    views = feed.latest(limit=0)
    assert views and {v.action for v in views} >= {"NO_TRADE"}
    assert not feed.reader.warnings
