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

    def __init__(self, source: str, status: int, path: str, detail: str = "") -> None:
        super().__init__(f"{source} : HTTP {status} sur {path}" + (f" ({detail})" if detail else ""))
        self.source = source
        self.status = status
        self.path = path
        self.detail = detail


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
    NO_RESPONSE = 0  # ``SourceHTTPError.status`` quand le serveur n'a pas répondu (délai, connexion refusée)

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
        backoff_base: float = 1.0,
        backoff_max: float = 60.0,
        error_detail: Callable[[requests.Response], str] | None = None,
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
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.error_detail = error_detail

    def get(self, url: str, params: dict | None = None, headers: dict | None = None) -> requests.Response:
        path = urlsplit(url).path
        last_status = 0
        last_detail = ""
        for attempt in range(self.max_attempts):
            self.limiter.acquire()
            try:
                resp = self.http.get(url, params=params, headers=headers, timeout=self.timeout, allow_redirects=False)
            except (requests.ConnectionError, requests.Timeout) as e:
                # Délai de connexion ou de lecture, connexion coupée : même traitement qu'un 503.
                # Statut 0 = pas de réponse HTTP. Le message ne cite que le type d'erreur (pas d'URL).
                last_status, last_detail = 0, f"pas de réponse : {type(e).__name__}"
                if attempt + 1 < self.max_attempts:
                    self.sleep(min(self.backoff_base * 2.0**attempt, self.backoff_max))
                continue
            if resp.status_code in self.RETRY_STATUSES and attempt + 1 < self.max_attempts:
                last_status = resp.status_code
                self.sleep(min(self.backoff_base * 2.0**attempt, self.backoff_max))
                continue
            if resp.status_code >= 400:
                detail = ""
                if self.error_detail is not None:
                    try:
                        detail = self.error_detail(resp)[:300]
                    except Exception:  # noqa: BLE001 - le détail est facultatif
                        detail = ""
                raise SourceHTTPError(self.source, resp.status_code, path, detail)
            return resp
        raise SourceHTTPError(self.source, last_status, path, last_detail)
