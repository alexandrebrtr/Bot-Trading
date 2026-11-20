"""Client HTTP des sources publiques gratuites : lecture seule, hôtes en liste blanche.

Chaque source déclare ses hôtes. Le transport refuse, **avant** tout envoi :
- toute méthode autre que ``GET`` ;
- tout schéma autre que ``https`` ;
- tout hôte absent de la liste de la source.

Les erreurs ne contiennent jamais l'URL complète (la clé FRED passe en paramètre
de requête) : seul le chemin est cité.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from urllib.parse import urlsplit

import requests
from requests.adapters import BaseAdapter, HTTPAdapter

from bot_trading.broker.capital_readonly import RateLimiter


class SourceBlockedError(PermissionError):
    """Requête refusée par le garde-fou de la source (méthode, schéma ou hôte)."""


class SourceHTTPError(RuntimeError):
    """Erreur HTTP d'une source, sans secret dans le message."""

    def __init__(self, source: str, status: int, path: str) -> None:
        super().__init__(f"{source} : HTTP {status} sur {path}")
        self.source = source
        self.status = status
        self.path = path


class PublicGuardAdapter(BaseAdapter):
    def __init__(self, allowed_hosts: Iterable[str], inner: BaseAdapter | None = None) -> None:
        super().__init__()
        self.allowed_hosts = frozenset(allowed_hosts)
        self.inner = inner or HTTPAdapter()
        self.blocked: list[tuple[str, str]] = []

    def send(self, request, **kwargs):  # type: ignore[override]
        parts = urlsplit(request.url or "")
        method = (request.method or "").upper()
        if method != "GET" or parts.scheme != "https" or parts.hostname not in self.allowed_hosts:
            self.blocked.append((method, f"{parts.scheme}://{parts.hostname}{parts.path}"))
            raise SourceBlockedError(f"Requête bloquée : {method} {parts.scheme}://{parts.hostname}{parts.path}")
        return self.inner.send(request, **kwargs)

    def close(self) -> None:
        self.inner.close()


class PublicSourceClient:
    """Session GET-only avec limiteur de débit et quelques tentatives sur 429/5xx."""

    RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

    def __init__(
        self,
        source: str,
        allowed_hosts: Iterable[str],
        *,
        rate_per_second: float,
        user_agent: str = "bot-trading-data/0.1",
        transport: BaseAdapter | None = None,
        timeout: float = 30.0,
        max_attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.source = source
        self.guard = PublicGuardAdapter(allowed_hosts, transport)
        self.http = requests.Session()
        self.http.adapters.clear()
        self.http.mount("https://", self.guard)
        self.http.mount("http://", self.guard)
        self.http.max_redirects = 0
        self.http.headers["User-Agent"] = user_agent
        self.limiter = RateLimiter(rate_per_second, sleep=sleep)
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.sleep = sleep

    def get(self, url: str, params: dict | None = None, headers: dict | None = None) -> requests.Response:
        path = urlsplit(url).path
        last_status = 0
        for attempt in range(self.max_attempts):
            self.limiter.acquire()
            resp = self.http.get(url, params=params, headers=headers, timeout=self.timeout, allow_redirects=False)
            if resp.status_code in self.RETRY_STATUSES and attempt + 1 < self.max_attempts:
                last_status = resp.status_code
                self.sleep(2.0**attempt)
                continue
            if resp.status_code >= 400:
                raise SourceHTTPError(self.source, resp.status_code, path)
            return resp
        raise SourceHTTPError(self.source, last_status, path)
