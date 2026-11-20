from __future__ import annotations

import math
import random
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from bot_trading.backtest.engine import Backtester, BacktestConfig
from bot_trading.backtest.lookahead import check_future_independence, check_prefix_invariance
from bot_trading.backtest.metrics import (
    daily_returns,
    deflated_sharpe,
    directional_accuracy,
    max_drawdown,
    probabilistic_sharpe,
    risk_of_ruin,
    sharpe_ratio,
)
from bot_trading.backtest.strategies import (
    BuyAndHold,
    FundamentalFilter,
    FundamentalOnly,
    FVGEntryParams,
    FVGTechnical,
    RandomEntry,
    SimpleTrendFollowing,
)
from bot_trading.backtest.validation import (
    HoldoutAlreadyUsed,
    HoldoutGuard,
    add_months,
    compare,
    metrics_table,
    run_holdout,
    run_stress,
    slice_bars,
    walk_forward,
    walk_forward_folds,
)
from bot_trading.core.decision import Action, Decision
from bot_trading.core.models import Candle, Timeframe
from bot_trading.marketdata.source import Bar, InMemoryPointInTimeSource, Observation
from bot_trading.marketdata.synthetic import generate_bars, synthetic_bias

UTC = timezone.utc


@pytest.fixture(scope="module")
def market():
    bars, labels = generate_bars(datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 5, 1, tzinfo=UTC), Timeframe.H1, seed=11)
    pit = InMemoryPointInTimeSource(synthetic_bias(labels))
    return bars, labels, pit


FACTORIES = {
    "bh": lambda: BuyAndHold(),
    "tf": lambda: SimpleTrendFollowing(sma_period=50),
    "fvg": lambda: FVGTechnical(params=FVGEntryParams(mode="touch")),
    "rnd": lambda: RandomEntry(probability=0.05, seed=3),
    "fund": lambda: FundamentalOnly(),
    "full": lambda: FundamentalFilter(FVGTechnical(params=FVGEntryParams(mode="touch"))),
}


# ------------------------------------------------------------------ look-ahead


@pytest.mark.parametrize("name", list(FACTORIES))
def test_benchmarks_have_no_lookahead(market, name):
    bars, _, pit = market
    cut = len(bars) // 2
    check_prefix_invariance(FACTORIES[name], bars, cut, pit=pit)
    alt, _ = generate_bars(bars[cut].time + timedelta(hours=1), bars[-1].time, Timeframe.H1, seed=99, start_price=bars[cut].bid.close * 1.2)
    check_future_independence(FACTORIES[name], bars, alt, cut, pit=pit)


def test_cheating_strategy_is_caught(market):
    bars, _, _ = market
    cut = len(bars) // 2
    step = timedelta(hours=1)

    class Cheat:
        """Précalcule « la clôture suivante » sur toute la série reçue : look-ahead classique."""

        name = "cheat"

        def __init__(self, data):
            self.next_close = {b.time: n.bid.close for b, n in zip(data, data[1:])}

        def on_bar(self, ctx):
            nxt = self.next_close.get(ctx.bar.time)
            if nxt is not None and nxt > ctx.candle.close + 20:
                return Decision(Action.LONG, stop_level=ctx.candle.close - 50)
            return Decision.no_trade()

    data = list(bars)
    c = data[cut].bid.close
    data[cut + 1] = replace(data[cut + 1], bid=Candle(data[cut + 1].time, c, c + 60, c - 1, c + 50))
    alt = [Bar(Candle(data[cut].time + step * (k + 1), c - 100, c - 99, c - 101, c - 100), Timeframe.H1, 0.6) for k in range(50)]
    with pytest.raises(AssertionError):
        check_future_independence(lambda d: Cheat(d), data, alt, cut)
    # La même vérification passe pour une stratégie causale construite de la même façon.
    check_future_independence(lambda d: FVGTechnical(), data, alt, cut)


def test_fundamental_filter_blocks_non_bullish(market):
    bars, _, _ = market
    pit = InMemoryPointInTimeSource([Observation("bias/swing", bars[0].time, "bearish", bars[0].time, "test")])
    res = Backtester(pit=pit).run(FundamentalFilter(FVGTechnical(params=FVGEntryParams(mode="touch"))), bars)
    assert not res.trades
    tech = Backtester().run(FVGTechnical(params=FVGEntryParams(mode="touch")), bars)
    assert tech.trades  # la technique seule aurait tradé : c'est bien le filtre qui bloque


def test_bias_is_only_seen_after_publication(market):
    bars, _, _ = market
    publish = bars[200].time + timedelta(minutes=30)
    pit = InMemoryPointInTimeSource([Observation("bias/swing", bars[0].time, "bullish", publish, "test")])
    res = Backtester(pit=pit).run(FundamentalOnly(), bars)
    first = min(t.trade.opened_at for t in res.trades)
    assert first > publish


# ------------------------------------------------------------------ métriques


def test_max_drawdown_and_returns():
    t0 = datetime(2026, 1, 5, tzinfo=UTC)
    curve = [(t0 + timedelta(days=i), e) for i, e in enumerate([100, 120, 90, 110, 130, 65])]
    mdd, days = max_drawdown(curve)
    assert mdd == pytest.approx(0.5)
    assert days == pytest.approx(2.0)  # sous l'eau du jour 1 au jour 3
    assert daily_returns(curve)[0] == pytest.approx(0.2)
    assert sharpe_ratio([0.01] * 10) is None


def test_psr_dsr_and_ruin():
    rng = random.Random(1)
    good = [rng.gauss(0.002, 0.01) for _ in range(500)]
    bad = [rng.gauss(-0.002, 0.01) for _ in range(500)]
    assert probabilistic_sharpe(good) > 0.9
    assert probabilistic_sharpe(bad) < 0.1
    assert deflated_sharpe(good, 100) < probabilistic_sharpe(good)
    assert risk_of_ruin([0.05, -0.02] * 10) < risk_of_ruin([0.02, -0.2] * 10)
    assert 0.0 <= risk_of_ruin([-0.2] * 10) <= 1.0


def test_metrics_complete(market):
    bars, labels, pit = market
    res = Backtester(pit=pit).run(FACTORIES["full"](), bars)
    m = res.metrics()
    d = m.to_dict()
    for key in ("net_pnl_eur", "max_drawdown", "sharpe", "sortino", "profit_factor", "expectancy_eur", "expectancy_r",
                "win_rate", "avg_win_eur", "avg_loss_eur", "risk_of_ruin", "funding_eur", "by_regime", "by_setup", "no_trade_share"):
        assert key in d
    assert m.net_pnl_eur == pytest.approx(res.final_equity_eur - res.initial_equity_eur)
    assert math.isclose(m.net_pnl_eur, sum(t.net_pnl_eur for t in res.trades), abs_tol=1e-6)
    acc = directional_accuracy(res, bars, horizon_bars=24)
    assert acc["forecasts"] > 0


def test_compare_and_table(market):
    bars, _, pit = market
    results = compare({k: FACTORIES[k] for k in ("bh", "fvg", "full")}, bars, pit=pit)
    table = metrics_table(results)
    assert table.count("\n") == 4
    assert "bh" in table


# ------------------------------------------------------------------ validation


def test_walk_forward_folds_layout():
    folds, holdout = walk_forward_folds(datetime(2018, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC))
    assert holdout == (datetime(2025, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC))
    assert len(folds) == 10
    for f in folds:
        assert f.train_end == f.test_start
        assert f.test_end <= holdout[0]
    assert add_months(datetime(2024, 1, 31, tzinfo=UTC), 1).day == 29


def test_slice_bars_warmup_is_strictly_past(market):
    bars, _, _ = market
    start = bars[500].time
    data, ts = slice_bars(bars, start, bars[800].time, warmup_bars=100)
    assert data[0].time == bars[400].time
    assert all(b.time < bars[800].time for b in data)
    assert ts == start


def test_walk_forward_runs_and_counts_trials(market):
    bars, _, pit = market
    folds, _ = walk_forward_folds(bars[0].time, bars[-1].time, train_months=2, test_months=1, step_months=1, holdout_months=0)
    assert folds
    rep = walk_forward(
        lambda p: FVGTechnical(params=FVGEntryParams(**p)),
        {"mode": ["touch", "half"], "take_profit_r": [1.5, 2.0]},
        bars, folds, pit=pit, warmup_bars=50,
        objective=lambda m: m.expectancy_r if m.expectancy_r is not None else -math.inf,
    )
    assert rep.n_trials == 4 * len(folds)
    for fr in rep.folds:
        assert all(t.trade.opened_at >= fr.fold.test_start for t in fr.oos_result.trades)
        assert fr.oos_result.trade_start == fr.fold.test_start
    s = rep.summary()
    assert s["folds"] == len(folds)


def test_holdout_opens_once(tmp_path, market):
    bars, _, _ = market
    j = tmp_path / "holdout.json"
    g = HoldoutGuard(j)
    window = (bars[-300].time, bars[-1].time)
    run_holdout(g, lambda p: BuyAndHold(), {}, bars, window, "test", warmup_bars=10)
    with pytest.raises(HoldoutAlreadyUsed):
        g.open("encore")
    with pytest.raises(HoldoutAlreadyUsed):
        HoldoutGuard(j).open("nouveau processus")


def test_stress_costs_hurt(market):
    bars, _, _ = market
    out = run_stress(lambda p: BuyAndHold(), {}, bars)
    assert out["financement_x2"].funding_eur > out["base"].funding_eur
    assert out["spread_x2"].net_pnl_eur <= out["base"].net_pnl_eur + 1e-9
    assert set(out) >= {"base", "spread_x2", "spread_x3", "prime_gsl_1pt", "eurusd_1.30", "cumul_defavorable"}


def test_cli_backtest_smoke(capsys):
    from bot_trading.__main__ import main

    assert main(["backtest", "--years", "1", "--seed", "2"]) == 0
    out = capsys.readouterr().out
    assert "SYNTHÉTIQUES" in out and "Buy & hold" in out and "Complète" in out
