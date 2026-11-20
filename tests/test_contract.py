import pytest

from bot_trading.config import InstrumentSpec
from bot_trading.core import contract

SPEC = InstrumentSpec()


def test_one_lot_margin_close_to_user_estimate():
    # 1 lot ≈ 340 € de marge selon l'utilisateur (US500 ≈ 6800, levier 20).
    m = contract.margin_eur(1.0, 6800, SPEC, eurusd=1.0, leverage=20)
    assert m == pytest.approx(340.0)


def test_min_lot_margin_fits_20_eur():
    m = contract.margin_eur(0.01, 6800, SPEC, eurusd=1.16, leverage=20)
    assert m < 20 and m == pytest.approx(6800 * 0.01 / 20 / 1.16)


def test_pnl_long():
    assert contract.pnl_eur(0.01, 6800, 6810, True, SPEC, 1.0) == pytest.approx(0.10)
    assert contract.pnl_eur(0.01, 6800, 6790, True, SPEC, 1.0) == pytest.approx(-0.10)


@pytest.mark.parametrize("size,ok", [(0.01, True), (0.02, True), (0.5, True), (0.005, False), (0.015, False), (0.0, False)])
def test_valid_size(size, ok):
    assert contract.is_valid_size(size, SPEC) is ok


def test_floor_size():
    assert contract.floor_size(0.019, SPEC) == 0.01
    assert contract.floor_size(0.009, SPEC) == 0.0
    assert contract.floor_size(0.37, SPEC) == 0.37
