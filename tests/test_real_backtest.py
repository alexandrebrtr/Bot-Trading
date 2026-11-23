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


def test_inflation_scoring_favours_direction_over_level():
    """3,5 % qui reflue : pénalisé par l'ancienne notation, presque neutre avec la nouvelle."""
    from bot_trading.intelligence.macro import MacroConfig
    from bot_trading.intelligence.series import squash

    def f1(cfg, level, trend):
        return (-cfg.inflation_level_weight * squash((level - cfg.inflation_comfort) / 1.0)
                - cfg.inflation_trend_weight * squash(trend / cfg.inflation_trend_scale))

    new, old = MacroConfig(), MacroConfig.level_first()
    assert f1(new, 3.5, -0.5) > 0 > f1(old, 3.5, -0.5)
    assert f1(new, 2.5, +0.5) < f1(old, 2.5, +0.5) < 0  # une réaccélération pèse plus qu'avant
    assert abs(new.inflation_level_weight + new.inflation_trend_weight + new.inflation_surprise_weight - 1) < 1e-9


def test_cli_store_backtest_accepts_old_inflation_scoring(tmp_path, capsys):
    store = to_store(build_store(), tmp_path / "store")
    _write_bars(store, datetime(2026, 6, 1, tzinfo=UTC), datetime(2026, 11, 20, tzinfo=UTC))
    assert cli(["backtest", "--store", str(tmp_path / "store"), "--holdout-months", "1", "--inflation-scoring", "niveau"]) == 0
    assert "Notation de l'inflation : niveau" in capsys.readouterr().out


def test_cli_risk_scenario_is_backtest_only(capsys):
    from bot_trading.config import RiskLimits

    assert cli(["backtest", "--years", "1", "--leverage", "10", "--max-margin-loss", "0.5"]) == 0
    out = capsys.readouterr().out
    assert "SCÉNARIO DE RISQUE" in out and "levier 10:1" in out and "≈ 5.0% du prix" in out
    assert RiskLimits().max_leverage == 10.0  # la politique par défaut est intacte


def test_cli_decisions_on_store_counts_refusal_reasons(tmp_path, capsys):
    store = to_store(build_store(), tmp_path / "store")
    _write_bars(store, datetime(2026, 6, 1, tzinfo=UTC), datetime(2026, 11, 20, tzinfo=UTC))
    assert cli(["decisions", "--store", str(tmp_path / "store"), "--holdout-months", "1", "--leverage", "10"]) == 0
    out = capsys.readouterr().out
    assert "RÉELLES" in out and "Motifs de NO_TRADE" in out and "SCÉNARIO DE RISQUE" in out
    assert "Réserve finale non ouverte : 2026-10-20" in out


def test_extension_grid_parsing():
    import math

    from bot_trading.__main__ import _extension_grid

    assert _extension_grid("3, 4.5,off") == [3.0, 4.5, math.inf]


def test_fixed_out_of_sample_uses_only_test_windows():
    from bot_trading.backtest.strategies import BuyAndHold
    from bot_trading.backtest.validation import fixed_out_of_sample, walk_forward_folds
    from bot_trading.core.models import Timeframe
    from bot_trading.marketdata.synthetic import generate_bars

    bars, _ = generate_bars(datetime(2018, 1, 1, tzinfo=UTC), datetime(2019, 1, 1, tzinfo=UTC), Timeframe.H1, seed=4)
    folds, _ = walk_forward_folds(bars[0].time, bars[-1].time, train_months=3, test_months=3, step_months=3, holdout_months=0)
    rep = fixed_out_of_sample(lambda p: BuyAndHold(), {}, bars, folds)
    assert len(rep.folds) == len(folds) and rep.n_trials == len(folds)
    for f in rep.folds:
        assert f.in_sample is None
        assert all(f.fold.test_start <= t.trade.opened_at < f.fold.test_end for t in f.oos_result.trades)


def test_cli_extension_study_on_synthetic(capsys):
    args = ["backtest", "--years", "3", "--train-months", "12", "--test-months", "6", "--holdout-months", "6", "--extension-grid", "3,off"]
    assert cli(args) == 0
    out = capsys.readouterr().out
    assert "Entrée tardive (T3)" in out and "| désactivé |" in out and "| 3 |" in out and "Buy & hold (repère)" in out
    assert "Sélection walk-forward" in out and "Walk-forward (technique FVG)" not in out


def test_cli_entry_study_lists_variants(capsys):
    args = ["backtest", "--years", "3", "--train-months", "12", "--test-months", "6", "--holdout-months", "6", "--entry-study", "--no-selection"]
    assert cli(args) == 0
    out = capsys.readouterr().out
    assert "Entrées plus fréquentes (EXP-14) (H1)" in out
    for name in ("| actuel |", "| H4 non exigé |", "| confirmation touch |", "| H4 non exigé + touch |"):
        assert name in out
    assert "Sélection walk-forward non lancée" in out


def test_entry_variants_build_valid_strategies():
    from bot_trading.__main__ import ENTRY_VARIANTS
    from bot_trading.strategy import DecisionParams, DecisionStrategy, TechnicalParams

    for v in ENTRY_VARIANTS.values():
        s = DecisionStrategy(technical=TechnicalParams(**v.get("technical", {})), decision=DecisionParams(**v.get("decision", {})))
        assert s is not None
