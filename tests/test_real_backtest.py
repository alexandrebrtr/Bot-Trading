"""Backtest sur le store réel : FRED sans clé, régime historique point-in-time, réserve finale intacte. Aucun réseau."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from intel_fixtures import ASOF, build_store
from test_regime_pipeline import prov, to_store
from bot_trading.__main__ import main as cli
from bot_trading.core.models import Timeframe
from bot_trading.marketdata.bars import bar_record, candles_dataset
from bot_trading.marketdata.engine import DataEngine, DatasetSpec
from bot_trading.marketdata.pit import StorePITData, regime_history, regime_observations
from bot_trading.marketdata.sources import fred_graph
from bot_trading.marketdata.storage import ParquetStore
from bot_trading.marketdata.synthetic import generate_bars
from bot_trading.strategy.fundamental import SERIES_ASSESSMENT, SERIES_BIAS, FundamentalView

UTC = timezone.utc


def test_fred_graph_publication_is_never_early():
    raw = b"observation_date,CPIAUCSL\n2026-09-01,320.1\n2026-10-01,.\n"
    rows = fred_graph.parse_csv("CPIAUCSL", raw)
    assert [r["value"] for r in rows] == [320.1, None]
    # CPI de septembre : publié mi-octobre, estimé au 21 octobre 8:30 New York.
    assert rows[0]["available_at"] == datetime(2026, 10, 21, 12, 30, tzinfo=UTC)
    assert rows[0]["realtime_start"] == date(2026, 10, 21)
    gdp = fred_graph.parse_csv("GDPC1", b"DATE,GDPC1\n2026-04-01,24000\n")[0]
    assert gdp["available_at"].date() == date(2026, 8, 5)  # T2 : après l'estimation avancée de fin juillet
    dgs = fred_graph.parse_csv("DGS10", b"DATE,DGS10\n2026-11-19,4.1\n")[0]
    assert dgs["available_at"] > datetime(2026, 11, 20, 23, tzinfo=UTC)


def test_pit_falls_back_to_fred_graph_only_without_vintages(tmp_path):
    store = ParquetStore(tmp_path / "s")
    rows = fred_graph.parse_csv("UNRATE", b"DATE,UNRATE\n2026-08-01,4.3\n2026-09-01,4.4\n")
    DataEngine(store).ingest(rows, prov("macro.fredgraph.unrate", ASOF), fred_graph.KEYS, DatasetSpec("macro.fredgraph.unrate", "monthly"))
    data = StorePITData(store)
    obs = data.series("UNRATE", ASOF)
    assert [o.value for o in obs] == [4.3, 4.4] and obs[0].source == "FREDGRAPH:UNRATE"
    assert [o.value for o in data.series("UNRATE", datetime(2026, 10, 5, tzinfo=UTC))] == [4.3]  # septembre pas encore publié


def test_regime_history_is_point_in_time_and_cached(tmp_path):
    store = to_store(build_store(), tmp_path / "store")
    cache = tmp_path / "regimes.jsonl"
    start, end = ASOF - timedelta(days=5), ASOF + timedelta(hours=1)
    hist = regime_history(store, start, end, cache_path=cache)
    assert [a.asof for a in hist] == [start.replace(hour=21) + timedelta(days=i) for i in range(5)] + [ASOF]
    assert hist[-1].allow_new_entries and hist[-1].regime.value == "RISK_ON"
    assert len(cache.read_text().splitlines()) == 6
    again = regime_history(store, start, end, cache_path=cache)
    assert [a.model_dump() for a in again] == [a.model_dump() for a in hist]
    assert len(cache.read_text().splitlines()) == 6  # rien de recalculé
    obs = regime_observations(hist)
    last = [o for o in obs if o.series == SERIES_ASSESSMENT][-1]
    assert last.available_at == ASOF and FundamentalView.from_assessment(last.value).allow_new_entries
    assert [o.value for o in obs if o.series == SERIES_BIAS][-1] == "bullish"


def _write_bars(store, start, end):
    bars, _ = generate_bars(start, end, Timeframe.H1, seed=3)
    rows = [{**bar_record("US500", Timeframe.H1, b.bid, None), "ask_close": b.bid.close + 0.3, "bid_close": b.bid.close} for b in bars]
    ds = candles_dataset("dukascopy", "US500", Timeframe.H1)
    DataEngine(store).ingest_candles(rows, prov(ds, end), Timeframe.H1)
    return bars


def test_cli_store_backtest_keeps_holdout_closed(tmp_path, capsys):
    store = to_store(build_store(), tmp_path / "store")
    bars = _write_bars(store, datetime(2026, 6, 1, tzinfo=UTC), datetime(2026, 11, 20, tzinfo=UTC))
    rc = cli(["backtest", "--store", str(tmp_path / "store"), "--timeframe", "H1", "--holdout-months", "1",
              "--regime-cache", str(tmp_path / "r.jsonl")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "RÉELLES" in out and "Complète (Decision Engine)" in out and "Fondamentale seule" in out
    assert "Réserve finale non ouverte : 2026-10-20" in out
    # Aucun régime évalué dans la réserve : elle n'est jamais lue.
    import json

    asofs = [json.loads(line)["asof"] for line in (tmp_path / "r.jsonl").read_text().splitlines()]
    assert asofs and max(asofs) < "2026-10-20"
    assert bars[-1].time > datetime(2026, 11, 1, tzinfo=UTC)


def test_cli_store_backtest_without_bars(tmp_path, capsys):
    assert cli(["backtest", "--store", str(tmp_path / "vide")]) == 1


def test_equity_component_is_wired_to_the_store(tmp_path):
    """Sans CIK ni univers, la composante « entreprises » était toujours vide."""
    from bot_trading.marketdata.pit import _engine
    from bot_trading.marketdata.universe import TOP_CIKS, TOP_TICKERS

    store = ParquetStore(tmp_path / "s")
    assert StorePITData(store).ticker_ciks["AAPL"] == TOP_CIKS["AAPL"] == "320193"
    eng = _engine(store)
    assert eng.equity_agent.fallback_universe == list(TOP_TICKERS)
    rows = []
    for i, (ps, pe) in enumerate([(date(2026, 3, 29), date(2026, 6, 28)), (date(2026, 6, 29), date(2026, 9, 27))]):
        filed = datetime(2026, 8 + 3 * i, 1, 21, tzinfo=UTC)
        for concept, v in (("Revenues", 90e9 + i * 5e9), ("EarningsPerShareDiluted", 1.4 + i * 0.2)):
            rows.append({"cik": 320193, "taxonomy": "us-gaap", "concept": concept, "unit": "USD", "period_start": ps, "period_end": pe,
                         "accession_number": f"a{i}", "value": v, "event_time": datetime(pe.year, pe.month, pe.day, tzinfo=UTC), "available_at": filed})
    DataEngine(store).ingest(rows, prov("fundamentals.edgar.facts", ASOF), ("cik", "taxonomy", "concept", "unit", "period_start", "period_end", "accession_number"),
                             DatasetSpec("fundamentals.edgar.facts", "quarterly"))
    reps = StorePITData(store).earnings(ASOF, "AAPL")
    assert [r.period_end for r in reps] == [date(2026, 6, 28), date(2026, 9, 27)]
    assert StorePITData(store).earnings(datetime(2026, 9, 1, tzinfo=UTC), "AAPL")[-1].period_end == date(2026, 6, 28)
