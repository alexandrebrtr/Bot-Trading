"""VIX quotidien depuis Cboe (CSV depuis 1990).

URL indiquée par https://www.cboe.com/tradable_products/vix/vix_historical_data :
``https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv``
(⚠️ issue d'un résumé de page, à tester). Colonnes ``DATE,OPEN,HIGH,LOW,CLOSE``,
dates ``MM/DD/YYYY``. Même schéma pour VIX9D, VIX3M, VVIX.

Disponibilité : clôture VIX calculée vers 16:15 New York, fichier mis à jour
ensuite. Par prudence, ``available_at`` = 18:00 New York le jour même
(paramétrable). Conditions : « for the convenience of site visitors », usage perso.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime

from bot_trading.marketdata.common import Provenance, et_to_utc, sha256_bytes, utc_now
from bot_trading.marketdata.http import PublicSourceClient

SOURCE = "cboe"
HOSTS = ("cdn-api.cboe.com",)
BASE = "https://cdn-api.cboe.com/api/global/us_indices/daily_prices"
KEYS = ("symbol", "event_time")
LICENSE = "Cboe : données fournies pour commodité, sans garantie ; usage personnel."
DEFAULT_AVAILABLE_ET = (18, 0)


def parse_history_csv(text: str, symbol: str = "VIX", available_et: tuple[int, int] = DEFAULT_AVAILABLE_ET) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    out = []
    for row in reader:
        row = {k.strip().upper(): (v or "").strip() for k, v in row.items() if k}
        if not row.get("DATE") or not row.get("CLOSE"):
            continue
        d = datetime.strptime(row["DATE"], "%m/%d/%Y").date()
        out.append(
            {
                "symbol": symbol,
                "date": d,
                "open": float(row["OPEN"]) if row.get("OPEN") else None,
                "high": float(row["HIGH"]) if row.get("HIGH") else None,
                "low": float(row["LOW"]) if row.get("LOW") else None,
                "close": float(row["CLOSE"]),
                "event_time": et_to_utc(d, 9, 30),
                "available_at": et_to_utc(d, *available_et),
            }
        )
    return out


class CboeSource:
    def __init__(self, client: PublicSourceClient | None = None, clock=utc_now) -> None:
        self.client = client or PublicSourceClient(SOURCE, HOSTS, rate_per_second=1)
        self.clock = clock

    def fetch(self, symbol: str = "VIX", since: date | None = None) -> tuple[list[dict], Provenance]:
        url = f"{BASE}/{symbol}_History.csv"
        now = self.clock()
        raw = self.client.get(url).content
        rows = [r for r in parse_history_csv(raw.decode("utf-8", "replace"), symbol) if since is None or r["date"] >= since]
        # Le fichier du jour peut contenir une ligne en cours de séance : on ne garde que le connu.
        rows = [r for r in rows if r["available_at"] <= now]
        return rows, Provenance(SOURCE, f"market.cboe.{symbol.lower()}", url, now, {"symbol": symbol}, sha256_bytes(raw), LICENSE)
