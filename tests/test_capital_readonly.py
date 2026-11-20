"""L'adaptateur Capital.com ne doit jamais pouvoir envoyer d'ordre.

Aucun test ne touche le réseau : le transport est un faux adaptateur qui
enregistre les requêtes qui l'atteignent.
"""

import inspect
import json
from datetime import datetime, timezone

import pytest
import requests
from requests.adapters import BaseAdapter

from bot_trading.broker import capital_readonly as cr
from bot_trading.broker.capital_readonly import (
    CapitalCredentials,
    CapitalReadOnlyClient,
    ForbiddenEndpointError,
    RateLimiter,
    is_request_allowed,
    parse_prices,
)
from bot_trading.core.models import Timeframe

BASE = cr.LIVE_BASE_URL

PRICES_PAYLOAD = {
    "prices": [
        {
            "snapshotTime": "2026/11/20 15:05:00",
            "snapshotTimeUTC": "2026-11-20T14:05:00",
            "openPrice": {"bid": 6801.0, "ask": 6801.6},
            "closePrice": {"bid": 6803.0, "ask": 6803.6},
            "highPrice": {"bid": 6804.0, "ask": 6804.6},
            "lowPrice": {"bid": 6800.0, "ask": 6800.6},
            "lastTradedVolume": 120,
        },
        {
            "snapshotTime": "2026/11/20 15:00:00",
            "snapshotTimeUTC": "2026-11-20T14:00:00",
            "openPrice": {"bid": 6800.0, "ask": 6800.6},
            "closePrice": {"bid": 6801.0, "ask": 6801.6},
            "highPrice": {"bid": 6802.0, "ask": 6802.6},
            "lowPrice": {"bid": 6799.0, "ask": 6799.6},
        },
    ]
}


class FakeTransport(BaseAdapter):
    def __init__(self, routes=None):
        super().__init__()
        self.sent: list[requests.PreparedRequest] = []
        self.routes = routes or {}
        self.login_calls = 0
        self.expire_next = False

    def send(self, request, **kwargs):
        self.sent.append(request)
        path = request.path_url.split("?")[0]
        resp = requests.Response()
        resp.request = request
        resp.url = request.url
        resp.status_code = 200
        if request.method == "POST" and path == "/api/v1/session":
            self.login_calls += 1
            resp.headers["CST"] = f"cst-{self.login_calls}"
            resp.headers["X-SECURITY-TOKEN"] = f"xst-{self.login_calls}"
            body = {}
        elif self.expire_next and request.method == "GET":
            self.expire_next = False
            resp.status_code = 401
            body = {"errorCode": "error.invalid.session.token"}
        else:
            body = self.routes.get(path, {"status": "OK"})
        resp._content = json.dumps(body).encode()
        return resp

    def close(self):
        pass


CREDS = CapitalCredentials("key", "user@example.invalid", "pw")


def client(transport=None):
    return CapitalReadOnlyClient(CREDS, transport=transport or FakeTransport(), rate_limiter=RateLimiter(1e9))


# ------------------------------------------------------------ liste blanche
@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/v1/positions"),
        ("GET", "/api/v1/positions"),
        ("PUT", "/api/v1/positions/D1"),
        ("DELETE", "/api/v1/positions/D1"),
        ("POST", "/api/v1/workingorders"),
        ("PUT", "/api/v1/workingorders/D1"),
        ("DELETE", "/api/v1/workingorders/D1"),
        ("GET", "/api/v1/confirms/o_123"),
        ("PUT", "/api/v1/session"),
        ("GET", "/api/v1/accounts"),
        ("PUT", "/api/v1/accounts/preferences"),
        ("POST", "/api/v1/accounts/topUp"),
        ("GET", "/api/v1/history/activity"),
        ("POST", "/api/v1/watchlists"),
        ("POST", "/api/v1/prices/US500"),
        ("GET", "/api/v1/prices/US500/../../positions"),
        ("GET", "/api/v1/markets/US500/positions"),
        ("GET", "/API/V1/POSITIONS"),
    ],
)
def test_forbidden_requests(method, path):
    assert not is_request_allowed(method, BASE + path)


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/v1/time"),
        ("GET", "/api/v1/ping"),
        ("POST", "/api/v1/session"),
        ("DELETE", "/api/v1/session"),
        ("GET", "/api/v1/session/encryptionKey"),
        ("GET", "/api/v1/markets"),
        ("GET", "/api/v1/markets/US500"),
        ("GET", "/api/v1/marketnavigation/123"),
        ("GET", "/api/v1/prices/US500?resolution=HOUR&max=10"),
    ],
)
def test_allowed_requests(method, path):
    assert is_request_allowed(method, BASE + path)


def test_unknown_host_and_plain_http_refused():
    assert not is_request_allowed("GET", "https://evil.example.com/api/v1/prices/US500")
    assert not is_request_allowed("GET", "http://api-capital.backend-capital.com/api/v1/prices/US500")
    with pytest.raises(ValueError):
        CapitalReadOnlyClient(CREDS, base_url="https://evil.example.com", transport=FakeTransport())


# --------------------------------------------------------- garde transport
@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
def test_direct_http_order_never_reaches_network(method):
    t = FakeTransport()
    c = client(t)
    with pytest.raises(ForbiddenEndpointError):
        c.http.request(method, BASE + "/api/v1/positions", json={"epic": "US500", "direction": "BUY", "size": 0.01})
    with pytest.raises(ForbiddenEndpointError):
        c.http.request("POST", BASE + "/api/v1/workingorders", json={})
    assert t.sent == []
    assert c.guard.blocked


def test_private_request_helper_also_blocked():
    t = FakeTransport()
    c = client(t)
    with pytest.raises(ForbiddenEndpointError):
        c._request("POST", "/api/v1/positions", json={})
    # seul le login (autorisé) a pu passer
    assert [r.path_url for r in t.sent] == ["/api/v1/session"]


def test_mounting_new_adapter_on_https_is_still_guarded_by_default_session():
    # La session n'a que l'adaptateur de garde ; aucun HTTPAdapter brut n'est monté.
    c = client()
    assert all(isinstance(a, cr.ReadOnlyGuardAdapter) for a in c.http.adapters.values())


def test_client_has_no_trading_methods():
    names = {n.lower() for n, _ in inspect.getmembers(CapitalReadOnlyClient, inspect.isfunction)}
    for bad in ("order", "position", "deal", "trade", "buy", "sell", "close", "confirm", "switch", "topup"):
        assert not any(bad in n for n in names), bad


# --------------------------------------------------------------- lecture
def test_login_headers_and_prices():
    t = FakeTransport({"/api/v1/prices/US500": PRICES_PAYLOAD})
    c = client(t)
    bars = c.prices("US500", Timeframe.M5, max_candles=2, start=datetime(2026, 11, 20, 14, tzinfo=timezone.utc))
    login, req = t.sent
    assert login.method == "POST" and json.loads(login.body) == {"identifier": "user@example.invalid", "password": "pw"}
    assert login.headers["X-CAP-API-KEY"] == "key"
    assert req.headers["CST"] == "cst-1" and req.headers["X-SECURITY-TOKEN"] == "xst-1"
    assert "resolution=MINUTE_5" in req.url and "max=2" in req.url and "from=2026-11-20T14%3A00%3A00" in req.url
    assert [b.time.minute for b in bars] == [0, 5]
    assert bars[0].bid.open == 6800.0 and bars[0].ask.close == 6801.6
    assert bars[1].spread_close == pytest.approx(0.6)


def test_relogin_once_on_401():
    t = FakeTransport({"/api/v1/markets/US500": {"instrument": {"epic": "US500"}}})
    c = client(t)
    c.ping()
    t.expire_next = True
    assert c.market("US500")["instrument"]["epic"] == "US500"
    assert t.login_calls == 2


def test_prices_parameter_validation():
    c = client()
    with pytest.raises(ValueError):
        c.prices("US500", "MINUTE_3")
    with pytest.raises(ValueError):
        c.prices("US500", "HOUR", max_candles=1001)


def test_parse_prices_without_volume():
    bars = parse_prices(PRICES_PAYLOAD)
    assert bars[0].bid.volume == 0 and bars[1].bid.volume == 120


def test_credentials_never_printed(monkeypatch):
    monkeypatch.setenv("CAPITAL_API_KEY", "secret-key")
    monkeypatch.setenv("CAPITAL_IDENTIFIER", "me")
    monkeypatch.setenv("CAPITAL_API_PASSWORD", "secret-pw")
    creds = CapitalCredentials.from_env()
    assert "secret" not in repr(creds) and "secret" not in str(creds)


def test_missing_credentials(monkeypatch):
    for k in ("CAPITAL_API_KEY", "CAPITAL_IDENTIFIER", "CAPITAL_API_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(RuntimeError):
        CapitalCredentials.from_env()


def test_rate_limiter_spaces_calls():
    now = [0.0]
    slept = []
    rl = RateLimiter(10, clock=lambda: now[0], sleep=lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)))
    for _ in range(3):
        rl.acquire()
    assert slept == pytest.approx([0.1, 0.1])
