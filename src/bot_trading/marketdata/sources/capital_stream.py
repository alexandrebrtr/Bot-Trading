"""Flux temps réel Capital.com (WebSocket), **réception de prix uniquement**.

Doc officielle (section *Streaming API*), lue d'après un résumé le 20/11/2026 :
- URL ``STREAMING_URL`` (constante de ``broker/capital_readonly.py``, seul module à connaître les hôtes) ;
- messages JSON ``{"destination", "correlationId", "cst", "securityToken", "payload"}`` ;
- ``OHLCMarketData.subscribe`` avec ``payload = {"epics", "resolutions", "type": "classic"}`` ;
  événements ``destination = "ohlc.event"`` avec ``payload = {epic, resolution,
  priceType (bid/ask), t (ms), o, h, l, c}`` ;
- ``marketData.subscribe`` → événements ``quote`` (``bid``, ``ofr``, ``timestamp``) ;
- ``ping`` au moins toutes les 10 minutes ; 40 instruments max.

⚠️ Les noms de champs des événements sont à revalider sur un flux réel.

Garde-fous :
- seul l'hôte ``STREAMING_URL`` (défini dans ``broker/capital_readonly.py``) est accepté ;
- toute destination hors ``ALLOWED_STREAM_DESTINATIONS`` est refusée avant l'envoi ;
- les messages reçus sont des **données** : seuls des nombres et des
  identifiants attendus en sont extraits.

Seules les bougies **closes** sont émises : une bougie (epic, résolution, côté)
est close quand un événement d'horodatage plus récent arrive. Les deux côtés
(bid et ask) sont réunis avant émission.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from bot_trading.broker.capital_readonly import ALLOWED_STREAM_DESTINATIONS, STREAMING_URL
from bot_trading.core.models import Candle, Quote, Timeframe
from bot_trading.marketdata.bars import bar_record

RESOLUTION_TO_TF = {tf.capital_resolution: tf for tf in Timeframe}
MAX_STREAM_EPICS = 40
PING_INTERVAL_S = 9 * 60


class ForbiddenStreamMessageError(PermissionError):
    pass


class Connection(Protocol):
    def send(self, data: str) -> None: ...
    def recv(self) -> str: ...
    def close(self) -> None: ...


def default_connection_factory(url: str) -> Connection:  # pragma: no cover - réseau
    if url != STREAMING_URL:
        raise ForbiddenStreamMessageError(f"URL de flux non autorisée : {url}")
    import websocket  # websocket-client, extra "stream"

    return websocket.create_connection(url, timeout=30)


def _num(v) -> float:
    x = float(v)
    if not math.isfinite(x):
        raise ValueError("valeur non finie")
    return x


@dataclass
class _Partial:
    t: datetime
    o: float
    h: float
    l: float  # noqa: E741
    c: float


@dataclass
class OhlcAssembler:
    """Transforme des événements ``ohlc.event`` en bougies closes bid/ask."""

    open_bars: dict[tuple[str, str, str], _Partial] = field(default_factory=dict)
    closed_sides: dict[tuple[str, str, datetime], dict[str, Candle]] = field(default_factory=dict)

    def on_event(self, payload: dict) -> list[dict]:
        epic = str(payload["epic"])
        res = str(payload["resolution"])
        side = str(payload.get("priceType", "bid")).lower()
        if res not in RESOLUTION_TO_TF or side not in ("bid", "ask"):
            return []
        t = datetime.fromtimestamp(int(payload["t"]) / 1000, tz=timezone.utc)
        bar = _Partial(t, _num(payload["o"]), _num(payload["h"]), _num(payload["l"]), _num(payload["c"]))
        key = (epic, res, side)
        cur = self.open_bars.get(key)
        out: list[dict] = []
        if cur is not None and bar.t > cur.t:
            out.extend(self._close_side(epic, res, side, cur))
        if cur is None or bar.t >= cur.t:
            self.open_bars[key] = bar
        return out

    def _close_side(self, epic: str, res: str, side: str, p: _Partial) -> list[dict]:
        candle = Candle(p.t, p.o, max(p.h, p.o, p.c), min(p.l, p.o, p.c), p.c)
        k = (epic, res, p.t)
        sides = self.closed_sides.setdefault(k, {})
        sides[side] = candle
        if "bid" in sides and "ask" in sides:
            del self.closed_sides[k]
            return [bar_record(epic, RESOLUTION_TO_TF[res], sides["bid"], sides["ask"])]
        return []


def parse_quote(payload: dict) -> Quote:
    ts = datetime.fromtimestamp(int(payload["timestamp"]) / 1000, tz=timezone.utc)
    return Quote(ts, _num(payload["bid"]), _num(payload["ofr"]))


class CapitalStream:
    """Abonnement aux bougies et cotations. Aucune méthode d'ordre n'existe ici."""

    def __init__(
        self,
        tokens: Callable[[], tuple[str, str]],
        connection_factory: Callable[[str], Connection] = default_connection_factory,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._tokens = tokens
        self._factory = connection_factory
        self._clock = clock
        self._conn: Connection | None = None
        self._corr = 0
        self._last_ping = 0.0
        self.assembler = OhlcAssembler()
        self.sent: list[dict] = []

    # ---------------------------------------------------------------- envoi
    def _send(self, destination: str, payload: dict | None = None) -> None:
        if destination not in ALLOWED_STREAM_DESTINATIONS:
            raise ForbiddenStreamMessageError(f"Destination refusée : {destination}")
        if self._conn is None:
            raise RuntimeError("Flux non connecté")
        cst, sec = self._tokens()
        self._corr += 1
        msg = {"destination": destination, "correlationId": str(self._corr), "cst": cst, "securityToken": sec}
        if payload is not None:
            msg["payload"] = payload
        self._conn.send(json.dumps(msg))
        self.sent.append({"destination": destination, "payload": payload})  # jamais les jetons

    def connect(self) -> None:
        self._conn = self._factory(STREAMING_URL)
        self._last_ping = self._clock()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def subscribe_ohlc(self, epics: list[str], timeframes: list[Timeframe]) -> None:
        if not 1 <= len(epics) <= MAX_STREAM_EPICS:
            raise ValueError(f"1 à {MAX_STREAM_EPICS} epics")
        self._send(
            "OHLCMarketData.subscribe",
            {"epics": epics, "resolutions": [tf.capital_resolution for tf in timeframes], "type": "classic"},
        )

    def subscribe_quotes(self, epics: list[str]) -> None:
        if not 1 <= len(epics) <= MAX_STREAM_EPICS:
            raise ValueError(f"1 à {MAX_STREAM_EPICS} epics")
        self._send("marketData.subscribe", {"epics": epics})

    def ping_if_due(self) -> bool:
        if self._clock() - self._last_ping >= PING_INTERVAL_S:
            self._send("ping")
            self._last_ping = self._clock()
            return True
        return False

    # ------------------------------------------------------------- réception
    def handle(self, raw: str) -> tuple[list[dict], Quote | None]:
        """Traite un message brut. Renvoie (bougies closes, cotation éventuelle).
        Les messages illisibles ou inattendus sont ignorés."""
        try:
            msg = json.loads(raw)
            dest = msg.get("destination")
            payload = msg.get("payload") or {}
            if dest == "ohlc.event":
                return self.assembler.on_event(payload), None
            if dest == "quote":
                return [], parse_quote(payload)
        except (ValueError, KeyError, TypeError, AttributeError):
            pass
        return [], None

    def events(self, max_messages: int | None = None) -> Iterator[tuple[list[dict], Quote | None]]:
        if self._conn is None:
            self.connect()
        n = 0
        while max_messages is None or n < max_messages:
            self.ping_if_due()
            raw = self._conn.recv()  # type: ignore[union-attr]
            n += 1
            yield self.handle(raw)
