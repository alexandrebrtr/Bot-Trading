"""Pont entre le store Parquet et les agents d'``intelligence`` (``PointInTimeData``).

``StorePITData`` lit ce que les connecteurs ont collecté (FRED/ALFRED, Cboe,
Trésor, RSS Fed, calendrier FRED, EDGAR) et ne renvoie **que** ce qui était
publié à ``asof`` (colonne ``available_at``). C'est ce qui permet au Market
Regime Engine de produire ``regime.json`` automatiquement pour le paper trading.

Correspondances :

| Demande | Dataset | Repli |
|---|---|---|
| ``series(id)`` FRED | ``macro.fred.<id>`` (dernière vintage par période) | ``macro.fredgraph.<id>`` (sans clé, si pas de vintages) ; VIX : ``market.cboe.vix`` ; DGS2/DGS10 : ``rates.treasury.par_curve`` |
| ``fed_communications`` | ``news.fed_rss`` (communiqués FOMC, minutes, discours, témoignages) | — |
| ``scheduled_events`` | ``macro.fred.release_calendar`` (CPI, NFP, PCE, PIB, ventes au détail, FOMC si listé) | événements manuels (``extra_events``) |
| ``earnings`` | ``fundamentals.edgar.facts`` (via ``ticker_ciks``) | — |
| ``index_weights`` | non collecté (licence SSGA) : ``None`` | univers de repli de l'agent |

Les textes RSS restent des données externes non fiables : ils ne servent qu'au
calcul de ton déterministe (``intelligence.fed``), jamais comme instructions.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta

from bot_trading.intelligence.data import EarningsReport, FedCommunication, IndexWeights, Observation, ScheduledEvent
from bot_trading.intelligence.fed import DEFAULT_SPEAKER_WEIGHTS
from bot_trading.marketdata.common import et_to_utc

# Nom de publication FRED → type d'événement (HIGH_IMPACT de ``intelligence.macro``).
RELEASE_KINDS: tuple[tuple[str, str, tuple[int, int]], ...] = (
    ("consumer price index", "CPI", (8, 30)),
    ("employment situation", "NFP", (8, 30)),
    ("personal income and outlays", "PCE", (8, 30)),
    ("gross domestic product", "GDP", (8, 30)),
    ("advance monthly sales for retail", "RETAIL_SALES", (8, 30)),
    ("fomc", "FOMC", (14, 0)),
)

_SPEAKER_RX = re.compile(r"\b(?:Chair(?:man)?|Vice Chair(?: for Supervision)?|Governor|President)\s+([A-Z][A-Za-z'\-]+)")
_LEADING_NAME_RX = re.compile(r"^([A-Z][A-Za-z'\-]+),")
_FRED_FALLBACK = {"VIXCLS": ("market.cboe.vix", "close"), "DGS2": ("rates.treasury.par_curve", "y_2y"), "DGS10": ("rates.treasury.par_curve", "y_10y")}


def _day(v) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def speaker_of(title: str, feed: str) -> str:
    for name in DEFAULT_SPEAKER_WEIGHTS:
        if name != "FOMC" and re.search(rf"\b{re.escape(name)}\b", title):
            return name
    m = _SPEAKER_RX.search(title) or _LEADING_NAME_RX.search(title)
    if m:
        return m.group(1)
    return "FOMC" if feed == "press_monetary" else "Fed"


def kind_of(title: str, feed: str) -> str | None:
    t = title.lower()
    if feed == "speeches":
        return "speech"
    if feed == "testimony":
        return "testimony"
    if feed == "press_monetary":
        if "minutes" in t:
            return "minutes"
        if "fomc statement" in t or "issues fomc" in t:
            return "statement"
        return None
    return None  # press_all : doublons et sujets non monétaires


class StorePITData:
    """Implémentation de ``intelligence.data.PointInTimeData`` sur un ``ParquetStore``."""

    def __init__(
        self,
        store,
        ticker_ciks: Mapping[str, str] | None = None,
        extra_events: Iterable[ScheduledEvent] = (),
    ) -> None:
        from bot_trading.marketdata.universe import TOP_CIKS

        self.store = store
        # Par défaut : CIK du top 10 suivi (sans eux, ``earnings`` ne renvoyait jamais rien).
        self.ticker_ciks = {k.upper(): str(v) for k, v in (TOP_CIKS if ticker_ciks is None else ticker_ciks).items()}
        self.extra_events = list(extra_events)
        self._rows: dict[str, list[dict]] = {}
        self._datasets: set[str] | None = None

    # ------------------------------------------------------------ bas niveau
    def _has(self, dataset: str) -> bool:
        if self._datasets is None:
            self._datasets = set(self.store.datasets())
        return dataset in self._datasets

    def _read(self, dataset: str) -> list[dict]:
        """Tout le dataset (mis en cache) ; le filtre ``available_at`` est fait par l'appelant."""
        if dataset not in self._rows:
            self._rows[dataset] = self.store.read(dataset) if self._has(dataset) else []
        return self._rows[dataset]

    # ------------------------------------------------------------ séries
    def series(self, series_id: str, asof: datetime) -> list[Observation]:
        rows = [r for r in self._read(f"macro.fred.{series_id.lower()}") if r["available_at"] <= asof and r.get("value") is not None]
        tag = "ALFRED"
        if not rows and not self._has(f"macro.fred.{series_id.lower()}"):
            # Repli sans clé : export fredgraph (dernière vintage, publication estimée prudemment).
            rows = [r for r in self._read(f"macro.fredgraph.{series_id.lower()}") if r["available_at"] <= asof and r.get("value") is not None]
            tag = "FREDGRAPH"
        if rows:
            latest: dict[date, dict] = {}
            for r in sorted(rows, key=lambda r: r["available_at"]):
                latest[_day(r["observation_date"])] = r
            return [
                Observation(series_id, d, float(r["value"]), r["available_at"], f"{tag}:{series_id}")
                for d, r in sorted(latest.items())
            ]
        fb = _FRED_FALLBACK.get(series_id)
        if fb is None:
            return []
        dataset, col = fb
        out: dict[date, Observation] = {}
        for r in self._read(dataset):
            if r["available_at"] <= asof and r.get(col) is not None:
                d = _day(r.get("date") or r["event_time"])
                out[d] = Observation(series_id, d, float(r[col]), r["available_at"], dataset)
        return [out[d] for d in sorted(out)]

    # ------------------------------------------------------------ Fed
    def fed_communications(self, asof: datetime, since: datetime) -> list[FedCommunication]:
        out = []
        for r in self._read("news.fed_rss"):
            t = r["available_at"]
            if not (since <= t <= asof):
                continue
            title = str(r.get("title") or "")
            kind = kind_of(title, str(r.get("feed") or ""))
            if kind is None:
                continue
            out.append(FedCommunication(
                id=str(r.get("guid") or r.get("link") or title),
                speaker=speaker_of(title, str(r.get("feed") or "")),
                kind=kind,  # type: ignore[arg-type]
                published_at=t,
                title=title,
                text=f"{title}. {r.get('summary') or ''}",
                url=str(r.get("link") or ""),
            ))
        return sorted(out, key=lambda c: c.published_at)

    # ------------------------------------------------------------ calendrier
    def scheduled_events(self, asof: datetime, start: datetime, end: datetime) -> list[ScheduledEvent]:
        out: dict[tuple[str, datetime], ScheduledEvent] = {}
        for r in self._read("macro.fred.release_calendar"):
            if r["available_at"] > asof:
                continue
            name = str(r.get("release_name") or "")
            for needle, kind, (h, m) in RELEASE_KINDS:
                if needle in name.lower():
                    when = et_to_utc(_day(r["release_date"]), h, m)
                    if start <= when <= end:
                        out[(kind, when)] = ScheduledEvent(kind, name, when, r["available_at"])
                    break
        for e in self.extra_events:
            if e.known_since <= asof and start <= e.scheduled_at <= end:
                out[(e.kind, e.scheduled_at)] = e
        return sorted(out.values(), key=lambda e: e.scheduled_at)

    # ------------------------------------------------------------ entreprises
    def earnings(self, asof: datetime, ticker: str) -> list[EarningsReport]:
        cik = self.ticker_ciks.get(ticker.upper())
        if cik is None:
            return []
        facts = [r for r in self._read("fundamentals.edgar.facts") if str(r.get("cik")) == cik and r["available_at"] <= asof]
        by_period: dict[date, dict[str, dict]] = {}
        for r in sorted(facts, key=lambda r: r["available_at"]):
            ps, pe = r.get("period_start"), r.get("period_end")
            if ps is None or pe is None:
                continue
            days = (_day(pe) - _day(ps)).days
            if not 80 <= days <= 100:  # trimestre seulement (les cumuls annuels faussent les comparaisons)
                continue
            by_period.setdefault(_day(pe), {})[str(r["concept"])] = r
        out = []
        for pe, concepts in sorted(by_period.items()):
            rev = concepts.get("Revenues") or concepts.get("RevenueFromContractWithCustomerExcludingAssessedTax")
            eps = concepts.get("EarningsPerShareDiluted")
            if rev is None or eps is None:
                continue
            op = concepts.get("OperatingIncomeLoss")
            filed = max(x["available_at"] for x in (rev, eps) + ((op,) if op else ()))
            out.append(EarningsReport(ticker.upper(), pe, filed, float(rev["value"]), float(eps["value"]), float(op["value"]) if op else None))
        return out

    def index_weights(self, asof: datetime) -> IndexWeights | None:
        return None


def assess_regime(store, asof: datetime, previous=None, **kw):
    """Évaluation du Market Regime Engine à ``asof`` sur les données du store (mode paper, sans LLM)."""
    from bot_trading.config import TradingMode
    from bot_trading.intelligence.regime import MarketRegimeEngine

    engine = _engine(store, **kw)
    return engine.assess(asof, previous)


def _engine(store, fallback_universe=None, **kw):
    """Market Regime Engine branché sur le store, avec l'univers « entreprises » par défaut."""
    from bot_trading.config import TradingMode
    from bot_trading.intelligence.regime import MarketRegimeEngine
    from bot_trading.marketdata.universe import TOP_TICKERS

    universe = list(TOP_TICKERS if fallback_universe is None else fallback_universe)
    return MarketRegimeEngine(StorePITData(store, **kw), mode=TradingMode.PAPER, fallback_universe=universe)


def floor_time(t: datetime, minutes: int) -> datetime:
    """Arrondi vers le bas sur la grille du timeframe (``asof`` d'une évaluation = clôture de bougie)."""
    base = t.replace(second=0, microsecond=0)
    total = base.hour * 60 + base.minute
    return base.replace(hour=0, minute=0) + timedelta(minutes=total - total % minutes)


# ------------------------------------------------------------------ historique (backtest)


def regime_history(
    store,
    start: datetime,
    end: datetime,
    every: timedelta = timedelta(days=1),
    hour_utc: int = 21,
    cache_path=None,
    progress=None,
    **kw,
) -> list:
    """Régimes évalués à pas fixe sur ``[start, end)`` (21:00 UTC : après la clôture cash US).

    Chaque évaluation ne voit que les données publiées à son ``asof`` (StorePITData)
    et passe par ``guard_coverage`` comme en paper trading. ``cache_path`` (JSONL) :
    les ``asof`` déjà présents sont relus au lieu d'être recalculés ; à vider si le
    store a changé.
    """
    import json
    from pathlib import Path

    from bot_trading.config import TradingMode
    from bot_trading.intelligence.regime import MarketRegimeEngine
    from bot_trading.intelligence.schemas import RegimeAssessment
    from bot_trading.paper.regime import guard_coverage

    cached: dict[datetime, RegimeAssessment] = {}
    path = Path(cache_path) if cache_path else None
    if path is not None and path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                a = RegimeAssessment.model_validate(json.loads(line))
            except ValueError:
                continue
            cached[a.asof] = a
    engine = _engine(store, **kw)
    t = start.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    if t < start:
        t += timedelta(days=1)
    out: list = []
    fresh: list = []
    previous = None
    while t < end:
        a = cached.get(t)
        if a is None:
            a = guard_coverage(engine.assess(t, previous))
            fresh.append(a)
        out.append(a)
        previous = a.regime
        if progress is not None:
            progress(t, a)
        t += every
    if path is not None and fresh:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            for a in fresh:
                f.write(json.dumps(a.model_dump(mode="json")) + "\n")
    return out


def regime_observations(assessments) -> list:
    """Séries point-in-time du backtest : évaluation complète, régime et biais, publiés à ``asof``."""
    from bot_trading.marketdata.source import Observation as PITObservation
    from bot_trading.strategy.fundamental import SERIES_ASSESSMENT, SERIES_BIAS, SERIES_REGIME, normalize_bias, normalize_regime

    out = []
    for a in assessments:
        out.append(PITObservation(SERIES_ASSESSMENT, a.asof, a, a.asof, "regime_engine"))
        out.append(PITObservation(SERIES_REGIME, a.asof, normalize_regime(a.regime), a.asof, "regime_engine"))
        # Biais exploitable seulement si les entrées sont autorisées (couverture, F10, F11).
        bias = normalize_bias(a.bias) if a.allow_new_entries else ("bearish" if normalize_regime(a.regime) == "RISK_OFF" else "neutral")
        out.append(PITObservation(SERIES_BIAS, a.asof, bias, a.asof, "regime_engine"))
    return out
