from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from bot_trading.core.models import Candle

T0 = datetime(2026, 11, 2, 14, 0, tzinfo=timezone.utc)  # lundi


def make_candles(ohlc: list[tuple[float, float, float, float]], start: datetime = T0, minutes: int = 5) -> list[Candle]:
    return [Candle(start + timedelta(minutes=minutes * i), o, h, l, c) for i, (o, h, l, c) in enumerate(ohlc)]


def random_walk(n: int, seed: int = 7, start_price: float = 6800.0) -> list[Candle]:
    rng = random.Random(seed)
    out = []
    price = start_price
    for i in range(n):
        o = price
        c = o + rng.gauss(0.2, 6)
        h = max(o, c) + abs(rng.gauss(0, 3))
        l = min(o, c) - abs(rng.gauss(0, 3))
        out.append(Candle(T0 + timedelta(minutes=5 * i), o, h, l, c))
        # gaps occasionnels pour produire des FVG
        price = c + (rng.choice([0, 0, 0, 8, -8]))
    return out


@pytest.fixture
def walk():
    return random_walk(600)
