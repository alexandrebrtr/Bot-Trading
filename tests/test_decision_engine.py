"""Decision Engine, Technical Engine, obstacles, sizing et gestion des positions swing."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from bot_trading.config import BrokerCosts, InstrumentSpec, RiskLimits, Settings, TradingMode
from bot_trading.core import contract
from bot_trading.core.decision import Action, Decision
from bot_trading.core.models import Candle, Direction, Position, Timeframe
from bot_trading.strategy import (
    AccountView,
    DecisionEngine,
    DecisionParams,
    DrawdownTiers,
    ExitParams,
    FundamentalView,
    ObstacleParams,
    PositionManager,
    Setup,
    TechnicalEngine,
    TechnicalParams,
    TechnicalSnapshot,
    assessment_observation,
    check_obstacles,
    size_for_risk,
)

UTC = timezone.utc
T0 = datetime(2026, 11, 3, 10, 0, tzinfo=UTC)
# Montants calculés au levier 20 (stop max ≈ 2,5 %) ; la politique par défaut est 10.
SETTINGS = Settings(mode=TradingMode.BACKTEST, risk=RiskLimits(max_leverage=20.0))
H1 = timedelta(hours=1)


# ------------------------------------------------------------------ fabriques


def bull_view(**kw) -> FundamentalView:
    return replace(FundamentalView.from_labels("RISK_ON", "bullish", asof=T0, sources=("test:regime",)), **kw)


def setup(quality: float = 0.7, stop: float = 4980.0, tf: Timeframe = Timeframe.H1) -> Setup:
    return Setup("FVG", tf, 5004.0, 4996.0, quality, {}, 5, stop, True, "réaction M15 (test)", T0)


def snapshot(setups=(), d1="bullish", h4="bullish", ext=1.0) -> TechnicalSnapshot:
    best = max((s for s in setups if s.confirmed), key=lambda s: s.quality, default=None)
    return TechnicalSnapshot(
        time=T0, available=(Timeframe.H1, Timeframe.H4, Timeframe.D1), d1_trend=d1, d1_close=5000.0, d1_ema=4950.0,
        d1_atr=50.0, extension_atr=ext, vol_percentile=0.5, h4_structure=h4, h4_invalidation=4950.0,
        confirmation_tf=Timeframe.H1, setups=tuple(setups), best=best, notes=("test",),
    )


def decide(fund=None, snap=None, equity=20.0, peak=20.0, bid=5000.0, spread=0.6, engine=None, median=None):
    engine = engine or DecisionEngine(SETTINGS)
    tech = TechnicalEngine(base=Timeframe.H1)
    return engine.decide_entry(fund or bull_view(), tech, AccountView(equity, peak), bid, bid + spread, T0, median, snap or snapshot([setup()]))


# ------------------------------------------------------------------ Decision


def test_long_has_every_mission_field_and_respects_caps():
    d = decide()
    assert d.action == Action.LONG, d.reason
    assert d.missing_fields() == []
    assert d.stop_level == 4980.0
    assert d.risk_fraction == pytest.approx(0.10)
    assert d.bias == "bullish" and d.regime == "RISK_ON" and d.setup == "FVG_H1"
    size = d.meta["size_estimate"]
    margin = contract.margin_eur(size, 5000.6, SETTINGS.instrument, SETTINGS.default_eurusd, 20)
    assert d.estimated_risk_eur <= 0.5 * margin + 1e-9  # R8
    assert d.estimated_risk_eur <= 0.2 * 20.0 + 1e-9  # R9
    assert margin <= 0.5 * 20.0 + 1e-9  # R19
    assert d.to_dict()["action"] == "LONG"


@pytest.mark.parametrize(
    "fund, stage",
    [
        (FundamentalView.unavailable(), "fondamental"),
        (FundamentalView.from_labels("RISK_OFF", "bearish"), "fondamental"),
        (FundamentalView.from_labels("RISK_ON", "bearish"), "fondamental"),
        (FundamentalView.from_labels("RISK_ON", "bullish", event_blackout=True), "fondamental"),
    ],
)
def test_fundamental_dominates_even_with_perfect_setup(fund, stage):
    d = decide(fund=fund, snap=snapshot([setup(quality=1.0)]))
    assert d.action == Action.NO_TRADE
    assert d.meta["stage"] == stage
    assert d.missing_fields() == []


def test_neutral_regime_needs_max_quality_and_halves_risk():
    neutral = FundamentalView.from_labels("NEUTRE", "neutral")
    low = decide(fund=neutral, snap=snapshot([setup(quality=0.6)]))
    assert low.action == Action.NO_TRADE and low.meta["stage"] == "setup"
    high = decide(fund=neutral, snap=snapshot([setup(quality=0.85)]))
    assert high.action == Action.LONG
    assert high.risk_fraction == pytest.approx(0.05)


@pytest.mark.parametrize(
    "snap, stage",
    [
        (snapshot([setup()], d1="bearish"), "D1"),
        (snapshot([setup()], d1="unknown"), "D1"),
        (snapshot([setup()], ext=5.0), "D1"),
        (snapshot([setup()], h4="bearish"), "H4"),
        (snapshot([]), "setup"),
        (snapshot([replace(setup(), confirmed=False, confirmation="pas de réaction")]), "confirmation"),
        (snapshot([setup(stop=4800.0)]), "execution"),  # 200 pts > 2,5 % du prix
    ],
)
def test_technical_layers_can_only_refuse(snap, stage):
    d = decide(snap=snap)
    assert d.action == Action.NO_TRADE
    assert d.meta["stage"] == stage


def test_spread_filter():
    d = decide(spread=3.0, median=0.6)
    assert d.action == Action.NO_TRADE and d.meta["stage"] == "execution"


def test_drawdown_tiers_reduce_risk_then_require_max_quality():
    d = decide(equity=15.0, peak=20.0)  # DD 25 %
    assert d.action == Action.LONG and d.risk_fraction == pytest.approx(0.05)
    d = decide(equity=12.0, peak=20.0)  # DD 40 % : qualité ≥ 0,9 exigée
    assert d.action == Action.NO_TRADE and d.meta["stage"] == "setup"
    d = decide(equity=12.0, peak=20.0, snap=snapshot([setup(quality=0.95)]))
    assert d.action == Action.LONG


def test_sizing_below_minimum_gives_no_trade():
    d = decide(equity=1.0, peak=1.0)
    assert d.action == Action.NO_TRADE and d.meta["stage"] in ("sizing", "risk_veto")


def test_risk_engine_veto_turns_long_into_no_trade():
    engine = DecisionEngine(SETTINGS)
    engine.risk.engage_kill_switch()
    d = decide(engine=engine)
    assert d.action == Action.NO_TRADE and d.meta["stage"] == "risk_veto"
    assert "KILL_SWITCH" in d.reason


def test_take_profit_modes():
    fixed = DecisionEngine(SETTINGS, DecisionParams(take_profit="fixed_r", take_profit_r=2.0))
    d = decide(engine=fixed)
    assert d.take_profit_level == pytest.approx(5000.6 + 2 * (5000.6 - 4980.0), abs=0.01)
    none = DecisionEngine(SETTINGS, DecisionParams(take_profit="none"))
    assert decide(engine=none).take_profit_level is None
    with pytest.raises(ValueError):
        DecisionParams(base_risk_fraction=0.5)  # au-delà de R9


# ------------------------------------------------------------------ sizing


def test_size_for_risk_formula_and_caps():
    spec, costs, limits = InstrumentSpec(), BrokerCosts(), RiskLimits(max_leverage=20.0)
    r = size_for_risk(equity_eur=20, entry=5000, stop=4970, risk_fraction=0.1, spec=spec, costs=costs, limits=limits, eurusd=1.16)
    expected = contract.floor_size(2.0 / (30 * contract.value_per_point_eur(1.0, spec, 1.16)), spec)
    assert r.ok and r.size == pytest.approx(expected)
    capped = size_for_risk(equity_eur=20, entry=5000, stop=4970, risk_fraction=0.1, spec=spec, costs=costs, limits=limits, eurusd=1.16, max_margin_fraction=0.5)
    assert capped.margin_eur <= 10.0 + 1e-9 and capped.size <= r.size
    far = size_for_risk(equity_eur=20, entry=5000, stop=4800, risk_fraction=0.1, spec=spec, costs=costs, limits=limits, eurusd=1.16)
    assert not far.ok and far.reason == "INVALIDATION_TOO_FAR"
    wrong = size_for_risk(equity_eur=20, entry=5000, stop=5001, risk_fraction=0.1, spec=spec, costs=costs, limits=limits, eurusd=1.16)
    assert not wrong.ok and wrong.reason == "STOP_WRONG_SIDE"
    big = size_for_risk(equity_eur=10_000, entry=5000, stop=4990, risk_fraction=0.2, spec=spec, costs=costs, limits=limits, eurusd=1.16)
    assert big.size == pytest.approx(1.0)  # R5 exposition max 1 lot


def test_drawdown_tiers():
    t = DrawdownTiers()
    assert t.risk_multiplier(0.1) == 1.0 and t.risk_multiplier(0.2) == 0.5
    assert t.min_quality(0.3) is None and t.min_quality(0.35) == pytest.approx(0.9)


# ------------------------------------------------------------------ Technical Engine


def feed(engine: TechnicalEngine, candles: list[Candle]):
    for c in candles:
        engine.update_from(c, c.time + H1, {})
    return engine.snapshot()


def flat(n: int, start: datetime = T0, price: float = 5000.0) -> list[Candle]:
    return [Candle(start + i * H1, price, price + 2, price - 2, price) for i in range(n)]


def fvg_then(touch: tuple[float, float, float, float], mode: str = "half") -> TechnicalSnapshot:
    cs = flat(20)
    t = cs[-1].time + H1
    cs += [
        Candle(t, 5000, 5004, 4999, 5003),  # c1 : haut 5004
        Candle(t + H1, 5003, 5031, 5002, 5030),  # c2 : impulsion
        Candle(t + 2 * H1, 5030, 5034, 5010, 5032),  # c3 : bas 5010 > 5004 -> FVG [5004 ; 5010]
        Candle(t + 3 * H1, 5032, 5035, 5025, 5030),
    ]
    o, h, l, c = touch
    cs.append(Candle(t + 4 * H1, o, h, l, c))
    eng = TechnicalEngine(TechnicalParams(entry_mode=mode, setup_timeframes=(Timeframe.H1,), use_order_blocks=False), base=Timeframe.H1)
    return feed(eng, cs)


def test_fvg_setup_confirmed_after_measurable_reaction():
    snap = fvg_then((5012, 5017, 5006, 5016))  # mèche basse, clôture haussière au-dessus du milieu
    assert snap.best is not None and snap.best.name == "FVG_H1"
    assert snap.best.bottom == pytest.approx(5004) and snap.best.top == pytest.approx(5010)
    assert snap.best.stop < 5004
    assert 0.0 <= snap.best.quality <= 1.0


def test_first_contact_without_reaction_is_not_an_entry():
    snap = fvg_then((5012, 5013, 5006, 5007))  # bougie baissière dans la zone
    assert snap.best is None
    assert snap.setups and not snap.setups[0].confirmed


def test_full_reject_mode_requires_full_mitigation():
    snap = fvg_then((5012, 5017, 5006, 5016), mode="full_reject")
    assert snap.best is None


def test_unavailable_timeframes_fall_back_to_base():
    eng = TechnicalEngine(base=Timeframe.H1)
    assert Timeframe.M5 not in eng.states and Timeframe.M15 not in eng.states
    assert eng.confirmation_tf == Timeframe.H1


# ------------------------------------------------------------------ obstacles


def test_obstacle_room_in_r_atr_and_spreads():
    cs = flat(20)
    t = cs[-1].time + H1
    cs += [
        Candle(t, 5100, 5101, 5100, 5100),  # c1 : bas 5100
        Candle(t + H1, 5100, 5100, 5060, 5061),
        Candle(t + 2 * H1, 5061, 5090, 5050, 5055),  # c3 : haut 5090 < 5100 -> FVG baissier [5090 ; 5100]
    ]
    eng = TechnicalEngine(base=Timeframe.H1)
    feed(eng, cs)
    ok = check_obstacles(eng, entry=5050, stop=5040, spread=0.5, atr=10, p=ObstacleParams(min_room_r=1.5))
    assert ok.first is not None and ok.first.kind == "FVG_BEAR"
    assert ok.room_r == pytest.approx(4.0) and ok.room_atr == pytest.approx(4.0) and ok.room_spreads == pytest.approx(80)
    assert ok.passed
    tight = check_obstacles(eng, entry=5050, stop=5020, spread=0.5, atr=10, p=ObstacleParams(min_room_r=1.5))
    assert not tight.passed
    inside = check_obstacles(eng, entry=5095, stop=5085, spread=0.5, atr=10)
    assert inside.room_points == 0 and not inside.passed


# ------------------------------------------------------------------ positions swing


def position(entry=5000.0, stop=4980.0, opened=T0, funding=0.0) -> Position:
    return Position("SIM-1", "US500", Direction.BUY, 0.05, entry, opened, stop, True, 10.0, funding_paid_eur=funding)


def manage(pos, bid, fund=None, now=None, params=None, tech=None):
    m = PositionManager(SETTINGS, params or ExitParams())
    return m.manage(pos, bid, now or pos.opened_at + timedelta(hours=5), 20.0, fund or bull_view(), tech or TechnicalEngine(base=Timeframe.H1))


def test_breakeven_raise_only_upwards():
    d = manage(position(), bid=5025)  # +1,25 R
    assert d.action == Action.HOLD and d.stop_level is not None
    assert 5000 <= d.stop_level < 5025
    assert d.missing_fields() == []
    below = manage(position(stop=5010), bid=5025)  # stop déjà au-dessus du break-even
    assert below.action == Action.HOLD and below.stop_level is None
    small = manage(position(), bid=5010)  # +0,5 R : pas encore
    assert small.stop_level is None


def test_fundamental_invalidation_closes_and_risk_off_tightens():
    assert manage(position(), 5010, fund=FundamentalView.from_labels("RISK_ON", "bearish")).action == Action.CLOSE
    d = manage(position(), 5010, fund=FundamentalView.from_labels("RISK_OFF", "neutral"))
    assert d.action == Action.HOLD and d.stop_level is not None and d.stop_level >= 5000
    closing = manage(position(), 5010, fund=FundamentalView.from_labels("RISK_OFF", "neutral"), params=ExitParams(on_risk_off="close"))
    assert closing.action == Action.CLOSE


def test_unavailable_analysis_keeps_guaranteed_stop():
    d = manage(position(), 4990, fund=FundamentalView.unavailable())
    assert d.action == Action.HOLD and d.stop_level is None


def test_time_and_funding_exits():
    late = manage(position(), 5002, now=T0 + timedelta(days=11))
    assert late.action == Action.CLOSE and "temporelle" in late.reason
    costly = manage(position(funding=0.5), 5002)
    assert costly.action == Action.CLOSE and "Financement" in costly.reason


def test_new_stop_stays_below_bid_with_min_distance():
    s = replace(SETTINGS, instrument=InstrumentSpec(min_guaranteed_stop_distance=30.0))
    m = PositionManager(s)
    d = m.manage(position(), 5025, T0 + H1, 20.0, bull_view(), TechnicalEngine(base=Timeframe.H1))
    assert d.stop_level is None  # break-even à 5001 < 5025 − 30 impossible : stop inchangé


# ------------------------------------------------------------------ vue fondamentale


def test_view_from_regime_engine_and_point_in_time():
    from intel_fixtures import ASOF, build_store

    from bot_trading.backtest.engine import PointInTimeView
    from bot_trading.intelligence import MarketRegimeEngine
    from bot_trading.marketdata.source import InMemoryPointInTimeSource

    a = MarketRegimeEngine(build_store("goldilocks")).assess(ASOF)
    v = FundamentalView.from_assessment(a)
    assert v.available and v.regime == a.regime.value and v.size_multiplier == a.size_multiplier
    assert v.sources
    src = InMemoryPointInTimeSource([assessment_observation(a)])
    assert FundamentalView.from_pit(PointInTimeView(src, ASOF)) == v
    assert not FundamentalView.from_pit(PointInTimeView(src, ASOF - timedelta(seconds=1))).available


def test_labels_mapping():
    assert FundamentalView.from_labels("bull", "bullish").regime == "RISK_ON"
    assert FundamentalView.from_labels("bear", None).allow_new_entries is False
    assert FundamentalView.from_labels("chop", "neutral").size_multiplier == 0.5
    assert not FundamentalView.from_labels(None, None).available


def test_decision_missing_fields_reports_gaps():
    assert "bias" in Decision.no_trade("x").missing_fields()
