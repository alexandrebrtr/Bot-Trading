"""Adaptateur Capital.com en **lecture seule**.

Le seul compte API disponible est un compte **réel**. Ce module est donc conçu
pour qu'aucun ordre ne puisse partir, même par erreur de programmation :

1. Le client n'expose aucune méthode de trading (pas de positions, d'ordres,
   de confirmations, de préférences de compte ni de changement de compte).
2. Toutes les requêtes passent par ``ReadOnlyGuardAdapter``, monté sur la
   session HTTP pour ``http://`` et ``https://``. Il vérifie chaque requête
   **avant** l'envoi réseau contre une liste blanche (méthode + chemin exact)
   et lève ``ForbiddenEndpointError`` sinon. Même un appel direct à
   la session HTTP (``client.http``) vers un endpoint de trading est bloqué.
3. Les redirections sont désactivées et l'hôte doit être un hôte Capital.com connu.

Endpoints de la documentation officielle (https://open-api.capital.com/),
lue le 20/11/2026 :
- ``GET /api/v1/time``, ``GET /api/v1/ping``
- ``GET /api/v1/session/encryptionKey``, ``GET/POST/DELETE /api/v1/session``
  (authentification : header ``X-CAP-API-KEY``, corps ``identifier`` / ``password``,
  jetons ``CST`` et ``X-SECURITY-TOKEN`` renvoyés en headers, session de 10 minutes)
- ``GET /api/v1/markets``, ``GET /api/v1/markets/{epic}``
- ``GET /api/v1/marketnavigation``, ``GET /api/v1/marketnavigation/{nodeId}``
- ``GET /api/v1/prices/{epic}`` (``resolution``, ``max`` ≤ 1000, ``from``, ``to``
  au format ``YYYY-MM-DDTHH:MM:SS``)

Endpoints **interdits** (écriture ou effet de bord) : ``/api/v1/positions*``,
``/api/v1/workingorders*``, ``/api/v1/confirms*``, ``PUT /api/v1/session``,
``/api/v1/accounts*``, ``/api/v1/history*``, ``/api/v1/watchlists*``.
``PUT /api/v1/session`` (changement de compte) est exclu volontairement.
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests
from requests.adapters import BaseAdapter, HTTPAdapter

from bot_trading.core.models import Candle, Timeframe

LIVE_BASE_URL = "https://api-capital.backend-capital.com"
DEMO_BASE_URL = "https://demo-api-capital.backend-capital.com"
ALLOWED_HOSTS = frozenset({"api-capital.backend-capital.com", "demo-api-capital.backend-capital.com"})

_EPIC = r"[A-Za-z0-9._-]+"
# (méthode, regex du chemin complet). Toute autre combinaison est refusée.
ALLOWED_ENDPOINTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("GET", re.compile(r"^/api/v1/time$")),
    ("GET", re.compile(r"^/api/v1/ping$")),
    ("GET", re.compile(r"^/api/v1/session/encryptionKey$")),
    ("GET", re.compile(r"^/api/v1/session$")),
    ("POST", re.compile(r"^/api/v1/session$")),
    ("DELETE", re.compile(r"^/api/v1/session$")),
    ("GET", re.compile(r"^/api/v1/markets$")),
    ("GET", re.compile(rf"^/api/v1/markets/{_EPIC}$")),
    ("GET", re.compile(r"^/api/v1/marketnavigation$")),
    ("GET", re.compile(r"^/api/v1/marketnavigation/[A-Za-z0-9._-]+$")),
    ("GET", re.compile(rf"^/api/v1/prices/{_EPIC}$")),
)

# Double barrière : ces fragments sont refusés quelle que soit la liste blanche.
FORBIDDEN_FRAGMENTS = ("positions", "workingorders", "confirms", "accounts", "topup", "history", "watchlists")

# WebSocket temps réel (doc officielle, section Streaming API). Le flux ne sert qu'à
# recevoir des prix : seules ces destinations peuvent être envoyées
# (vérifié par ``marketdata.sources.capital_stream``).
STREAMING_URL = "wss://api-streaming-capital.backend-capital.com/connect"
ALLOWED_STREAM_DESTINATIONS = frozenset(
    {"marketData.subscribe", "marketData.unsubscribe", "OHLCMarketData.subscribe", "OHLCMarketData.unsubscribe", "ping"}
)

RESOLUTIONS = ("MINUTE", "MINUTE_5", "MINUTE_15", "MINUTE_30", "HOUR", "HOUR_4", "DAY", "WEEK")
MAX_CANDLES_PER_REQUEST = 1000


class ForbiddenEndpointError(PermissionError):
    """Levée avant tout envoi réseau pour une requête hors liste blanche."""


def is_request_allowed(method: str, url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS:
        return False
    path = parts.path
    lowered = path.lower()
    if any(f in lowered for f in FORBIDDEN_FRAGMENTS):
        return False
    method = method.upper()
    return any(m == method and rx.match(path) for m, rx in ALLOWED_ENDPOINTS)


class ReadOnlyGuardAdapter(BaseAdapter):
    """Adaptateur de transport qui filtre chaque requête avant de la déléguer."""

    def __init__(self, inner: BaseAdapter | None = None) -> None:
        super().__init__()
        self.inner = inner or HTTPAdapter()
        self.blocked: list[tuple[str, str]] = []

    def send(self, request, **kwargs):  # type: ignore[override]
        if not is_request_allowed(request.method or "", request.url or ""):
            self.blocked.append((request.method or "", request.url or ""))
            raise ForbiddenEndpointError(
                f"Requête bloquée (lecture seule) : {request.method} {urlsplit(request.url or '').path}"
            )
        return self.inner.send(request, **kwargs)

    def close(self) -> None:
        self.inner.close()


def build_guarded_session(inner: BaseAdapter | None = None) -> tuple[requests.Session, ReadOnlyGuardAdapter]:
    session = requests.Session()
    guard = ReadOnlyGuardAdapter(inner)
    session.adapters.clear()
    session.mount("https://", guard)
    session.mount("http://", guard)
    session.max_redirects = 0
    return session, guard


@dataclass(frozen=True)
class CapitalCredentials:
    api_key: str
    identifier: str
    password: str

    @classmethod
    def from_env(cls) -> "CapitalCredentials":
        missing = [k for k in ("CAPITAL_API_KEY", "CAPITAL_IDENTIFIER", "CAPITAL_API_PASSWORD") if not os.getenv(k)]
        if missing:
            raise RuntimeError(f"Variables d'environnement manquantes : {', '.join(missing)}")
        return cls(os.environ["CAPITAL_API_KEY"], os.environ["CAPITAL_IDENTIFIER"], os.environ["CAPITAL_API_PASSWORD"])

    def __repr__(self) -> str:  # ne jamais afficher les secrets
        return "CapitalCredentials(api_key=***, identifier=***, password=***)"


class RateLimiter:
    """Limiteur simple : au plus ``rate`` appels par seconde (doc : 10/s par utilisateur)."""

    def __init__(self, rate: float, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> None:
        self.interval = 1.0 / rate
        self.clock = clock
        self.sleep = sleep
        self._next = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = self.clock()
            if now < self._next:
                self.sleep(self._next - now)
                now = self._next
            self._next = now + self.interval


@dataclass(frozen=True)
class PriceBar:
    """Bougie Capital.com avec les deux côtés du carnet."""

    bid: Candle
    ask: Candle

    @property
    def time(self) -> datetime:
        return self.bid.time

    @property
    def spread_close(self) -> float:
        return self.ask.close - self.bid.close


def _parse_time(value: str) -> datetime:
    value = value.rstrip("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Horodatage illisible : {value!r}")


def parse_prices(payload: dict) -> list[PriceBar]:
    """Convertit la réponse de ``GET /api/v1/prices/{epic}`` en ``PriceBar``.

    Champs utilisés : ``prices[].snapshotTimeUTC`` (à défaut ``snapshotTime``),
    ``openPrice``, ``highPrice``, ``lowPrice``, ``closePrice`` avec ``bid`` / ``ask``,
    et ``lastTradedVolume`` s'il est présent. À revalider sur une réponse réelle.
    """
    bars: list[PriceBar] = []
    for p in payload.get("prices", []):
        ts = _parse_time(p.get("snapshotTimeUTC") or p["snapshotTime"])
        vol = float(p.get("lastTradedVolume") or 0.0)

        def side(name: str) -> Candle:
            return Candle(
                time=ts,
                open=float(p["openPrice"][name]),
                high=float(p["highPrice"][name]),
                low=float(p["lowPrice"][name]),
                close=float(p["closePrice"][name]),
                volume=vol,
            )

        bars.append(PriceBar(bid=side("bid"), ask=side("ask")))
    bars.sort(key=lambda b: b.time)
    return bars


class CapitalReadOnlyClient:
    """Client de données Capital.com. Aucune méthode de trading n'existe ici."""

    def __init__(
        self,
        credentials: CapitalCredentials,
        base_url: str = LIVE_BASE_URL,
        transport: BaseAdapter | None = None,
        rate_limiter: RateLimiter | None = None,
        timeout: float = 10.0,
    ) -> None:
        host = urlsplit(base_url).hostname
        if host not in ALLOWED_HOSTS:
            raise ValueError(f"Hôte non autorisé : {host}")
        self.base_url = base_url.rstrip("/")
        self._creds = credentials
        self.http, self.guard = build_guarded_session(transport)
        self.limiter = rate_limiter or RateLimiter(10)
        self.timeout = timeout
        self._cst: str | None = None
        self._security_token: str | None = None

    # ------------------------------------------------------------ transport
    def _request(self, method: str, path: str, *, auth: bool = True, retry: bool = True, **kwargs) -> requests.Response:
        headers = kwargs.pop("headers", {})
        headers["X-CAP-API-KEY"] = self._creds.api_key
        if auth:
            if self._cst is None:
                self.login()
            headers["CST"] = self._cst
            headers["X-SECURITY-TOKEN"] = self._security_token
        self.limiter.acquire()
        resp = self.http.request(
            method, f"{self.base_url}{path}", headers=headers, timeout=self.timeout, allow_redirects=False, **kwargs
        )
        if resp.status_code == 401 and auth and retry:
            self._cst = None
            return self._request(method, path, auth=auth, retry=False, headers=headers, **kwargs)
        resp.raise_for_status()
        return resp

    # -------------------------------------------------------------- session
    def login(self) -> None:
        resp = self._request(
            "POST",
            "/api/v1/session",
            auth=False,
            json={"identifier": self._creds.identifier, "password": self._creds.password},
        )
        self._cst = resp.headers.get("CST")
        self._security_token = resp.headers.get("X-SECURITY-TOKEN")
        if not self._cst or not self._security_token:
            raise RuntimeError("Jetons CST / X-SECURITY-TOKEN absents de la réponse")

    def logout(self) -> None:
        if self._cst:
            self._request("DELETE", "/api/v1/session")
        self._cst = self._security_token = None

    def session_tokens(self) -> tuple[str, str]:
        """Jetons ``CST`` / ``X-SECURITY-TOKEN`` pour le WebSocket (connexion si besoin)."""
        if self._cst is None:
            self.login()
        return self._cst, self._security_token  # type: ignore[return-value]

    def ping(self) -> dict:
        return self._request("GET", "/api/v1/ping").json()

    def server_time(self) -> dict:
        return self._request("GET", "/api/v1/time", auth=False).json()

    # -------------------------------------------------------------- marchés
    def market(self, epic: str) -> dict:
        return self._request("GET", f"/api/v1/markets/{epic}").json()

    def search_markets(self, search_term: str) -> dict:
        return self._request("GET", "/api/v1/markets", params={"searchTerm": search_term}).json()

    def prices(
        self,
        epic: str,
        resolution: str | Timeframe,
        max_candles: int = MAX_CANDLES_PER_REQUEST,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[PriceBar]:
        res = resolution.capital_resolution if isinstance(resolution, Timeframe) else resolution
        if res not in RESOLUTIONS:
            raise ValueError(f"Résolution inconnue : {res}")
        if not 1 <= max_candles <= MAX_CANDLES_PER_REQUEST:
            raise ValueError(f"max doit être entre 1 et {MAX_CANDLES_PER_REQUEST}")
        params: dict[str, str | int] = {"resolution": res, "max": max_candles}
        if start is not None:
            params["from"] = start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        if end is not None:
            params["to"] = end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        return parse_prices(self._request("GET", f"/api/v1/prices/{epic}", params=params).json())
