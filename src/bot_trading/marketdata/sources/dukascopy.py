"""Historique long US500 depuis le datafeed public Dukascopy (backtest).

Format (client open source dukascopy-node / theorycraft, non documenté
officiellement par Dukascopy) :

- URL ``https://datafeed.dukascopy.com/datafeed/{SYMBOL}/{AAAA}/{MM-1}/{JJ}/{BID|ASK}_candles_min_1.bi5``
  (mois **commençant à 00**) ;
- fichier compressé LZMA (format « alone »), enregistrements de 24 octets
  big-endian : secondes depuis minuit UTC (uint32), open, close, low, high
  (uint32, prix × ``price_divisor``), volume (float32) ;
- fichier vide = pas de cotation (week-end, jour férié).

⚠️ À vérifier : le symbole de l'US 500 (``USA500IDXUSD`` par défaut) et le
diviseur de prix des indices (1000 par défaut). Le test de cohérence le plus
simple : comparer une clôture D1 avec Capital.com sur la même date.

Prix **Dukascopy**, pas Capital.com : spread, horaires et ajustements
différents. À calibrer sur la période commune avant tout backtest.
Licence des données non trouvée : usage personnel / recherche.
"""

from __future__ import annotations

import lzma
import struct
from datetime import date, datetime, time, timedelta, timezone

from bot_trading.core.models import Candle, Timeframe
from bot_trading.marketdata.bars import bar_record, candles_dataset, closed_only
from bot_trading.marketdata.candles import resample
from bot_trading.marketdata.common import Provenance, daterange_days, sha256_bytes, utc_now
from bot_trading.marketdata.http import PublicSourceClient, SourceHTTPError

SOURCE = "dukascopy"
HOSTS = ("datafeed.dukascopy.com",)
BASE = "https://datafeed.dukascopy.com/datafeed"
DEFAULT_SYMBOL = "USA500IDXUSD"
DEFAULT_PRICE_DIVISOR = 1000.0
LICENSE = "Dukascopy : conditions des données non trouvées, usage personnel/recherche uniquement."
_REC = struct.Struct(">IIIIIf")


class SourceUnavailableError(RuntimeError):
    """Trop d'échecs consécutifs : la source est considérée comme injoignable pour cette collecte."""


def day_url(symbol: str, day: date, side: str) -> str:
    return f"{BASE}/{symbol}/{day.year:04d}/{day.month - 1:02d}/{day.day:02d}/{side.upper()}_candles_min_1.bi5"


def parse_minute_candles(raw: bytes, day: date, price_divisor: float = DEFAULT_PRICE_DIVISOR) -> list[Candle]:
    if not raw:
        return []
    data = lzma.decompress(raw)
    if len(data) % _REC.size:
        raise ValueError("Fichier bi5 tronqué")
    midnight = datetime.combine(day, time(0), tzinfo=timezone.utc)
    out: list[Candle] = []
    for secs, o, c, lo, h, vol in _REC.iter_unpack(data):
        if vol == 0 and o == c == lo == h:
            continue  # minute sans cotation
        out.append(
            Candle(midnight + timedelta(seconds=secs), o / price_divisor, h / price_divisor, lo / price_divisor, c / price_divisor, float(vol))
        )
    return out


def merge_sides(epic: str, tf: Timeframe, bid: list[Candle], ask: list[Candle]) -> list[dict]:
    asks = {c.time: c for c in ask}
    return [bar_record(epic, tf, b, asks.get(b.time)) for b in bid]


class DukascopySource:
    def __init__(
        self,
        client: PublicSourceClient | None = None,
        symbol: str = DEFAULT_SYMBOL,
        epic: str = "US500",
        price_divisor: float = DEFAULT_PRICE_DIVISOR,
        clock=utc_now,
        rate_per_second: float = 1.0,
        max_consecutive_skips: int = 5,
    ) -> None:
        # Le datafeed renvoie 503 ou ne répond plus quand on va trop vite :
        # débit modéré et attentes longues (jusqu'à 1 min) avant d'abandonner un jour.
        self.client = client or PublicSourceClient(SOURCE, HOSTS, rate_per_second=rate_per_second, max_attempts=7,
                                                   backoff_base=2.0, backoff_max=60.0, timeout=20.0)
        self.skipped: list[tuple[date, str]] = []  # jours abandonnés après toutes les tentatives (à relancer)
        self.max_consecutive_skips = max_consecutive_skips
        self._consecutive = 0
        self.symbol = symbol
        self.epic = epic
        self.price_divisor = price_divisor
        self.clock = clock

    def _day(self, day: date, side: str) -> tuple[list[Candle], bytes]:
        try:
            raw = self.client.get(day_url(self.symbol, day, side)).content
        except SourceHTTPError as e:
            if e.status == 404:
                return [], b""
            if e.status in self.client.RETRY_STATUSES or e.status == self.client.NO_RESPONSE:
                # Serveur saturé ou muet malgré les tentatives : jour sauté et signalé, la collecte continue.
                # Relancer la même commande le complète (dédoublonnage du store).
                self.skipped.append((day, side.upper()))
                self._consecutive += 1
                if self._consecutive >= self.max_consecutive_skips:
                    raise SourceUnavailableError(
                        f"Dukascopy injoignable : {self._consecutive} échecs d'affilée (dernier : {e}). "
                        "Les tranches déjà collectées sont enregistrées ; réessayer plus tard avec --resume."
                    ) from e
                return [], b""
            raise
        self._consecutive = 0
        return parse_minute_candles(raw, day, self.price_divisor), raw

    def fetch(self, start: date, end: date, tf: Timeframe = Timeframe.M5) -> tuple[list[dict], Provenance]:
        """Bougies ``tf`` (M5 à D1) agrégées depuis les minutes, closes uniquement."""
        return self.fetch_many(start, end, [tf])[tf]

    def fetch_many(self, start: date, end: date, tfs) -> dict:
        """Comme ``fetch`` pour plusieurs timeframes, en ne téléchargeant les minutes qu'une fois."""
        now = self.clock()
        bid_all: list[Candle] = []
        ask_all: list[Candle] = []
        digest = []
        for day in daterange_days(start, end):
            n_skipped = len(self.skipped)
            b, rb = self._day(day, "BID")
            a, ra = self._day(day, "ASK")
            if len(self.skipped) > n_skipped:
                continue  # un côté manquant : jour entier laissé en trou plutôt que sans ask
            bid_all += b
            ask_all += a
            digest.append(sha256_bytes(rb + ra))
        out = {}
        for tf in tfs:
            # resample n'émet la dernière bougie que si elle est complète.
            records = closed_only(merge_sides(self.epic, tf, resample(bid_all, tf), resample(ask_all, tf)), now)
            out[tf] = (records, self._prov(tf, start, end, now, digest))
        return out

    def _prov(self, tf, start, end, now, digest) -> Provenance:
        return Provenance(
            source=SOURCE,
            dataset=candles_dataset(SOURCE, self.epic, tf),
            url=f"{BASE}/{self.symbol}",
            fetched_at=now,
            params={"symbol": self.symbol, "start": start.isoformat(), "end": end.isoformat(), "divisor": self.price_divisor},
            payload_sha256=sha256_bytes("".join(digest).encode()),
            license_note=LICENSE,
        )
