"""Bougies Capital.com : historique REST paginé et flux WebSocket, sans réseau."""

import json
from datetime import datetime, timedelta, timezone

import pytest
import requests
from requests.adapters import BaseAdapter

from bot_trading.broker.capital_readonly import (
    STREAMING_URL,
    CapitalCredentials,
    CapitalReadOnlyClient,
    RateLimiter,
)
from bot_trading.core.models import Timeframe
from bot_trading.marketdata.engine import DataEngine
from bot_trading.marketdata.sources.capital import CapitalCandleSource
from bot_trading.marketdata.sources.capital_stream import CapitalStream, ForbiddenStreamMessageError
from bot_trading.marketdata.storage import ParquetStore

UTC = timezone.utc
START = datetime(2026, 11, 18, 14, 0, tzinfo=UTC)  # mercredi


def price(t, base):
    return {
        "snapshotTimeUTC": t.strftime("%Y-%m-%dT%H:%M:%S"),
        "openPrice": {"bid": base, "ask": base + 0.6},
        "closePrice": {"bid": base + 1, "ask": base + 1.6},
        "highPrice": {"bid": base + 2, "ask": base + 2.6},
        "lowPrice": {"bid": base - 1, "ask": base - 0.4},
    }


class PricesTransport(BaseAdapter):
    """Simule /prices : renvoie les bougies H1 dans [from, to), au plus `max`."""

    def __init__(self, last_open):
        super().__init__()
        self.last_open = last_open
        self.calls = []

    def send(self, request, **kwargs):
        resp = requests.Response()
        resp.request, resp.url, resp.status_code = request, request.url, 200
        if request.method == "POST":
            resp.headers.update({"CST": "c", "X-SECURITY-TOKEN": "x"})
            body = {}
        else:
            from urllib.parse import parse_qs, urlsplit

            q = parse_qs(urlsplit(request.url).query)
            frm = datetime.fromisoformat(q["from"][0]).replace(tzinfo=UTC)
            to = datetime.fromisoformat(q["to"][0]).replace(tzinfo=UTC)
            self.calls.append((frm, to))
            bars, t = [], frm
            while t < to and t <= self.last_open and len(bars) < int(q["max"][0]):
                bars.append(price(t, 6800 + (t - START).total_seconds() / 3600))
                t += timedelta(hours=1)
            body = {"prices": bars}
        resp._content = json.dumps(body).encode()
        return resp

    def close(self):
        pass


def capital_client(transport):
    return CapitalReadOnlyClient(CapitalCredentials("k", "u@example.invalid", "p"), transport=transport, rate_limiter=RateLimiter(1e9))


def test_history_drops_open_candle_and_stores(tmp_path):
    now = START + timedelta(hours=5, minutes=30)  # bougie de 19:00 encore ouverte
    t = PricesTransport(last_open=START + timedelta(hours=5))
    src = CapitalCandleSource(capital_client(t), clock=lambda: now)
    rows, prov = src.fetch(Timeframe.H1, START)
    assert [r["event_time"] for r in rows] == [START + timedelta(hours=i) for i in range(5)]
    assert all(r["available_at"] <= now for r in rows)
    assert rows[0]["bid_close"] == 6801 and rows[0]["ask_close"] == pytest.approx(6801.6) and rows[0]["close"] == pytest.approx(6801.3)
    assert prov.dataset == "candles.capital.us500.h1" and prov.params["resolution"] == "HOUR"
    eng = DataEngine(ParquetStore(tmp_path), clock=lambda: now)
    eng.ingest_candles(rows, prov, Timeframe.H1)
    candles = eng.candles("capital", "US500", Timeframe.H1, as_of=START + timedelta(hours=3))
    assert len(candles) == 3  # seules les bougies closes à 17:00 sont visibles
    assert eng.quality(prov.dataset, now).ok


def test_history_paginates_when_window_full(monkeypatch):
    import bot_trading.marketdata.sources.capital as cap

    monkeypatch.setattr(cap, "MAX_CANDLES_PER_REQUEST", 3)
    monkeypatch.setattr("bot_trading.broker.capital_readonly.MAX_CANDLES_PER_REQUEST", 3)
    now = START + timedelta(hours=10)
    t = PricesTransport(last_open=START + timedelta(hours=9))
    rows, _ = CapitalCandleSource(capital_client(t), clock=lambda: now).fetch(Timeframe.H1, START)
    assert len(rows) == 10 and len({r["event_time"] for r in rows}) == 10
    assert len(t.calls) >= 4


# -------------------------------------------------------------- WebSocket
class FakeConn:
    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.out = []
        self.closed = False

    def send(self, data):
        self.out.append(json.loads(data))

    def recv(self):
        return self.incoming.pop(0)

    def close(self):
        self.closed = True


def ohlc(t, side, o, c, res="MINUTE_5"):
    return json.dumps({"status": "OK", "destination": "ohlc.event", "payload": {
        "resolution": res, "epic": "US500", "type": "classic", "priceType": side,
        "t": int(t.timestamp() * 1000), "o": o, "h": max(o, c) + 1, "l": min(o, c) - 1, "c": c}})


def test_stream_emits_only_closed_bars_with_both_sides():
    t0 = START
    msgs = [
        ohlc(t0, "bid", 6800, 6801), ohlc(t0, "ask", 6800.6, 6801.6),
        ohlc(t0, "bid", 6800, 6803),  # mise à jour de la bougie en cours
        "pas du json", json.dumps({"destination": "ohlc.event", "payload": {"epic": "US500"}}),
        ohlc(t0 + timedelta(minutes=5), "bid", 6803, 6804),  # bid de t0 clos
        ohlc(t0 + timedelta(minutes=5), "ask", 6803.6, 6804.6),  # ask de t0 clos -> émission
        json.dumps({"destination": "quote", "payload": {"epic": "US500", "bid": 6804, "ofr": 6804.6, "timestamp": 1763474700000}}),
    ]
    conns = []

    def factory(url):
        assert url == STREAMING_URL
        conns.append(FakeConn(msgs))
        return conns[-1]

    clock = iter([0, 0] + [0] * 6 + [600, 600, 600])
    s = CapitalStream(lambda: ("CST", "XST"), factory, clock=lambda: next(clock))
    s.connect()
    s.subscribe_ohlc(["US500"], [Timeframe.M5, Timeframe.H1])
    sub = conns[0].out[0]
    assert sub["destination"] == "OHLCMarketData.subscribe" and sub["cst"] == "CST"
    assert sub["payload"] == {"epics": ["US500"], "resolutions": ["MINUTE_5", "HOUR"], "type": "classic"}
    bars, quotes = [], []
    for b, q in s.events(max_messages=len(msgs)):
        bars += b
        quotes += [q] if q else []
    assert len(bars) == 1
    b = bars[0]
    assert b["event_time"] == t0 and b["bid_close"] == 6803 and b["ask_close"] == 6801.6
    assert b["available_at"] == t0 + timedelta(minutes=5)
    assert quotes and quotes[0].spread == pytest.approx(0.6)
    assert any(m["destination"] == "ping" for m in conns[0].out)  # ping après 9 minutes
    assert all("cst" not in m for m in s.sent)  # les jetons ne sont pas journalisés


def test_stream_refuses_other_destinations():
    s = CapitalStream(lambda: ("a", "b"), lambda url: FakeConn([]), clock=lambda: 0)
    s.connect()
    for dest in ("positions.create", "workingorders", "trade", "OHLCMarketData.subscribe "):
        with pytest.raises(ForbiddenStreamMessageError):
            s._send(dest, {})
    with pytest.raises(ValueError):
        s.subscribe_quotes([f"E{i}" for i in range(41)])
    assert not [n for n in dir(s) if any(w in n.lower() for w in ("order", "position", "deal", "trade"))]
