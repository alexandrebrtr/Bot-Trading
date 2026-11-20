"""Data Engine : orchestration, lectures point-in-time et rapport de santé."""

from datetime import datetime, timedelta, timezone

from bot_trading.core.models import Timeframe
from bot_trading.marketdata.__main__ import main as data_main
from bot_trading.marketdata.common import Provenance
from bot_trading.marketdata.engine import DataEngine, infer_spec
from bot_trading.marketdata.quality import Freshness
from bot_trading.marketdata.sources import fed_rss, fred
from bot_trading.marketdata.storage import ParquetStore
from test_data_sources import CPI_VINTAGES, RSS, fred_source

UTC = timezone.utc
NOW = datetime(2026, 11, 20, 22, 0, tzinfo=UTC)


def test_engine_macro_and_news_point_in_time(tmp_path):
    eng = DataEngine(ParquetStore(tmp_path), clock=lambda: NOW)
    src, _ = fred_source(CPI_VINTAGES)
    rows, prov = src.observations("CPIAUCSL")
    eng.ingest_fred(rows, prov, "CPIAUCSL")
    s = eng.fred_series("CPIAUCSL", as_of=datetime(2026, 11, 1, tzinfo=UTC))
    assert s.latest_as_of(NOW)[1] == 320.0  # la révision du 13/11 n'existe pas encore
    news = fed_rss.parse_rss(RSS, "press_monetary")
    eng.ingest_fed_rss(news, Provenance("fed_rss", fed_rss.DATASET, fed_rss.FEEDS["press_monetary"], NOW))
    assert len(eng.fed_news(as_of=datetime(2026, 10, 29, tzinfo=UTC))) == 1
    reports = {r.dataset: r for r in eng.health(NOW)}
    assert reports["macro.fred.cpiaucsl"].freshness == Freshness.FRESH  # mensuel, publié il y a 7 jours
    assert reports[fed_rss.DATASET].freshness == Freshness.STALE  # rien depuis plus de 14 jours
    assert eng.quality("macro.fred.cpiaucsl", NOW + timedelta(days=60)).freshness == Freshness.STALE


def test_infer_spec_after_restart():
    assert infer_spec("candles.capital.us500.m15").timeframe == Timeframe.M15
    assert infer_spec("macro.fred.icsa").frequency == "weekly"
    assert infer_spec("macro.fred.release_calendar").frequency == "weekly"
    assert infer_spec("rates.treasury.par_curve").frequency == "daily"
    assert fred.SERIES["DGS10"][1] == "daily"


def test_health_command(tmp_path, capsys):
    assert data_main(["--store", str(tmp_path), "--env", str(tmp_path / "absent.env"), "health"]) == 0
    assert "Aucun dataset" in capsys.readouterr().out
