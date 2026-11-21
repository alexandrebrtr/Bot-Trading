"""FRED sans clé : export CSV public ``fredgraph.csv`` (dernière vintage seulement).

Sert quand ``FRED_API_KEY`` manque (environnement de backtest, nouvelle machine).
Limites, à garder en tête en lisant un backtest qui l'utilise :

- **Pas de vintages** : les valeurs sont celles d'aujourd'hui, révisions
  comprises (NFP, PIB, PCE sont révisés). C'est un léger look-ahead sur le
  *niveau* des séries révisées, pas sur leur *date* de disponibilité.
- **Date de publication estimée** : l'export ne donne pas la date de sortie.
  ``available_at`` = début de la période + délai de publication prudent
  (``PUBLICATION_LAG``, majorant du calendrier habituel de l'agence), puis
  l'heure officielle de ``fred.RELEASE_TIMES_ET``. On publie donc **plus tard**
  que la réalité, jamais plus tôt.
- Pas de calendrier des publications (fenêtres d'annonce absentes du backtest).

Les lignes vont dans ``macro.fredgraph.<id>`` (jamais mélangées aux vintages
ALFRED de ``macro.fred.<id>``) ; ``StorePITData`` ne les lit qu'à défaut des
vintages.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta

from bot_trading.marketdata.common import UTC, Provenance, end_of_day_et, et_to_utc, sha256_bytes, utc_now
from bot_trading.marketdata.http import PublicSourceClient
from bot_trading.marketdata.sources import fred

SOURCE = "fred_graph"
HOSTS = ("fred.stlouisfed.org",)
BASE = "https://fred.stlouisfed.org/graph/fredgraph.csv"
LICENSE = fred.LICENSE + " Export fredgraph.csv : dernière vintage, dates de publication estimées."
KEYS = ("series_id", "observation_date")


def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


# (mois après le début de la période, jours en plus) : majorant du calendrier habituel.
PUBLICATION_LAG: dict[str, tuple[int, int]] = {
    "CPIAUCSL": (1, 20), "CPILFESL": (1, 20),             # BLS CPI : ~10-15 du mois suivant
    "PCEPI": (1, 35), "PCEPILFE": (1, 35), "PCE": (1, 35),  # BEA : fin du mois suivant
    "UNRATE": (1, 10), "PAYEMS": (1, 10),                 # BLS : 1er vendredi du mois suivant
    "INDPRO": (1, 20),                                    # Fed G.17 : ~15-17 du mois suivant
    "RSAFS": (1, 20),                                     # Census : ~15 du mois suivant
    "FEDFUNDS": (1, 5),                                   # premiers jours du mois suivant
    "GDPC1": (3, 35),                                     # BEA : estimation avancée ~30 j après le trimestre
    "ICSA": (0, 7),                                       # semaine close le samedi, publiée le jeudi suivant
    "DTWEXBGS": (0, 8),                                   # H.10 hebdomadaire (lundi)
}
DAILY_LAG_DAYS = 1  # séries quotidiennes (taux, VIX, spread HY) : lendemain, fin de journée New York


def estimated_release(series_id: str, period: date) -> date:
    lag = PUBLICATION_LAG.get(series_id)
    if lag is None:
        return period + timedelta(days=DAILY_LAG_DAYS)
    months, days = lag
    return _add_months(period, months) + timedelta(days=days) if months else period + timedelta(days=days)


def available_at(series_id: str, period: date) -> datetime:
    d = estimated_release(series_id, period)
    hm = fred.RELEASE_TIMES_ET.get(series_id)
    return et_to_utc(d, *hm) if hm else end_of_day_et(d)


def parse_csv(series_id: str, raw: bytes) -> list[dict]:
    out = []
    reader = csv.reader(io.StringIO(raw.decode("utf-8-sig")))
    header = next(reader, None)
    if not header or len(header) < 2:
        raise ValueError(f"{series_id} : CSV FRED inattendu")
    for row in reader:
        if len(row) < 2 or not row[0]:
            continue
        try:
            obs = date.fromisoformat(row[0][:10])
        except ValueError:
            continue
        rel = estimated_release(series_id, obs)
        out.append({
            "series_id": series_id,
            "observation_date": obs,
            "realtime_start": rel,
            "realtime_end": None,
            "value": fred._value(row[1].strip()),
            "event_time": datetime(obs.year, obs.month, obs.day, tzinfo=UTC),
            "available_at": available_at(series_id, obs),
        })
    return out


class FredGraphSource:
    def __init__(self, client: PublicSourceClient | None = None, clock=utc_now) -> None:
        self.client = client or PublicSourceClient(SOURCE, HOSTS, rate_per_second=1.0)
        self.clock = clock

    def observations(self, series_id: str) -> tuple[list[dict], Provenance]:
        now = self.clock()
        raw = self.client.get(BASE, params={"id": series_id}).content
        rows = [r for r in parse_csv(series_id, raw) if r["available_at"] <= now]
        prov = Provenance(SOURCE, f"macro.fredgraph.{series_id.lower()}", BASE, now, {"id": series_id}, sha256_bytes(raw), LICENSE)
        return rows, prov
