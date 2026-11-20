"""Data Engine : collecte, stockage traçable et contrôle de fraîcheur.

Le Data Engine ne prend aucune décision de trading. Il fournit aux agents et
au backtester des données **horodatées** (``available_at``) et **sourcées**.
Toute lecture destinée à une décision passe par ``as_of`` pour éviter le
look-ahead.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

from bot_trading.core.models import Candle, Timeframe
from bot_trading.marketdata.bars import CANDLE_KEYS, candles_dataset, to_candles
from bot_trading.marketdata.common import Provenance, utc_now
from bot_trading.marketdata.quality import QualityReport, candle_quality, series_quality
from bot_trading.marketdata.sources import cboe, edgar, fed_rss, fred, treasury
from bot_trading.marketdata.storage import ParquetStore, WriteResult


@dataclass
class DatasetSpec:
    name: str
    frequency: str  # intraday, daily, weekly, monthly, quarterly, feed
    timeframe: Timeframe | None = None


@dataclass
class DataEngine:
    store: ParquetStore
    clock: Callable[[], datetime] = utc_now
    specs: dict[str, DatasetSpec] = field(default_factory=dict)

    # ------------------------------------------------------------ écriture
    def ingest(self, records: Sequence[dict], prov: Provenance, keys: Sequence[str], spec: DatasetSpec | None = None) -> WriteResult:
        if spec is not None:
            self.specs[prov.dataset] = spec
        return self.store.write(prov.dataset, records, keys, prov)

    def ingest_candles(self, records: Sequence[dict], prov: Provenance, tf: Timeframe) -> WriteResult:
        return self.ingest(records, prov, CANDLE_KEYS, DatasetSpec(prov.dataset, "intraday", tf))

    def ingest_fred(self, records: Sequence[dict], prov: Provenance, series_id: str) -> WriteResult:
        freq = fred.SERIES.get(series_id, ("", "monthly"))[1]
        return self.ingest(records, prov, fred.OBS_KEYS, DatasetSpec(prov.dataset, freq))

    def ingest_fed_rss(self, records: Sequence[dict], prov: Provenance) -> WriteResult:
        return self.ingest(records, prov, fed_rss.KEYS, DatasetSpec(prov.dataset, "feed"))

    def ingest_vix(self, records: Sequence[dict], prov: Provenance) -> WriteResult:
        return self.ingest(records, prov, cboe.KEYS, DatasetSpec(prov.dataset, "daily"))

    def ingest_treasury(self, records: Sequence[dict], prov: Provenance) -> WriteResult:
        return self.ingest(records, prov, treasury.KEYS, DatasetSpec(prov.dataset, "daily"))

    def ingest_edgar_filings(self, records: Sequence[dict], prov: Provenance) -> WriteResult:
        return self.ingest(records, prov, edgar.FILINGS_KEYS, DatasetSpec(prov.dataset, "quarterly"))

    def ingest_edgar_facts(self, records: Sequence[dict], prov: Provenance) -> WriteResult:
        return self.ingest(records, prov, edgar.FACTS_KEYS, DatasetSpec(prov.dataset, "quarterly"))

    # ------------------------------------------------------------- lecture
    def candles(self, source: str, epic: str, tf: Timeframe, as_of: datetime, start: datetime | None = None, side: str = "bid") -> list[Candle]:
        """Bougies **closes** et connues à ``as_of``, prêtes pour les moteurs d'analyse."""
        rows = self.store.read(candles_dataset(source, epic, tf), start=start, as_of=as_of)
        return to_candles(rows, side)

    def fred_series(self, series_id: str, as_of: datetime | None = None) -> fred.PointInTimeSeries:
        rows = self.store.read(f"macro.fred.{series_id.lower()}", as_of=as_of)
        return fred.PointInTimeSeries(series_id, rows)

    def fed_news(self, as_of: datetime, since: datetime | None = None) -> list[dict]:
        return self.store.read(fed_rss.DATASET, start=since, as_of=as_of)

    # ------------------------------------------------------------- qualité
    def quality(self, dataset: str, now: datetime | None = None) -> QualityReport:
        now = now or self.clock()
        spec = self.specs.get(dataset) or infer_spec(dataset)
        rows = self.store.read(dataset)
        if spec.timeframe is not None:
            return candle_quality(dataset, rows, spec.timeframe, now)
        return series_quality(dataset, rows, now, spec.frequency)

    def health(self, now: datetime | None = None) -> list[QualityReport]:
        return [self.quality(d, now) for d in self.store.datasets()]


def infer_spec(dataset: str) -> DatasetSpec:
    """Retrouve la fréquence d'un dataset d'après son nom (après redémarrage)."""
    parts = dataset.split(".")
    if parts[0] == "candles":
        return DatasetSpec(dataset, "intraday", Timeframe(parts[-1].upper()))
    if parts[:2] == ["macro", "fred"]:
        if parts[2] == "release_calendar":
            return DatasetSpec(dataset, "weekly")
        return DatasetSpec(dataset, fred.SERIES.get(parts[2].upper(), ("", "monthly"))[1])
    if dataset == fed_rss.DATASET:
        return DatasetSpec(dataset, "feed")
    if dataset.startswith("fundamentals."):
        return DatasetSpec(dataset, "quarterly")
    return DatasetSpec(dataset, "daily")


def default_since(years: int = 10) -> date:
    today = utc_now().date()
    return today.replace(year=today.year - years)
