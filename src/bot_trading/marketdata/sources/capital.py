"""Bougies US500 depuis Capital.com, **en lecture seule**.

- Historique REST : ``CapitalReadOnlyClient.prices`` (transport bridé en liste
  blanche), paginé par fenêtres ``from``/``to`` de 1000 bougies au plus.
- Temps réel : voir ``capital_stream.py``.

Seules les bougies **closes** sont renvoyées : ``available_at = ouverture + durée``.
La profondeur d'historique par résolution n'est pas documentée (⚠️ à mesurer :
une fenêtre antérieure à l'historique disponible revient simplement vide).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from bot_trading.broker.capital_readonly import LIVE_BASE_URL, MAX_CANDLES_PER_REQUEST, CapitalReadOnlyClient, PriceBar
from bot_trading.core.models import Timeframe
from bot_trading.marketdata.bars import bar_record, candles_dataset, closed_only
from bot_trading.marketdata.common import Provenance, ensure_utc, sha256_bytes, utc_now

SOURCE = "capital"
LICENSE = "Capital.com : usage personnel, ne pas redistribuer (CGU)."


def price_bars_to_records(epic: str, tf: Timeframe, bars: list[PriceBar]) -> list[dict]:
    return [bar_record(epic, tf, b.bid, b.ask) for b in bars]


class CapitalCandleSource:
    def __init__(self, client: CapitalReadOnlyClient, epic: str = "US500", clock=utc_now) -> None:
        self.client = client
        self.epic = epic
        self.clock = clock

    def fetch(
        self,
        tf: Timeframe,
        start: datetime,
        end: datetime | None = None,
    ) -> tuple[list[dict], Provenance]:
        now = self.clock()
        start = ensure_utc(start)
        end = ensure_utc(end) if end else now
        window = timedelta(minutes=tf.minutes * MAX_CANDLES_PER_REQUEST)
        seen: dict[datetime, dict] = {}
        cursor = start
        requests_made = 0
        step = timedelta(minutes=tf.minutes)
        while cursor < end:
            stop = min(cursor + window, end)
            bars = self.client.prices(self.epic, tf, MAX_CANDLES_PER_REQUEST, cursor, stop)
            requests_made += 1
            for r in price_bars_to_records(self.epic, tf, bars):
                if start <= r["event_time"] < end:
                    seen[r["event_time"]] = r
            # Réponse pleine : l'API a pu tronquer la fenêtre, on repart après la dernière bougie.
            if len(bars) >= MAX_CANDLES_PER_REQUEST and bars[-1].time + step > cursor:
                cursor = bars[-1].time + step
            else:
                cursor = stop
        records = closed_only(sorted(seen.values(), key=lambda r: r["event_time"]), now)
        prov = Provenance(
            source=SOURCE,
            dataset=candles_dataset(SOURCE, self.epic, tf),
            url=f"{LIVE_BASE_URL}/api/v1/prices/{self.epic}",
            fetched_at=now,
            params={"resolution": tf.capital_resolution, "from": start.isoformat(), "to": end.isoformat(), "requests": requests_made},
            payload_sha256=sha256_bytes(json.dumps([r["event_time"].isoformat() for r in records]).encode()),
            license_note=LICENSE,
        )
        return records, prov
