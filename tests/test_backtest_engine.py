from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot_trading.backtest.engine import (
    Backtester,
    BacktestConfig,
    CausalHistory,
    LookaheadError,
    MultiTimeframe,
    PointInTimeView,
)
from bot_trading.config import BrokerCosts, InstrumentSpec, Settings, TradingMode
from bot_trading.core.decision import Action, Decision
from bot_trading.core.models import Candle, Timeframe
from bot_trading.marketdata.candles import resample
from bot_trading.marketdata.source import Bar, InMemoryPointInTimeSource, Observation

UTC = timezone.utc
# Mardi 10:00 UTC : pas de financement avant 22:00.
T0 = datetime(2026, 11, 3, 10, 0, tzinfo=UTC)


def flat_bars(n: int, price: float = 5000.0, tf: Timeframe = Timeframe.H1, start: datetime = T0, spread: float = 1.0) -> list[Bar]:
    step = timedelta(minutes=tf.minutes)
    return [Bar(Candle(start + i * step, price, price + 2, price - 2, price), tf, spread) for i in range(n)]


class Script:
    """Stratégie scriptée : {indice: Decision}."""

    name = "script"

    def __init__(self, plan: dict[int, Decision]) -> None:
        self.plan = plan
        self.seen: list[int] = []

    def on_bar(self, ctx):
        self.seen.append(ctx.index)
        return self.plan.get(ctx.index, Decision.hold() if ctx.in_position else Decision.no_trade())


def longs(res) -> list[str]:
    return [d.outcome for d in res.decisions if d.decision.action == Action.LONG]


def settings(**costs) -> Settings:
    return Settings(mode=TradingMode.BACKTEST, costs=BrokerCosts(**costs))


def test_entry_at_next_open_not_signal_close():
    bars = flat_bars(10)
    bars[3] = Bar(Candle(bars[3].time, 5000, 5003, 4999, 5000), Timeframe.H1, 1.0)
    bars[4] = Bar(Candle(bars[4].time, 5010, 5012, 5008, 5011), Timeframe.H1, 1.0)
    res = Backtester(BacktestConfig(settings=settings())).run(Script({3: Decision(Action.LONG, stop_level=4950)}), bars)
    assert longs(res)[0] == "FILLED"
    t = res.trades[0].trade
    assert t.entry_price == pytest.approx(5011.0)  # ouverture de la bougie 4 + spread
    assert t.opened_at == bars[4].time
    # R15 : 10 % de 20 € / (61 points × valeur du point) → plancher au pas de 0,01.
    assert t.size == pytest.approx(0.03)
    assert t.exit_reason == "END_OF_BACKTEST"


def test_guaranteed_stop_on_gap_exits_at_stop_level():
    bars = flat_bars(8)
    gap = bars[5].time
    bars[5] = Bar(Candle(gap, 4800, 4805, 4790, 4800), Timeframe.H1, 1.0)
    bars[6] = Bar(Candle(bars[6].time, 4800, 4802, 4798, 4800), Timeframe.H1, 1.0)
    bars[7] = Bar(Candle(bars[7].time, 4800, 4802, 4798, 4800), Timeframe.H1, 1.0)
    res = Backtester(BacktestConfig(settings=settings(guaranteed_stop_premium_points=0.5))).run(Script({1: Decision(Action.LONG, stop_level=4950)}), bars)
    t = res.trades[0].trade
    assert t.exit_reason == "STOP"
    assert t.exit_price == 4950  # pas 4800 : le stop garanti protège du gap
    assert t.fees_eur > 0  # prime du stop garanti facturée au déclenchement
    rec = res.trades[0]
    assert rec.r_multiple == pytest.approx(-1.0, abs=1e-9)


def test_overnight_funding_and_friday_multiplier():
    # Jeudi 20:00 → lundi : prélèvements jeudi 22:00 (×1) et vendredi 22:00 (×3).
    start = datetime(2026, 11, 5, 20, 0, tzinfo=UTC)
    bars = flat_bars(4 * 24, start=start)
    res = Backtester(BacktestConfig(settings=settings())).run(Script({0: Decision(Action.LONG, stop_level=4950, size=0.01)}), bars)
    t = res.trades[0].trade
    s = settings()
    notional = 0.01 * 5000 / s.default_eurusd
    days = 1 + 3 + 1  # jeudi, vendredi ×3, dimanche/lundi 22:00 → lundi (dernier jour inclus si atteint)
    swaps = [x for x in res.ledger if x[1].startswith("SWAP")]
    assert len(swaps) >= 2
    assert swaps[1][0].weekday() == 4
    assert abs(swaps[1][2]) == pytest.approx(3 * abs(swaps[0][2]))
    assert t.funding_eur == pytest.approx(sum(-x[2] for x in swaps))
    assert t.funding_eur <= notional * s.costs.overnight_long_rate_daily * days + 1e-9


def test_size_below_min_is_no_trade():
    bars = flat_bars(6)
    res = Backtester().run(Script({1: Decision(Action.LONG, stop_level=4900, risk_fraction=0.001)}), bars)
    assert longs(res)[0] == "SIZE_BELOW_MIN"
    assert not res.trades


def test_risk_engine_vetoes_are_recorded():
    bars = flat_bars(8)
    plan = {
        1: Decision(Action.LONG, stop_level=4950, size=1.0),  # marge insuffisante pour 20 €
        3: Decision(Action.LONG, stop_level=4700),  # stop > 2,5 % : R8
    }
    res = Backtester().run(Script(plan), bars)
    out = [d.outcome for d in res.decisions if d.decision.action == Action.LONG]
    assert "INSUFFICIENT_MARGIN" in out[0]
    assert "MAX_LOSS_EXCEEDED" in out[1] or out[1] == "SIZE_BELOW_MIN"
    assert not res.trades


def test_untradable_bar_and_interruptions():
    bars = flat_bars(6)
    bars[2] = Bar(bars[2].bid, Timeframe.H1, 1.0, tradable=False)
    # Trou de 5 heures entre la bougie 4 et la suivante.
    bars[5] = Bar(Candle(bars[4].time + timedelta(hours=6), 5000, 5001, 4999, 5000), Timeframe.H1, 1.0)
    res = Backtester().run(Script({1: Decision(Action.LONG, stop_level=4950)}), bars)
    assert "MARKET_CLOSED" in longs(res)[0]
    assert len(res.interruptions) == 1


def test_spread_too_wide_veto():
    bars = flat_bars(5, spread=5.0)
    res = Backtester().run(Script({1: Decision(Action.LONG, stop_level=4950)}), bars)
    assert "SPREAD_TOO_WIDE" in longs(res)[0]


def test_hold_raises_stop_only_upwards():
    bars = flat_bars(10)
    plan = {
        0: Decision(Action.LONG, stop_level=4950),
        3: Decision(Action.HOLD, stop_level=4970),
        5: Decision(Action.HOLD, stop_level=4960),  # éloignement refusé
    }
    res = Backtester().run(Script(plan), bars)
    outs = [d.outcome for d in res.decisions if d.decision.action == Action.HOLD and d.decision.stop_level]
    assert outs == ["STOP_RAISED", "STOP_UNCHANGED"]


def test_close_decision_executes_next_open():
    bars = flat_bars(10)
    res = Backtester().run(Script({0: Decision(Action.LONG, stop_level=4950), 4: Decision(Action.CLOSE, reason="test")}), bars)
    t = res.trades[0].trade
    assert t.exit_reason == "test"
    assert t.closed_at == bars[5].time


def test_warmup_never_trades():
    bars = flat_bars(10)
    s = Script({i: Decision(Action.LONG, stop_level=4950) for i in range(10)})
    res = Backtester().run(s, bars, trade_start=bars[6].time)
    assert s.seen == list(range(10))  # la stratégie voit le préchauffage
    assert res.trades[0].trade.opened_at >= bars[6].time
    assert res.equity_curve[0][0] > bars[6].time


def test_drawdown_halt_blocks_everything_after():
    # Le prix baisse de 3 % toutes les 3 bougies : chaque position est arrêtée au stop garanti.
    price = 5000.0
    bars = []
    for i, b in enumerate(flat_bars(60)):
        if i % 3 == 2:
            price *= 0.97
        bars.append(Bar(Candle(b.time, price, price + 1, price - 1, price), Timeframe.H1, 1.0))

    class Always:
        name = "always"

        def on_bar(self, ctx):
            if ctx.in_position:
                return Decision.hold()
            return Decision(Action.LONG, stop_level=ctx.candle.close * 0.985, risk_fraction=0.2)

    res = Backtester().run(Always(), bars)
    assert all(t.trade.exit_reason == "STOP" for t in res.trades)
    # Le Risk Engine refuse toute entrée qui ferait franchir le plancher de 50 %.
    assert min(e for _, e in res.equity_curve) >= 10.0 - 1e-9
    assert any("DRAWDOWN" in o for o in longs(res))


def test_causal_history_blocks_future():
    bars = flat_bars(5)
    h = CausalHistory(bars, 2)
    assert len(h) == 3
    assert h[-1] is bars[2]
    assert h[0:10] == bars[0:3]
    with pytest.raises(LookaheadError):
        _ = h[3]


def test_strategy_peeking_future_raises():
    class Peek:
        name = "peek"

        def on_bar(self, ctx):
            ctx.history[ctx.index + 1]
            return Decision.no_trade()

    with pytest.raises(LookaheadError):
        Backtester().run(Peek(), flat_bars(5))


def test_point_in_time_view_and_source():
    src = InMemoryPointInTimeSource()
    p = datetime(2026, 10, 1, tzinfo=UTC)
    src.add(Observation("cpi", p, 3.0, datetime(2026, 11, 12, 13, 30, tzinfo=UTC)))
    src.add(Observation("cpi", p, 3.1, datetime(2026, 12, 10, 13, 30, tzinfo=UTC)))  # révision
    v = PointInTimeView(src, datetime(2026, 11, 12, 13, 29, tzinfo=UTC))
    assert v.get("cpi") is None
    assert PointInTimeView(src, datetime(2026, 11, 20, tzinfo=UTC)).value("cpi") == 3.0
    assert PointInTimeView(src, datetime(2027, 1, 1, tzinfo=UTC)).value("cpi") == 3.1
    with pytest.raises(ValueError):
        src.add(Observation("cpi", p, 3.0, datetime(2026, 11, 12)))  # naïf
    with pytest.raises(ValueError):
        src.add(Observation("cpi", p, 3.0, datetime(2026, 9, 1, tzinfo=UTC)))  # publié avant la période

    class Liar:
        def as_of(self, series, t):
            return Observation(series, t, 1, t + timedelta(seconds=1))

    with pytest.raises(LookaheadError):
        PointInTimeView(Liar(), T0).get("x")


def test_multitimeframe_matches_resample_and_waits_for_close():
    from conftest import random_walk

    m5 = random_walk(600)
    mtf = MultiTimeframe(Timeframe.M5, [Timeframe.H1, Timeframe.M15])
    for i, c in enumerate(m5):
        mtf.update(c)
        for h in mtf.closed[Timeframe.H1]:
            assert h.time + timedelta(hours=1) <= c.time + timedelta(minutes=5)
    ref = resample(m5, Timeframe.H1, include_partial=False)
    got = mtf.closed[Timeframe.H1]
    assert got[: len(ref)] == ref
    assert len(got) in (len(ref), len(ref) + 1)


def test_backtest_mode_forced():
    bt = Backtester(BacktestConfig(settings=Settings(mode=TradingMode.PAPER)))
    assert bt.config.settings.mode == TradingMode.BACKTEST


def test_min_guaranteed_stop_distance_applied():
    s = Settings(mode=TradingMode.BACKTEST, instrument=InstrumentSpec(min_guaranteed_stop_distance=20.0))
    bars = flat_bars(6)
    res = Backtester(BacktestConfig(settings=s)).run(Script({1: Decision(Action.LONG, stop_level=4999, size=0.01)}), bars)
    t = res.trades[0]
    assert longs(res)[0] == "FILLED"
    assert t.initial_risk_eur == pytest.approx(20.0 * 0.01 / s.default_eurusd)
