from dataclasses import replace
from datetime import timedelta

import pytest

from bot_trading.config import BrokerCosts, InstrumentSpec, RiskLimits
from bot_trading.core.models import Direction, OrderRequest
from bot_trading.risk.engine import AccountState, MarketState, RiskEngine, Violation

from conftest import T0

EURUSD = 1.0


def engine(**limits):
    return RiskEngine(RiskLimits(**limits), InstrumentSpec(), BrokerCosts())


def account(equity=20.0, peak=20.0, used=0.0, lots=0.0, open_risk=0.0):
    return AccountState(equity, peak, used, lots, open_risk)


def market(bid=6800.0, ask=6800.6, age_s=1, **kw):
    return MarketState(bid=bid, ask=ask, quote_time=T0, now=T0 + timedelta(seconds=age_s), eurusd=EURUSD, **kw)


def order(size=0.01, stop=6750.0, gsl=True, direction=Direction.BUY, tp=None, epic="US500"):
    return OrderRequest(epic, direction, size, stop, gsl, tp)


def test_valid_long_is_approved():
    d = engine().check_order(order(), account(), market())
    assert d.approved, d.explain()


def test_short_vetoed_by_default():
    d = engine().check_order(order(direction=Direction.SELL, stop=6850), account(), market())
    assert Violation.SHORT_DISABLED in d.violations


def test_guaranteed_stop_mandatory():
    d = engine().check_order(order(gsl=False), account(), market())
    assert Violation.STOP_NOT_GUARANTEED in d.violations


def test_missing_stop_vetoed():
    d = engine().check_order(order(stop=None), account(), market())
    assert Violation.NO_STOP in d.violations


def test_stop_above_entry_vetoed():
    d = engine().check_order(order(stop=6801), account(), market())
    assert Violation.STOP_WRONG_SIDE in d.violations


def test_max_loss_50pct_of_margin():
    e = engine()
    # Marge = 0.01*6800.6/20 = 3.4003 ; 50 % = 1.70015 € → 170.015 points max.
    limit = e.max_stop_distance(6800.6)
    assert limit == pytest.approx(170.015)
    ok = e.check_order(order(stop=6800.6 - 170.0), account(), market())
    ko = e.check_order(order(stop=6800.6 - 171.0), account(), market())
    assert ok.approved, ok.explain()
    assert Violation.MAX_LOSS_EXCEEDED in ko.violations


def test_max_loss_includes_gsl_premium():
    e = RiskEngine(RiskLimits(), InstrumentSpec(), BrokerCosts(guaranteed_stop_premium_points=5))
    d = e.check_order(order(stop=6800.6 - 168.0), account(), market())
    assert Violation.MAX_LOSS_EXCEEDED in d.violations


def test_exposure_cap_one_lot():
    d = engine().check_order(order(size=0.02), account(equity=1000, peak=1000, lots=0.99), market())
    assert Violation.EXPOSURE_EXCEEDED in d.violations


def test_insufficient_margin():
    d = engine().check_order(order(size=0.1), account(), market())  # 34 € > 20 €
    assert Violation.INSUFFICIENT_MARGIN in d.violations


def test_invalid_size():
    d = engine().check_order(order(size=0.015), account(), market())
    assert Violation.INVALID_SIZE in d.violations


def test_drawdown_halt_latches():
    e = engine()
    d = e.evaluate_account(account(equity=10.0, peak=20.0))
    assert not d.approved and e.halted
    # même si l'equity remonte, le verrou reste
    d2 = e.check_order(order(), account(equity=19.0, peak=20.0), market())
    assert Violation.DRAWDOWN_HALT in d2.violations
    with pytest.raises(PermissionError):
        e.reset_halt("oui")
    e.reset_halt("JE_CONFIRME_LA_REPRISE")
    assert e.check_order(order(), account(equity=19.0, peak=20.0), market()).approved


def test_drawdown_measured_from_peak():
    e = engine()
    assert e.evaluate_account(account(equity=25.1, peak=50.0)).approved
    assert not e.evaluate_account(account(equity=25.0, peak=50.0)).approved


def test_trade_that_could_breach_drawdown_is_vetoed():
    e = engine()
    # equity 11 €, plancher 10 €, perte au stop ~1,5 € → violerait le plancher
    d = e.check_order(order(stop=6650.6), account(equity=11.0, peak=20.0), market())
    assert Violation.DRAWDOWN_WOULD_BREACH in d.violations


def test_open_risk_counts_toward_drawdown():
    e = engine()
    d = e.check_order(order(stop=6750.6), account(equity=12.0, peak=20.0, open_risk=1.8), market())
    assert Violation.DRAWDOWN_WOULD_BREACH in d.violations


def test_kill_switch():
    e = engine()
    e.engage_kill_switch()
    assert Violation.KILL_SWITCH in e.check_order(order(), account(), market()).violations


def test_market_conditions():
    e = engine()
    assert Violation.SPREAD_TOO_WIDE in e.check_order(order(), account(), market(ask=6803)).violations
    assert Violation.STALE_DATA in e.check_order(order(), account(), market(age_s=3600)).violations
    assert Violation.EVENT_BLACKOUT in e.check_order(order(), account(), market(event_blackout=True)).violations
    assert Violation.MARKET_CLOSED in e.check_order(order(), account(), market(market_open=False)).violations


def test_wrong_epic_and_tp_side():
    d = engine().check_order(order(epic="US100", tp=6700), account(), market())
    assert Violation.WRONG_EPIC in d.violations
    assert Violation.TAKE_PROFIT_WRONG_SIDE in d.violations


def test_all_violations_reported_and_deterministic():
    e1, e2 = engine(), engine()
    bad = order(size=0.015, stop=None, gsl=False, direction=Direction.SELL)
    d1 = e1.check_order(bad, account(), market(ask=6805))
    d2 = e2.check_order(bad, account(), market(ask=6805))
    assert d1 == d2
    assert len(d1.violations) >= 4


# ------------------------------------------------ risque max 20 % de l'equity
def test_equity_rule_binds_at_larger_size():
    e = engine()
    # 0,03 lot : marge ≈ 10,2 €, 50 % = 5,1 € ; 20 % de 20 € = 4 €
    ko = e.check_order(order(size=0.03, stop=6800.6 - 150), account(), market())
    assert ko.violations == (Violation.MAX_RISK_PER_TRADE_EXCEEDED,)
    assert ko.details["max_loss_allowed_eur"] == pytest.approx(4.0)
    ok = e.check_order(order(size=0.03, stop=6800.6 - 130), account(), market())
    assert ok.approved, ok.explain()


def test_margin_rule_binds_at_min_size():
    d = engine().check_order(order(stop=6800.6 - 171), account(), market())
    assert d.violations == (Violation.MAX_LOSS_EXCEEDED,)
    assert d.details["max_loss_allowed_eur"] == pytest.approx(1.70015)


def test_equity_rule_binds_when_equity_is_small():
    e = engine(initial_capital_eur=8.0)
    # 20 % de 8 € = 1,6 € < 50 % de la marge (1,70 €)
    d = e.check_order(order(stop=6800.6 - 165), account(equity=8.0, peak=8.0), market())
    assert d.violations == (Violation.MAX_RISK_PER_TRADE_EXCEEDED,)
    assert e.check_order(order(stop=6800.6 - 155), account(equity=8.0, peak=8.0), market()).approved


def test_equity_rule_cannot_loosen_margin_rule():
    e = engine(max_risk_fraction_of_equity=1.0)
    d = e.check_order(order(stop=6800.6 - 171), account(equity=1000, peak=1000), market())
    assert Violation.MAX_LOSS_EXCEEDED in d.violations


def test_max_loss_allowed_is_min_of_both_rules():
    e = engine()
    assert e.max_loss_allowed_eur(margin_eur=3.4, equity_eur=20) == pytest.approx(1.7)
    assert e.max_loss_allowed_eur(margin_eur=10.2, equity_eur=20) == pytest.approx(4.0)
    assert e.max_loss_allowed_eur(margin_eur=10.2, equity_eur=-5) == 0.0


def test_max_stop_distance_combined():
    e = engine()
    assert e.max_stop_distance(6800.6) == pytest.approx(170.015)
    assert e.max_stop_distance(6800.6, size=0.01, equity_eur=20) == pytest.approx(170.015)
    assert e.max_stop_distance(6800.6, size=0.03, equity_eur=20) == pytest.approx(4.0 / 0.03)
