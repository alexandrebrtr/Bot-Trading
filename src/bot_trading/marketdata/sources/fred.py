"""FRED / ALFRED en point-in-time, avec les dates de publication.

Doc officielle FRED API (lue via DATA_SOURCES.md, 20/11/2026) :
- ``/fred/series/observations`` avec ``realtime_start`` / ``realtime_end`` :
  avec ``realtime_start=1776-07-04`` et ``realtime_end=9999-12-31``, chaque
  ligne porte la période de validité de la valeur (toutes les vintages) ;
- ``/fred/series/vintagedates`` : dates de publication ou de révision ;
- ``/fred/release/dates`` avec ``include_release_dates_with_no_data=true`` :
  calendrier, dates futures incluses ;
- ``file_type=json``, ``limit`` ≤ 100000 avec ``offset`` ; ``"."`` = valeur absente.

Clé : ``FRED_API_KEY`` (fichier ``.env``). Elle n'apparaît jamais dans les
URL stockées, la traçabilité ou les messages d'erreur.

Horodatage de disponibilité : FRED ne donne qu'une **date** de vintage. On y
ajoute l'heure officielle de publication de l'agence (``RELEASE_TIMES_ET``,
heure de New York, ⚠️ à vérifier sur les calendriers BLS/BEA/Fed) ; sans heure
connue, on prend 23:59:59 New York (prudent). Attention : pour l'historique
ancien, la première vintage ALFRED peut être postérieure à la vraie
publication (date d'entrée dans ALFRED) : c'est prudent, jamais en avance.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from bot_trading.marketdata.common import UTC, Provenance, end_of_day_et, ensure_utc, et_to_utc, sha256_bytes, utc_now
from bot_trading.marketdata.http import PublicSourceClient

SOURCE = "fred"
HOSTS = ("api.stlouisfed.org",)
BASE = "https://api.stlouisfed.org/fred"
REALTIME_MIN = "1776-07-04"
REALTIME_MAX = "9999-12-31"
PAGE_LIMIT = 100000
LICENSE = "FRED : licence variable selon la série (voir notes de série) ; séries ICE BofA usage interne seulement."

# Séries suivies (stratégie §4). ISM : propriétaire, absent de FRED.
SERIES = {
    "CPIAUCSL": ("CPI", "monthly"),
    "CPILFESL": ("Core CPI", "monthly"),
    "PCEPI": ("PCE price index", "monthly"),
    "PCEPILFE": ("Core PCE price index", "monthly"),
    "UNRATE": ("Taux de chômage", "monthly"),
    "PAYEMS": ("NFP (emplois non agricoles)", "monthly"),
    "ICSA": ("Jobless claims initiales", "weekly"),
    "GDPC1": ("PIB réel", "quarterly"),
    "INDPRO": ("Production industrielle", "monthly"),
    "PCE": ("Consommation des ménages", "monthly"),
    "RSAFS": ("Retail sales", "monthly"),
    "FEDFUNDS": ("Fed funds effectif", "monthly"),
    "DGS2": ("Taux 2 ans", "daily"),
    "DGS10": ("Taux 10 ans", "daily"),
    "T10Y2Y": ("Pente 10 ans - 2 ans", "daily"),
    "DTWEXBGS": ("Dollar broad (proxy DXY)", "daily"),
    "BAMLH0A0HYM2": ("Spread high yield ICE BofA (usage interne)", "daily"),
    "VIXCLS": ("VIX (copie FRED)", "daily"),
}

# Heure de publication (New York) par série. ⚠️ À vérifier sur les calendriers des agences.
RELEASE_TIMES_ET: dict[str, tuple[int, int]] = {
    "CPIAUCSL": (8, 30), "CPILFESL": (8, 30),           # BLS CPI
    "PCEPI": (8, 30), "PCEPILFE": (8, 30), "PCE": (8, 30),  # BEA Personal Income and Outlays
    "UNRATE": (8, 30), "PAYEMS": (8, 30),              # BLS Employment Situation
    "ICSA": (8, 30),                                   # DOL weekly claims
    "GDPC1": (8, 30),                                  # BEA GDP
    "RSAFS": (8, 30),                                  # Census retail sales
    "INDPRO": (9, 15),                                 # Fed G.17
}


class MissingApiKeyError(RuntimeError):
    pass


def api_key_from_env() -> str:
    key = os.getenv("FRED_API_KEY", "").strip()
    if not key:
        raise MissingApiKeyError("FRED_API_KEY absente (à définir dans .env)")
    return key


def vintage_available_at(series_id: str, vintage: date) -> datetime:
    hm = RELEASE_TIMES_ET.get(series_id)
    return et_to_utc(vintage, *hm) if hm else end_of_day_et(vintage)


def _value(v: str | None) -> float | None:
    if v in (None, "", "."):
        return None
    return float(v)


def parse_observations(series_id: str, payload: dict) -> list[dict]:
    """Toutes les vintages : une ligne par (date d'observation, realtime_start)."""
    out = []
    for o in payload.get("observations", []):
        obs = date.fromisoformat(o["date"])
        rt_start = date.fromisoformat(o["realtime_start"])
        rt_end = None if o["realtime_end"] == REALTIME_MAX else date.fromisoformat(o["realtime_end"])
        out.append(
            {
                "series_id": series_id,
                "observation_date": obs,
                "realtime_start": rt_start,
                "realtime_end": rt_end,
                "value": _value(o.get("value")),
                # event_time = période observée ; available_at = publication de cette vintage.
                "event_time": datetime(obs.year, obs.month, obs.day, tzinfo=UTC),
                "available_at": vintage_available_at(series_id, rt_start),
            }
        )
    return out


def parse_release_dates(payload: dict, fetched_at: datetime) -> list[dict]:
    out = []
    for r in payload.get("release_dates", []):
        d = date.fromisoformat(r["date"])
        out.append(
            {
                "release_id": int(r["release_id"]),
                "release_name": str(r.get("release_name", ""))[:200],
                "release_date": d,
                "event_time": et_to_utc(d, 0),
                # Un calendrier est connu au moment où on le lit, donc avant l'événement.
                "available_at": fetched_at,
                "allow_available_before_event": True,
            }
        )
    return out


@dataclass(frozen=True)
class ReleaseView:
    """Ce que le marché voyait au moment de la publication d'une observation."""

    observation_date: date
    available_at: datetime
    value: float | None
    previous_value: float | None  # valeur de la période précédente telle que connue à la publication
    previous_revised_from: float | None  # sa valeur juste avant la publication (révision)


class PointInTimeSeries:
    """Série avec toutes ses vintages ; requêtes sans look-ahead."""

    def __init__(self, series_id: str, rows: Iterable[dict]) -> None:
        self.series_id = series_id
        self.rows = sorted(rows, key=lambda r: (r["observation_date"], r["available_at"]))

    def as_of(self, t: datetime) -> dict[date, float | None]:
        """Valeur de chaque observation telle que connue à l'instant ``t``."""
        t = ensure_utc(t)
        known: dict[date, float | None] = {}
        for r in self.rows:
            if r["available_at"] <= t:
                known[r["observation_date"]] = r["value"]  # vintages triées : la dernière gagne
        return dict(sorted(known.items()))

    def latest_as_of(self, t: datetime) -> tuple[date, float | None] | None:
        vals = {d: v for d, v in self.as_of(t).items() if v is not None}
        return max(vals.items()) if vals else None

    def first_releases(self) -> list[ReleaseView]:
        firsts: dict[date, dict] = {}
        for r in self.rows:
            firsts.setdefault(r["observation_date"], r)
        out = []
        dates = sorted(firsts)
        for i, d in enumerate(dates):
            r = firsts[d]
            prev_now = prev_before = None
            if i > 0:
                pd = dates[i - 1]
                prev_now = self.as_of(r["available_at"]).get(pd)
                prev_before = self.as_of(r["available_at"] - timedelta(seconds=1)).get(pd)
            out.append(ReleaseView(d, r["available_at"], r["value"], prev_now, prev_before))
        return out


class FredSource:
    def __init__(self, api_key: str | None = None, client: PublicSourceClient | None = None, clock=utc_now) -> None:
        self._key = api_key or api_key_from_env()
        self.client = client or PublicSourceClient(SOURCE, HOSTS, rate_per_second=1.5, max_attempts=5, backoff_base=2.0,
                                                   error_detail=self._error_detail)  # 120/min max
        if self.client.error_detail is None:
            self.client.error_detail = self._error_detail
        self.clock = clock

    def __repr__(self) -> str:
        return "FredSource(api_key=***)"

    def _error_detail(self, resp) -> str:
        """``error_message`` de FRED, sans jamais recopier la clé."""
        msg = str(resp.json().get("error_message", ""))
        return msg.replace(self._key, "***") if self._key else msg

    def _get(self, endpoint: str, params: dict) -> tuple[dict, bytes]:
        resp = self.client.get(f"{BASE}/{endpoint}", params={**params, "api_key": self._key, "file_type": "json"})
        return resp.json(), resp.content

    def observations(self, series_id: str, observation_start: date | None = None) -> tuple[list[dict], Provenance]:
        """Toutes les vintages (ALFRED). Séries quotidiennes de marché : vintage courante (voir ``current_observations``)."""
        if SERIES.get(series_id, ("", ""))[1] == "daily":
            return self.current_observations(series_id, observation_start)
        now = self.clock()
        params = {"series_id": series_id, "realtime_start": REALTIME_MIN, "realtime_end": REALTIME_MAX, "limit": PAGE_LIMIT}
        if observation_start:
            params["observation_start"] = observation_start.isoformat()
        rows, digests, offset = [], [], 0
        while True:
            payload, raw = self._get("series/observations", {**params, "offset": offset})
            digests.append(sha256_bytes(raw))
            page = parse_observations(series_id, payload)
            rows += page
            offset += len(page)
            if not page or offset >= int(payload.get("count", 0)):
                break
        prov = Provenance(SOURCE, f"macro.fred.{series_id.lower()}", f"{BASE}/series/observations", now, params,
                          sha256_bytes("".join(digests).encode()), LICENSE)
        return rows, prov

    def current_observations(self, series_id: str, observation_start: date | None = None) -> tuple[list[dict], Provenance]:
        """Vintage courante seulement, publication estimée prudemment (``fred_graph.available_at``).

        Pour les séries quotidiennes (taux, VIX, dollar, spread HY), ALFRED refuse
        la demande de toutes les vintages (HTTP 400 : trop de dates de vintage).
        Ces séries de marché ne sont quasiment jamais révisées : la vintage
        courante, datée du lendemain fin de journée New York, ne crée pas d'avance.
        """
        from bot_trading.marketdata.sources import fred_graph

        now = self.clock()
        params = {"series_id": series_id, "limit": PAGE_LIMIT}
        if observation_start:
            params["observation_start"] = observation_start.isoformat()
        rows, digests, offset = [], [], 0
        while True:
            payload, raw = self._get("series/observations", {**params, "offset": offset})
            digests.append(sha256_bytes(raw))
            page = []
            for o in payload.get("observations", []):
                obs = date.fromisoformat(o["date"])
                page.append({
                    "series_id": series_id,
                    "observation_date": obs,
                    "realtime_start": fred_graph.estimated_release(series_id, obs),
                    "realtime_end": None,
                    "value": _value(o.get("value")),
                    "event_time": datetime(obs.year, obs.month, obs.day, tzinfo=UTC),
                    "available_at": fred_graph.available_at(series_id, obs),
                })
            rows += page
            offset += len(page)
            if not page or offset >= int(payload.get("count", 0)):
                break
        rows = [r for r in rows if r["available_at"] <= now]
        prov = Provenance(SOURCE, f"macro.fred.{series_id.lower()}", f"{BASE}/series/observations", now,
                          {**params, "vintage": "current"}, sha256_bytes("".join(digests).encode()), LICENSE)
        return rows, prov

    def vintage_dates(self, series_id: str) -> list[date]:
        payload, _ = self._get("series/vintagedates", {"series_id": series_id, "limit": 10000})
        return [date.fromisoformat(d) for d in payload.get("vintage_dates", [])]

    def release_calendar(self, start: date, end: date) -> tuple[list[dict], Provenance]:
        now = self.clock()
        params = {"realtime_start": start.isoformat(), "realtime_end": end.isoformat(),
                  "include_release_dates_with_no_data": "true", "limit": 1000, "order_by": "release_date", "sort_order": "asc"}
        payload, raw = self._get("releases/dates", params)
        prov = Provenance(SOURCE, "macro.fred.release_calendar", f"{BASE}/releases/dates", now, params, sha256_bytes(raw), LICENSE)
        return parse_release_dates(payload, now), prov


OBS_KEYS = ("series_id", "observation_date", "realtime_start")
CALENDAR_KEYS = ("release_id", "release_date")
