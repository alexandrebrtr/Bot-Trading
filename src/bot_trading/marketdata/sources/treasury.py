"""Courbe des taux du Trésor US (par yield curve), quotidienne.

Doc : https://home.treasury.gov/treasury-daily-interest-rate-xml-feed. Flux
XML (Atom + OData) : ``entry/content/m:properties`` avec ``d:NEW_DATE``,
``d:BC_1MONTH`` … ``d:BC_2YEAR`` … ``d:BC_10YEAR`` … ``d:BC_30YEAR``.
Paramètres : ``data=daily_treasury_yield_curve`` et ``field_tdr_date_value=AAAA``
ou ``field_tdr_date_value_month=AAAAMM``. Sans clé.

Disponibilité : publiée en fin de journée (⚠️ heure exacte non documentée ici) ;
``available_at`` = 18:00 New York le jour même par prudence (paramétrable).
"""

from __future__ import annotations

from datetime import date, datetime

from bot_trading.marketdata.common import Provenance, et_to_utc, sha256_bytes, utc_now
from bot_trading.marketdata.http import PublicSourceClient
from bot_trading.marketdata.sources.xmlsafe import local, parse_xml

SOURCE = "treasury"
HOSTS = ("home.treasury.gov",)
URL = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml"
DATASET = "rates.treasury.par_curve"
KEYS = ("date",)
LICENSE = "Trésor US : donnée publique."
DEFAULT_AVAILABLE_ET = (18, 0)
TENORS = {
    "BC_1MONTH": "y_1m", "BC_2MONTH": "y_2m", "BC_3MONTH": "y_3m", "BC_4MONTH": "y_4m", "BC_6MONTH": "y_6m",
    "BC_1YEAR": "y_1y", "BC_2YEAR": "y_2y", "BC_3YEAR": "y_3y", "BC_5YEAR": "y_5y", "BC_7YEAR": "y_7y",
    "BC_10YEAR": "y_10y", "BC_20YEAR": "y_20y", "BC_30YEAR": "y_30y",
}


def parse_yield_curve_xml(raw: bytes, available_et: tuple[int, int] = DEFAULT_AVAILABLE_ET) -> list[dict]:
    root = parse_xml(raw)
    out = []
    for props in root.iter():
        if local(props.tag) != "properties":
            continue
        fields = {local(c.tag): (c.text or "").strip() for c in props}
        if not fields.get("NEW_DATE"):
            continue
        d = date.fromisoformat(fields["NEW_DATE"][:10])
        rec: dict = {"date": d}
        for k, col in TENORS.items():
            v = fields.get(k)
            rec[col] = float(v) if v else None
        rec["spread_10y_2y"] = (rec["y_10y"] - rec["y_2y"]) if rec["y_10y"] is not None and rec["y_2y"] is not None else None
        rec["event_time"] = et_to_utc(d, 0)
        rec["available_at"] = et_to_utc(d, *available_et)
        out.append(rec)
    return sorted(out, key=lambda r: r["date"])


class TreasurySource:
    def __init__(self, client: PublicSourceClient | None = None, clock=utc_now) -> None:
        self.client = client or PublicSourceClient(SOURCE, HOSTS, rate_per_second=1)
        self.clock = clock

    def fetch_year(self, year: int) -> tuple[list[dict], Provenance]:
        params = {"data": "daily_treasury_yield_curve", "field_tdr_date_value": str(year)}
        now: datetime = self.clock()
        raw = self.client.get(URL, params=params).content
        rows = [r for r in parse_yield_curve_xml(raw) if r["available_at"] <= now]
        return rows, Provenance(SOURCE, DATASET, URL, now, params, sha256_bytes(raw), LICENSE)
