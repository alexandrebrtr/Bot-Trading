"""Faux transports HTTP pour les tests du Data Engine (aucun réseau)."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import requests
from requests.adapters import BaseAdapter

from bot_trading.marketdata.http import PublicSourceClient


class RouteTransport(BaseAdapter):
    """Répond selon (hôte + chemin) ; enregistre les requêtes reçues."""

    def __init__(self, routes: dict[str, object] | None = None, handler=None) -> None:
        super().__init__()
        self.routes = routes or {}
        self.handler = handler
        self.sent: list[requests.PreparedRequest] = []

    def send(self, request, **kwargs):
        self.sent.append(request)
        parts = urlsplit(request.url)
        key = f"{parts.hostname}{parts.path}"
        resp = requests.Response()
        resp.request = request
        resp.url = request.url
        resp.status_code = 200
        body = self.handler(parts.path, parse_qs(parts.query)) if self.handler else self.routes.get(key)
        if body is None:
            resp.status_code = 404
            body = b""
        if isinstance(body, tuple):
            resp.status_code, body = body
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        if isinstance(body, str):
            body = body.encode()
        resp._content = body
        return resp

    def close(self):
        pass


def client(source: str, hosts, transport: BaseAdapter) -> PublicSourceClient:
    return PublicSourceClient(source, hosts, rate_per_second=1e9, transport=transport, sleep=lambda s: None)
