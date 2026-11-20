import pytest

from bot_trading.analysis.indicators import IncrementalATR, atr
from bot_trading.analysis.order_blocks import OBParams, detect_order_blocks
from bot_trading.analysis.structure import StructureTracker, SwingKind, detect_bos, detect_swings

from conftest import make_candles, random_walk


def test_incremental_atr_matches_batch():
    cs = random_walk(200)
    inc = IncrementalATR(14)
    for c in cs:
        inc.update(c)
    for a, b in zip(inc.values, atr(cs, 14)):
        assert (a is None and b is None) or a == pytest.approx(b)


def test_swing_confirmed_only_after_right_bars():
    cs = make_candles([(10, 11, 9, 10), (10, 12, 9.5, 11), (11, 15, 10, 14), (13.5, 14, 12, 12.5), (12.5, 13, 11, 12)])
    swings = detect_swings(cs, left=2, right=2)
    highs = [s for s in swings if s.kind == SwingKind.HIGH]
    assert highs and highs[0].index == 2 and highs[0].confirmed_index == 4
    assert detect_swings(cs[:4], 2, 2) == []


def test_tracker_matches_batch():
    cs = random_walk(500, seed=11)
    tr = StructureTracker(2, 2)
    for c in cs:
        tr.update(c)
    assert tr.bos == detect_bos(cs, 2, 2)
    assert sorted(tr.swings, key=lambda s: (s.confirmed_index, s.index, s.kind.value)) == sorted(
        detect_swings(cs, 2, 2), key=lambda s: (s.confirmed_index, s.index, s.kind.value)
    )


def test_bos_uses_close_not_wick():
    # swing high 15 en indice 2 ; une mèche à 16 sans clôture au-dessus ne casse pas
    cs = make_candles(
        [(10, 11, 9, 10), (10, 12, 9.5, 11), (11, 15, 10, 14), (13.5, 14, 12, 12.5), (12.5, 13, 11, 12),
         (12, 16, 11.5, 14.5), (14.5, 16.5, 14, 16)]
    )
    bos = detect_bos(cs, 2, 2)
    assert [(e.bullish, e.index, e.level) for e in bos if e.bullish] == [(True, 6, 15)]


def _ob_series():
    return make_candles(
        [
            (100, 101, 99, 100.5),
            (100.5, 102, 100, 101.5),
            (101.5, 105, 101, 104),  # swing high 105 (indice 2)
            (104, 104.5, 102, 102.5),
            (102.5, 103, 100.5, 101),
            (101, 101.5, 99.5, 100),  # dernière bougie baissière = OB (indice 5)
            (100, 103, 100, 102.8),
            (102.8, 106.5, 102.5, 106.2),  # clôture > 105 → BOS (indice 7)
        ]
    )


def test_bullish_order_block_detection():
    obs = detect_order_blocks(_ob_series(), OBParams(atr_period=3, min_displacement_atr=0.5))
    bull = [o for o in obs if o.bullish]
    assert len(bull) == 1
    ob = bull[0]
    assert ob.index == 5 and ob.confirmed_index == 7
    assert ob.bottom == 99.5 and ob.top == 101.5 and ob.bos_level == 105
    assert ob.body_top == 101 and ob.body_bottom == 100
    assert ob.has_fvg  # c1 = indice 5 (high 101.5) / c3 = indice 7 (low 102.5)


def test_displacement_filter():
    obs = detect_order_blocks(_ob_series(), OBParams(atr_period=3, min_displacement_atr=50))
    assert obs == []


def test_ob_not_visible_before_bos():
    cs = _ob_series()
    assert detect_order_blocks(cs[:7], OBParams(atr_period=3, min_displacement_atr=0.5)) == []


@pytest.mark.parametrize("seed", [4, 8])
def test_ob_no_lookahead_prefix_invariance(seed):
    cs = random_walk(500, seed=seed)
    p = OBParams(min_displacement_atr=0.5)
    full = detect_order_blocks(cs, p)
    assert full
    for cut in (100, 260, 499):
        prefix = detect_order_blocks(cs[: cut + 1], p)
        key = lambda xs: [(o.bullish, o.index, o.confirmed_index, o.top, o.bottom, o.displacement_atr, o.has_fvg) for o in xs]
        assert key(prefix) == key([o for o in full if o.confirmed_index <= cut])
