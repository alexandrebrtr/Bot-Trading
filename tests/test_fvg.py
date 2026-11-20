from dataclasses import replace
from datetime import timedelta

import pytest

from bot_trading.analysis.fvg import FVGEngine, FVGParams, detect_fvgs
from bot_trading.core.models import Timeframe

from conftest import make_candles, random_walk


def test_bullish_fvg_basic():
    cs = make_candles([(100, 101, 99, 100.5), (100.5, 105, 100.4, 104.8), (104.8, 106, 102, 105.5)])
    fvgs = detect_fvgs(cs, timeframe=Timeframe.H1)
    assert len(fvgs) == 1
    f = fvgs[0]
    assert f.bullish and f.bottom == 101 and f.top == 102
    assert f.first_index == 0 and f.confirmed_index == 2 and f.timeframe == Timeframe.H1


def test_bearish_fvg_basic():
    cs = make_candles([(100, 101, 99, 99.5), (99.5, 99.6, 95, 95.2), (95.2, 97, 94, 94.5)])
    f = detect_fvgs(cs)[0]
    assert not f.bullish and f.top == 99 and f.bottom == 97


def test_touching_wicks_is_not_fvg():
    cs = make_candles([(100, 101, 99, 100.5), (100.5, 105, 100.4, 104.8), (104.8, 106, 101, 105.5)])
    assert detect_fvgs(cs) == []


def test_fvg_not_visible_before_third_candle_closes():
    cs = make_candles([(100, 101, 99, 100.5), (100.5, 105, 100.4, 104.8), (104.8, 106, 102, 105.5)])
    eng = FVGEngine()
    assert eng.update(cs[0]) == [] and eng.update(cs[1]) == []
    assert len(eng.update(cs[2])) == 1


def test_mitigation_tracking():
    cs = make_candles(
        [
            (100, 101, 99, 100.5),
            (100.5, 105, 100.4, 104.8),
            (104.8, 106, 102, 105.5),  # FVG [101, 102]
            (105.5, 106, 103, 104),  # pas de contact
            (104, 104.5, 101.6, 103),  # pénétration 40 %
            (103, 103.5, 101.4, 103.2),  # toujours dans la zone, 60 %
            (103.2, 104, 100.9, 103.5),  # traversée complète, clôture au-dessus
            (103.5, 104, 100.5, 100.8),  # clôture sous la zone → invalidé
        ]
    )
    f = detect_fvgs(cs)[0]
    st = f.state
    assert st.first_touch_index == 4
    assert st.half_mitigated_index == 5
    assert st.full_mitigated_index == 6
    assert st.touches == 1  # bougies consécutives dans la zone = un seul contact
    assert st.invalidated_index == 7 and not st.is_active


def test_retests_counted_on_each_new_entry():
    cs = make_candles(
        [
            (100, 101, 99, 100.5),
            (100.5, 105, 100.4, 104.8),
            (104.8, 106, 102, 105.5),  # FVG [101, 102]
            (105.5, 106, 101.8, 104),  # contact 1
            (104, 105, 103, 104.5),  # sortie
            (104.5, 105, 101.5, 104),  # contact 2
            (104, 105, 103, 104.5),  # sortie
            (104.5, 105, 101.9, 104),  # contact 3
        ]
    )
    assert detect_fvgs(cs)[0].state.touches == 3


def test_formation_candles_do_not_mitigate():
    cs = make_candles([(100, 101, 99, 100.5), (100.5, 105, 100.4, 104.8), (104.8, 106, 102, 105.5)])
    f = detect_fvgs(cs)[0]
    assert f.state.max_mitigation == 0 and f.state.touches == 0


def test_size_and_impulse_in_atr_units():
    cs = random_walk(200, seed=3)
    for f in detect_fvgs(cs, FVGParams(atr_period=14)):
        if f.atr_at_confirmation:
            assert f.size_atr == pytest.approx(f.size / f.atr_at_confirmation)


def test_min_size_filter():
    cs = random_walk(400, seed=5)
    all_ = detect_fvgs(cs)
    big = detect_fvgs(cs, FVGParams(min_size_atr=0.5))
    assert len(big) < len(all_)
    assert all(f.size_atr is None or f.size_atr >= 0.5 for f in big)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_no_lookahead_prefix_invariance(seed):
    """Les FVG confirmés à l'indice i sont identiques, que la série s'arrête à i ou continue."""
    cs = random_walk(400, seed=seed)
    full = detect_fvgs(cs)
    assert full, "la série de test doit contenir des FVG"
    for cut in (50, 137, 250, 399):
        prefix = detect_fvgs(cs[: cut + 1])
        expected = [f for f in full if f.confirmed_index <= cut]
        keys = lambda fs: [(f.bullish, f.first_index, f.confirmed_index, f.top, f.bottom, f.size_atr, f.impulse_atr, f.with_bos) for f in fs]
        assert keys(prefix) == keys(expected)


def test_future_change_does_not_alter_past_detection():
    cs = random_walk(300, seed=9)
    a = detect_fvgs(cs)
    # même passé, futur différent (heures recalées après la bougie 199)
    altered = cs[:200] + [replace(c, time=cs[199].time + timedelta(minutes=5 * (k + 1))) for k, c in enumerate(random_walk(100, seed=99))]
    b = detect_fvgs(altered)
    past = lambda fs: [(f.first_index, f.top, f.bottom, f.bullish) for f in fs if f.confirmed_index < 200]
    assert past(a) == past(b)


def test_rejects_unordered_candles():
    cs = make_candles([(100, 101, 99, 100), (100, 101, 99, 100)])
    eng = FVGEngine()
    eng.update(cs[1])
    with pytest.raises(ValueError):
        eng.update(cs[0])
