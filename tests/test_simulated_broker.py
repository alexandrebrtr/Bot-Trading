from dataclasses import replace
from datetime import timedelta

import pytest

from bot_trading.broker.simulated import SimulatedBroker
from bot_trading.config import BrokerCosts, Settings
from bot_trading.core.models import Candle, Direction, OrderRequest, Quote

from conftest import T0


def broker(**costs):
    s = Settings()
    s = replace(s, costs=replace(BrokerCosts(overnight_long_rate_daily=0.0), **costs))
    b = SimulatedBroker(s, eurusd=1.0)
    b.on_quote(Quote(T0, 6800.0, 6800.6))
    return b


def buy(b, stop=6750.0, tp=None, size=0.01, gsl=True):
    return b.place_market_order(OrderRequest("US500", Direction.BUY, size, stop, gsl, tp))


def bar(minutes_after, o, h, l, c):
    return Candle(T0 + timedelta(minutes=minutes_after), o, h, l, c)


def test_initial_state():
    b = broker()
    assert b.balance_eur == 20.0 and b.equity_eur() == 20.0


def test_buy_fills_at_ask_and_pays_spread():
    b = broker()
    r = buy(b)
    assert r.accepted and r.position.entry_price == 6800.6
    assert r.position.margin_eur == pytest.approx(0.01 * 6800.6 / 20)
    assert b.equity_eur() == pytest.approx(20.0 - 0.006)


def test_risk_veto_blocks_order():
    b = broker()
    r = b.place_market_order(OrderRequest("US500", Direction.SELL, 0.01, 6850, True))
    assert not r.accepted and not b.positions


def test_guaranteed_stop_fills_exactly_at_stop_on_gap():
    b = broker(guaranteed_stop_premium_points=0.5)
    buy(b, stop=6750)
    b.on_bar(bar(0, 6700, 6705, 6650, 6690), spread=0.6)  # gap sous le stop
    t = b.closed_trades[-1]
    assert t.exit_reason == "STOP" and t.exit_price == 6750
    assert t.fees_eur == pytest.approx(0.005)
    assert b.balance_eur == pytest.approx(20 + (6750 - 6800.6) * 0.01 - 0.005)


def test_non_guaranteed_stop_slips_on_gap():
    b = broker()
    s = replace(b.settings, risk=replace(b.settings.risk, require_guaranteed_stop=False))
    b2 = SimulatedBroker(s, eurusd=1.0)
    b2.on_quote(Quote(T0, 6800.0, 6800.6))
    assert buy(b2, stop=6750, gsl=False).accepted
    b2.on_bar(bar(0, 6700, 6705, 6650, 6690))
    assert b2.closed_trades[-1].exit_price == 6700


def test_take_profit():
    b = broker()
    buy(b, tp=6850)
    b.on_bar(bar(0, 6801, 6860, 6799, 6855))
    t = b.closed_trades[-1]
    assert t.exit_reason == "TAKE_PROFIT" and t.exit_price == 6850


def test_stop_before_tp_in_same_bar():
    b = broker()
    buy(b, stop=6780, tp=6850)
    b.on_bar(bar(0, 6800, 6860, 6770, 6820))
    assert b.closed_trades[-1].exit_reason == "STOP"


def test_quote_triggers_stop():
    b = broker()
    buy(b, stop=6790)
    b.on_quote(Quote(T0 + timedelta(minutes=1), 6789.0, 6789.6))
    assert b.closed_trades[-1].exit_price == 6790 and not b.positions


def test_overnight_funding_and_friday_triple():
    s = replace(Settings(), costs=BrokerCosts(overnight_long_rate_daily=0.0001, funding_hour_utc=22))
    b = SimulatedBroker(s, eurusd=1.0)
    b.on_quote(Quote(T0, 6800.0, 6800.6))  # lundi 14h
    buy(b, stop=6700)
    # avance jusqu'au samedi 00h : lun, mar, mer, jeu (x1) + ven (x3) = 7 jours facturés
    b.on_quote(Quote(T0 + timedelta(days=4, hours=10), 6800.0, 6800.6))
    notional = 0.01 * 6800.3
    assert b.positions["SIM-000001"].funding_paid_eur == pytest.approx(notional * 0.0001 * 7)
    # rien de plus pendant le week-end
    b.on_quote(Quote(T0 + timedelta(days=6, hours=5), 6800.0, 6800.6))
    assert b.positions["SIM-000001"].funding_paid_eur == pytest.approx(notional * 0.0001 * 7)


def test_exposure_cap_across_orders():
    s = replace(Settings(), risk=replace(Settings().risk, initial_capital_eur=10_000))
    b = SimulatedBroker(s, eurusd=1.0)
    b.on_quote(Quote(T0, 6800.0, 6800.6))
    assert b.place_market_order(OrderRequest("US500", Direction.BUY, 0.6, 6750, True)).accepted
    assert not b.place_market_order(OrderRequest("US500", Direction.BUY, 0.5, 6750, True)).accepted
    assert b.place_market_order(OrderRequest("US500", Direction.BUY, 0.4, 6750, True)).accepted


def test_drawdown_halt_closes_and_blocks():
    # risque par trade relevé à 50 % de l'equity : ce test vise le drawdown, pas cette règle
    s = replace(Settings(), risk=replace(Settings().risk, initial_capital_eur=5.0, max_risk_fraction_of_equity=0.5))
    b = SimulatedBroker(s, eurusd=1.0)
    b.on_quote(Quote(T0, 6800.0, 6800.6))
    assert buy(b, stop=6640.6).accepted
    b.on_bar(bar(0, 6800, 7300, 6800, 7300))  # equity ≈ 10 €, nouveau plus haut
    assert b.peak_equity_eur == pytest.approx(5 + (7300 - 6800.6) * 0.01)
    b.on_bar(bar(5, 7300, 7300, 6700, 6700))  # equity ≈ 4 € < plancher 5 €
    assert b.risk.halted
    assert not b.positions and b.closed_trades[-1].exit_reason == "DRAWDOWN_HALT"
    r = buy(b, stop=6650)
    assert not r.accepted and "DRAWDOWN_HALT" in r.message


def test_stop_out_when_margin_level_too_low():
    # sans stop garanti et avec un stop lointain, la marge s'épuise avant le stop
    s = replace(
        Settings(),
        risk=replace(Settings().risk, require_guaranteed_stop=False, max_loss_fraction_of_margin=100, max_risk_fraction_of_equity=1.0, max_account_drawdown=1.0),
    )
    b = SimulatedBroker(s, eurusd=1.0)
    b.on_quote(Quote(T0, 6800.0, 6800.6))
    assert buy(b, stop=6450, gsl=False, size=0.05).accepted  # marge ≈ 17 €
    b.on_bar(bar(0, 6800, 6800, 6560, 6560))  # perte ≈ 12 € → equity ≈ 8 € < 50 % de 17 €
    assert b.closed_trades[-1].exit_reason == "STOP_OUT"
