"""SEC EDGAR : dépôts et faits XBRL des grosses pondérations du S&P 500.

Doc : https://www.sec.gov/search-filings/edgar-application-programming-interfaces
et https://www.sec.gov/os/accessing-edgar-data.
- ``https://data.sec.gov/submissions/CIK##########.json`` : dépôts récents
  (``filings.recent`` en colonnes : ``accessionNumber``, ``form``, ``filingDate``,
  ``acceptanceDateTime``, ``reportDate``, ``primaryDocument``, ``items``) ;
- ``https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`` : faits XBRL
  (``facts.<taxonomie>.<concept>.units.<unité>[]`` avec ``end``, ``val``, ``fy``,
  ``fp``, ``form``, ``filed``, ``accn``) ;
- ``https://www.sec.gov/files/company_tickers.json`` : correspondance ticker → CIK.

Sans clé, 10 requêtes/s max, ``User-Agent`` obligatoire « Nom contact@email » :
lu depuis ``SEC_USER_AGENT`` (``.env``), jamais dans le code.

Disponibilité (point-in-time) :
- dépôt : ``acceptanceDateTime``. ⚠️ Le suffixe ``Z`` est trompeur selon plusieurs
  utilisateurs (heure de New York) : on l'interprète comme heure de New York,
  ce qui est le choix prudent (plus tard que l'UTC) ;
- fait XBRL : acceptation du dépôt ``accn`` si connue, sinon 23:59:59 New York
  le jour ``filed``.
Ne contient pas d'estimations d'analystes. Textes marqués ``untrusted_external``.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from datetime import date, datetime

from bot_trading.marketdata.common import (
    NEW_YORK,
    UNTRUSTED,
    UTC,
    Provenance,
    clean_external_text,
    end_of_day_et,
    sha256_bytes,
    utc_now,
)
from bot_trading.marketdata.http import PublicSourceClient

SOURCE = "sec_edgar"
HOSTS = ("data.sec.gov", "www.sec.gov")
FILINGS_DATASET = "fundamentals.edgar.filings"
FACTS_DATASET = "fundamentals.edgar.facts"
FILINGS_KEYS = ("accession_number",)
FACTS_KEYS = ("cik", "taxonomy", "concept", "unit", "period_start", "period_end", "accession_number")
LICENSE = "SEC EDGAR : donnée publique, 10 req/s, User-Agent déclaré."

# Concepts utiles à l'analyse (chiffre d'affaires, bénéfices, marges).
DEFAULT_CONCEPTS = (
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "NetIncomeLoss",
    "OperatingIncomeLoss",
    "GrossProfit",
    "EarningsPerShareDiluted",
)
WATCHED_FORMS = frozenset({"10-K", "10-Q", "8-K", "10-K/A", "10-Q/A", "8-K/A"})


class MissingUserAgentError(RuntimeError):
    pass


def user_agent_from_env() -> str:
    ua = os.getenv("SEC_USER_AGENT", "").strip()
    if not ua or "@" not in ua:
        raise MissingUserAgentError("SEC_USER_AGENT absent ou sans email de contact (à définir dans .env)")
    return ua


def cik10(cik: int | str) -> str:
    return f"{int(cik):010d}"


def parse_acceptance(value: str) -> datetime:
    naive = datetime.fromisoformat(value.rstrip("Z").split(".")[0])
    return naive.replace(tzinfo=NEW_YORK).astimezone(UTC)


def parse_submissions(payload: dict, forms: Iterable[str] | None = WATCHED_FORMS) -> list[dict]:
    cik = int(payload["cik"])
    name = clean_external_text(payload.get("name"), 200)
    recent = payload.get("filings", {}).get("recent", {})
    n = len(recent.get("accessionNumber", []))
    keep = set(forms) if forms is not None else None
    out = []
    for i in range(n):
        form = recent["form"][i]
        if keep is not None and form not in keep:
            continue
        filed = date.fromisoformat(recent["filingDate"][i])
        acc = recent.get("acceptanceDateTime", [None] * n)[i]
        available = parse_acceptance(acc) if acc else end_of_day_et(filed)
        report = recent.get("reportDate", [""] * n)[i]
        out.append(
            {
                "cik": cik,
                "company": name,
                "accession_number": recent["accessionNumber"][i],
                "form": form,
                "filing_date": filed,
                "report_date": date.fromisoformat(report) if report else None,
                "primary_document": clean_external_text(recent.get("primaryDocument", [""] * n)[i], 300),
                "items": clean_external_text(recent.get("items", [""] * n)[i], 200),
                "event_time": available,
                "available_at": available,
                "trust": UNTRUSTED,
            }
        )
    return out


def parse_companyfacts(
    payload: dict,
    concepts: Iterable[str] | None = DEFAULT_CONCEPTS,
    acceptance_by_accn: dict[str, datetime] | None = None,
) -> list[dict]:
    cik = int(payload["cik"])
    wanted = set(concepts) if concepts is not None else None
    acceptance_by_accn = acceptance_by_accn or {}
    out = []
    for taxonomy, items in payload.get("facts", {}).items():
        for concept, body in items.items():
            if wanted is not None and concept not in wanted:
                continue
            for unit, facts in body.get("units", {}).items():
                for f in facts:
                    end = date.fromisoformat(f["end"])
                    filed = date.fromisoformat(f["filed"])
                    accn = f.get("accn", "")
                    available = acceptance_by_accn.get(accn) or end_of_day_et(filed)
                    out.append(
                        {
                            "cik": cik,
                            "taxonomy": taxonomy,
                            "concept": concept,
                            "unit": unit,
                            "period_start": date.fromisoformat(f["start"]) if f.get("start") else None,
                            "period_end": end,
                            "value": float(f["val"]),
                            "fiscal_year": f.get("fy"),
                            "fiscal_period": f.get("fp"),
                            "form": f.get("form"),
                            "filed": filed,
                            "accession_number": accn,
                            "event_time": datetime(end.year, end.month, end.day, tzinfo=UTC),
                            "available_at": available,
                        }
                    )
    return out


def parse_company_tickers(payload: dict) -> dict[str, int]:
    return {str(v["ticker"]).upper(): int(v["cik_str"]) for v in payload.values()}


class EdgarSource:
    def __init__(self, client: PublicSourceClient | None = None, user_agent: str | None = None, clock=utc_now) -> None:
        self.client = client or PublicSourceClient(SOURCE, HOSTS, rate_per_second=8, user_agent=user_agent or user_agent_from_env())
        self.clock = clock

    def tickers(self) -> dict[str, int]:
        return parse_company_tickers(self.client.get("https://www.sec.gov/files/company_tickers.json").json())

    def filings(self, cik: int) -> tuple[list[dict], Provenance]:
        url = f"https://data.sec.gov/submissions/CIK{cik10(cik)}.json"
        now = self.clock()
        resp = self.client.get(url)
        rows = [r for r in parse_submissions(resp.json()) if r["available_at"] <= now]
        return rows, Provenance(SOURCE, FILINGS_DATASET, url, now, {"cik": cik}, sha256_bytes(resp.content), LICENSE)

    def facts(self, cik: int, acceptance_by_accn: dict[str, datetime] | None = None) -> tuple[list[dict], Provenance]:
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10(cik)}.json"
        now = self.clock()
        resp = self.client.get(url)
        rows = [r for r in parse_companyfacts(resp.json(), acceptance_by_accn=acceptance_by_accn) if r["available_at"] <= now]
        return rows, Provenance(SOURCE, FACTS_DATASET, url, now, {"cik": cik}, sha256_bytes(resp.content), LICENSE)
