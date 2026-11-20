from bot_trading.core.models import Timeframe
from bot_trading.data.candles import load_csv, resample, write_csv

from conftest import random_walk


def test_resample_m5_to_h1_drops_incomplete_last_bucket():
    cs = random_walk(30)  # 14:00 → 16:25, la dernière heure est incomplète
    h1 = resample(cs, Timeframe.H1)
    assert len(h1) == 2
    first = h1[0]
    bucket = cs[:12]
    assert first.open == bucket[0].open and first.close == bucket[-1].close
    assert first.high == max(c.high for c in bucket) and first.low == min(c.low for c in bucket)
    assert len(resample(cs, Timeframe.H1, include_partial=True)) == 3


def test_h4_buckets_aligned_on_utc():
    h4 = resample(random_walk(200), Timeframe.H4, include_partial=True)
    assert all(c.time.hour % 4 == 0 and c.time.minute == 0 for c in h4)


def test_csv_roundtrip(tmp_path):
    cs = random_walk(20)
    p = tmp_path / "c.csv"
    write_csv(p, cs)
    assert load_csv(p) == cs
