"""Dashboard : lecture de l'état d'exécution, interface de décision, vues, graphiques,
kill switch et garantie de lecture seule. Aucun réseau."""

from __future__ import annotations

import ast
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bot_trading.config import BrokerCosts, InstrumentSpec, RiskLimits, Settings
from bot_trading.core.decision import Action, Decision
from bot_trading.core.models import Direction, OrderRequest, Timeframe
from bot_trading.dashboard import charts, market, views
from bot_trading.dashboard.decision_feed import (
    DecisionFeed,
    JsonlDecisionFeed,
    parse_record,
    publish_decision,
    summarize,
)
from bot_trading.dashboard.runtime import AccountSnapshot, RuntimeReader, append_jsonl, write_json
from bot_trading.risk.engine import AccountState, MarketState, RiskEngine, Violation
from bot_trading.risk.kill_switch import CONFIRMATION, KillSwitchStore

from conftest import make_candles, random_walk

NOW = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)
DASHBOARD_DIR = Path(__file__).resolve().parents[1] / "src" / "bot_trading" / "dashboard"


# ------------------------------------------------------------------ lecture


def test_reader_missing_directory_gives_empty_states(tmp_path):
    r = RuntimeReader(tmp_path / "absent")
    assert not r.exists()
    assert r.account() is None
    assert r.equity_curve() == [] and r.positions() == [] and r.trades() == []
    assert r.regime() is None and r.experiments() == [] and r.heartbeats() == []
    assert JsonlDecisionFeed(r).latest() == []
    assert r.warnings == []


def test_reader_tolerates_corrupted_files(tmp_path):
    (tmp_path / "account.json").write_text("{pas du json")
    (tmp_path / "equity.jsonl").write_text(
        '{"time": "2026-11-20T10:00:00Z", "equity_eur": 20}\nnope\n[1, 2]\n{"time": "x", "equity_eur": 3}\n'
        '{"time": "2026-11-20T09:00:00+00:00", "equity_eur": "19.5"}\n'
    )
    r = RuntimeReader(tmp_path)
    assert r.account() is None
    curve = r.equity_curve()
    assert [v for _, v in curve] == [19.5, 20.0]  # trié, ligne à heure invalide ignorée
    assert curve[0][0].tzinfo is not None
    assert any("account.json" in w for w in r.warnings)
    assert any("equity.jsonl : 2 ligne(s)" in w for w in r.warnings)


def test_account_snapshot_and_writer_roundtrip(tmp_path):
    write_json(tmp_path / "account.json", {"equity_eur": 18.5, "peak_equity_eur": 21, "used_margin_eur": 3.4, "open_exposure_lots": 0.01, "updated_at": NOW})
    a = RuntimeReader(tmp_path).account()
    assert a == AccountSnapshot(18.5, 18.5, 21.0, 3.4, 0.01, NOW, "paper")
    write_json(tmp_path / "account.json", {"balance_eur": 3})  # equity manquante
    r = RuntimeReader(tmp_path)
    assert r.account() is None and r.warnings


def test_heartbeats_accept_mapping_or_single(tmp_path):
    write_json(tmp_path / "heartbeat.json", {"paper_loop": {"time": NOW, "status": "OK"}})
    assert RuntimeReader(tmp_path).heartbeats() == [{"component": "paper_loop", "time": NOW.isoformat(), "status": "OK"}]
    write_json(tmp_path / "heartbeat.json", {"component": "x", "time": NOW})
    assert RuntimeReader(tmp_path).heartbeats()[0]["component"] == "x"


# ------------------------------------------------------------------ décisions


def test_publish_decision_roundtrip(tmp_path):
    d = Decision(Action.LONG, stop_level=6700.0, risk_fraction=0.1, reason="FVG H1 50 %", confidence=0.7,
                 setup="FVG_H1_50", regime="RISK_ON", bias="bullish", sources=("ALFRED:CPIAUCSL",))
    publish_decision(tmp_path / "decisions.jsonl", d, NOW, outcome="VETO", violations=(Violation.SPREAD_TOO_WIDE,), price=6800.5, timeframe="H1")
    publish_decision(tmp_path / "decisions.jsonl", Decision.no_trade("Biais neutre"), NOW + timedelta(hours=1))
    feed = JsonlDecisionFeed(RuntimeReader(tmp_path))
    assert isinstance(feed, DecisionFeed)
    out = feed.latest()
    assert [x.action for x in out] == ["LONG", "NO_TRADE"]
    v = out[0]
    assert v.time == NOW and v.stop_level == 6700.0 and v.setup == "FVG_H1_50" and v.sources == ("ALFRED:CPIAUCSL",)
    assert v.violations == ("SPREAD_TOO_WIDE",) and v.vetoed and v.price == 6800.5
    assert not out[1].vetoed


def test_invalid_decisions_are_skipped_and_reported(tmp_path):
    p = tmp_path / "decisions.jsonl"
    append_jsonl(p, {"time": NOW, "action": "BUY_NOW"})  # action inconnue
    append_jsonl(p, {"action": "LONG"})  # heure absente
    append_jsonl(p, {"time": NOW, "action": "hold", "confidence": "n/a", "violations": "STALE_DATA"})
    r = RuntimeReader(tmp_path)
    out = JsonlDecisionFeed(r).latest()
    assert len(out) == 1 and out[0].action == "HOLD" and out[0].confidence is None and out[0].violations == ("STALE_DATA",)
    assert any("2 décision(s) invalide(s)" in w for w in r.warnings)
    assert parse_record({"time": "2026-11-20", "action": "CLOSE"}).time.tzinfo is not None


def test_summarize_decisions():
    recs = [
        {"time": NOW, "action": "NO_TRADE"},
        {"time": NOW, "action": "NO_TRADE"},
        {"time": NOW, "action": "LONG", "outcome": "VETO: STALE_DATA", "violations": ["STALE_DATA"]},
        {"time": NOW, "action": "LONG", "outcome": "FILLED"},
    ]
    s = summarize([parse_record(r) for r in recs])
    assert s["total"] == 4 and s["counts"]["LONG"] == 2 and s["no_trade_share"] == 0.5
    assert s["vetoed"] == 1 and s["vetoes_by_code"] == {"STALE_DATA": 1}
    assert summarize([])["no_trade_share"] is None and summarize([])["last"] is None


# ------------------------------------------------------------------ vues


def test_portfolio_summary_uses_risk_limits():
    acc = AccountSnapshot(equity_eur=15.0, balance_eur=15.0, peak_equity_eur=24.0, used_margin_eur=3.0, open_exposure_lots=0.02, updated_at=NOW)
    curve = [(NOW - timedelta(days=2), 20.0), (NOW - timedelta(days=1), 24.0), (NOW, 15.0)]
    s = views.portfolio_summary(acc, curve, RiskLimits())
    assert s.net_pnl_eur == pytest.approx(-5.0) and s.net_return == pytest.approx(-0.25)
    assert s.drawdown == pytest.approx(1 - 15 / 24) and s.max_drawdown == pytest.approx(1 - 15 / 24)
    assert s.drawdown_floor_eur == pytest.approx(12.0) and s.distance_to_floor_eur == pytest.approx(3.0)
    assert s.free_margin_eur == pytest.approx(12.0) and s.exposure_capacity_lots == pytest.approx(0.98)


def test_floor_never_below_initial_capital_rule():
    acc = AccountSnapshot(8.0, 8.0, 9.0, 0.0, 0.0, NOW)
    s = views.portfolio_summary(acc, [], RiskLimits())
    assert s.peak_equity_eur == 20.0 and s.drawdown_floor_eur == 10.0 and s.distance_to_floor_eur == -2.0
    # Même plancher que le Risk Engine.
    engine = RiskEngine(RiskLimits(), InstrumentSpec(), BrokerCosts())
    assert engine.drawdown_floor_eur(9.0) == s.drawdown_floor_eur


def test_drawdown_series():
    pts = views.drawdown_series([(NOW, 10.0), (NOW, 12.0), (NOW, 9.0), (NOW, 12.0)])
    assert [round(v, 4) for _, v in pts] == [0.0, 0.0, -0.25, 0.0]


def test_trade_stats():
    trades = [
        {"net_pnl_eur": 2.0, "r_multiple": 1.0, "funding_eur": 0.1},
        {"gross_pnl_eur": -1.0, "funding_eur": 0.2, "fees_eur": 0.3, "r_multiple": -0.75},
        {"net_pnl_eur": None},
    ]
    s = views.trade_stats(trades)
    assert s.trades == 2 and s.net_pnl_eur == pytest.approx(0.5)
    assert s.win_rate == 0.5 and s.profit_factor == pytest.approx(2 / 1.5)
    assert s.avg_r == pytest.approx(0.125) and s.funding_eur == pytest.approx(0.3) and s.fees_eur == pytest.approx(0.3)
    assert views.trade_stats([]).win_rate is None


def test_heartbeat_status():
    assert views.heartbeat_status({"time": (NOW - timedelta(minutes=2)).isoformat()}, NOW)[0] == views.Status.OK
    assert views.heartbeat_status({"time": (NOW - timedelta(minutes=30)).isoformat()}, NOW)[0] == views.Status.STALE
    assert views.heartbeat_status({"time": (NOW - timedelta(hours=3)).isoformat()}, NOW)[0] == views.Status.DOWN
    assert views.heartbeat_status({"time": NOW.isoformat(), "status": "error"}, NOW)[0] == views.Status.ERROR
    assert views.heartbeat_status({}, NOW) == (views.Status.MISSING, None)
    assert views.fmt_age(timedelta(hours=26, minutes=5)) == "1 j 2 h"


# ------------------------------------------------------------------ graphiques


def test_chart_zone_matches_engine_and_confirmation_time():
    cs = make_candles([(100, 101, 99, 100.5), (100.5, 105, 100.4, 104.8), (104.8, 106, 102, 105.5)], minutes=60)
    data = charts.build_chart_data(cs, Timeframe.H1)
    fvg = [z for z in data.zones if z.kind == "FVG"]
    assert len(fvg) == 1
    z = fvg[0]
    assert z.bullish and z.bottom == 101 and z.top == 102
    assert z.start == cs[0].time
    assert z.confirmed_at == cs[2].time + timedelta(hours=1)  # clôture de la 3ᵉ bougie
    assert z.active and z.end == cs[-1].time + timedelta(hours=1)


def test_chart_zones_are_causal():
    """Ajouter des bougies ne crée ni ne déplace une zone confirmée plus tôt."""
    cs = random_walk(500)
    short = charts.build_chart_data(cs[:300], Timeframe.M5, visible_bars=300, include_inactive=True, max_zones=None, price_margin=1.0)
    full = charts.build_chart_data(cs, Timeframe.M5, visible_bars=500, include_inactive=True, max_zones=None, price_margin=1.0)
    cutoff = cs[299].time + timedelta(minutes=5)
    key = lambda z: (z.kind, z.bullish, z.start, z.confirmed_at, z.top, z.bottom)  # noqa: E731
    assert short.zones
    assert {key(z) for z in short.zones} == {key(z) for z in full.zones if z.confirmed_at <= cutoff}


def test_chart_filters_and_limits_zones():
    cs = random_walk(600)
    all_zones = charts.build_chart_data(cs, Timeframe.M5, visible_bars=100, include_inactive=True, max_zones=None)
    limited = charts.build_chart_data(cs, Timeframe.M5, visible_bars=100, include_inactive=True, max_zones=5)
    assert len(limited.zones) <= 5 and len(limited.candles) == 100
    lo = min(c.low for c in limited.candles) * 0.99
    hi = max(c.high for c in limited.candles) * 1.01
    assert all(z.top >= lo and z.bottom <= hi for z in all_zones.zones)
    newest = sorted(all_zones.zones, key=lambda z: z.confirmed_at)[-5:]
    assert {z.confirmed_at for z in limited.zones} == {z.confirmed_at for z in newest}
    active_only = charts.build_chart_data(cs, Timeframe.M5, visible_bars=100, max_zones=None)
    assert all(z.active for z in active_only.zones)
    rows = charts.zone_rows(limited)
    assert len(rows) == len(limited.zones) and {"type", "mitigation", "active"} <= set(rows[0])


def test_chart_empty_and_closures():
    assert charts.build_chart_data([], Timeframe.H1).empty
    cs = make_candles([(1, 2, 0.5, 1.5)] * 2, minutes=60)
    cs = [cs[0], type(cs[1])(cs[0].time + timedelta(hours=4), 1, 2, 0.5, 1.5)]
    assert charts.market_closures(cs, Timeframe.H1) == [cs[0].time + timedelta(hours=h) for h in (1, 2, 3)]


def test_figures_build_when_plotly_available():
    pytest.importorskip("plotly")
    from zoneinfo import ZoneInfo

    data = charts.build_chart_data(random_walk(300), Timeframe.M5)
    fig = charts.candlestick_figure(data, "US500 M5", tz=ZoneInfo("Europe/Paris"))
    assert len(fig.layout.shapes) == len(data.zones)
    assert charts.line_figure([(NOW, 1.0)], "x").data


# ------------------------------------------------------------------ store


def _store_with_m5(tmp_path, n: int = 48):
    from bot_trading.marketdata.bars import bar_record
    from bot_trading.marketdata.common import Provenance
    from bot_trading.marketdata.engine import DataEngine
    from bot_trading.marketdata.storage import ParquetStore

    cs = random_walk(n)
    store = ParquetStore(tmp_path / "store")
    recs = [bar_record("US500", Timeframe.M5, c, None) for c in cs]
    DataEngine(store).ingest_candles(recs, Provenance("dukascopy", "candles.dukascopy.us500.m5", "test://", NOW), Timeframe.M5)
    return store, cs


def test_load_candles_native_resampled_and_as_of(tmp_path):
    assert market.open_store(tmp_path / "absent") is None
    assert market.load_candles(None, "US500", Timeframe.H1, NOW).candles == []
    store, cs = _store_with_m5(tmp_path)
    now = cs[-1].time + timedelta(minutes=5)
    m5 = market.load_candles(store, "US500", Timeframe.M5, now)
    assert m5.source == "dukascopy (M5)" and len(m5.candles) == len(cs)
    # Bougie en cours exclue : la dernière M5 n'est connue qu'à sa clôture.
    assert len(market.load_candles(store, "US500", Timeframe.M5, now - timedelta(minutes=1)).candles) == len(cs) - 1
    h1 = market.load_candles(store, "US500", Timeframe.H1, now)
    assert h1.source == "dukascopy (M5 agrégé)" and len(h1.candles) == len(cs) // 12
    assert market.load_candles(store, "US500", Timeframe.H1, now, sources=("synthetic",)).candles == []
    assert market.latest_series(store, "market.cboe.vix", "close", now) == []


# ------------------------------------------------------------------ kill switch


def _order_context():
    account = AccountState(equity_eur=20.0, peak_equity_eur=20.0, used_margin_eur=0.0, open_exposure_lots=0.0)
    mkt = MarketState(bid=6800.0, ask=6800.6, quote_time=NOW, now=NOW, eurusd=1.16)
    order = OrderRequest("US500", Direction.BUY, 0.01, 6750.0, True)
    return order, account, mkt


def _engine():
    s = Settings()
    return RiskEngine(s.risk, s.instrument, s.costs)


def test_kill_switch_goes_through_risk_engine_and_persists(tmp_path):
    store = KillSwitchStore(tmp_path / "kill_switch.json")
    assert not store.load().engaged
    order, account, mkt = _order_context()
    assert _engine().check_order(order, account, mkt).approved  # ordre valide sans kill switch

    dashboard_engine = _engine()
    store.engage(dashboard_engine, "test", actor="dashboard", now=NOW)
    assert dashboard_engine.kill_switch

    # Un autre processus (boucle paper) recharge l'état : veto KILL_SWITCH.
    loop_engine = _engine()
    state = store.apply(loop_engine)
    assert state.engaged and state.reason == "test" and state.actor == "dashboard"
    decision = loop_engine.check_order(order, account, mkt)
    assert not decision.approved and Violation.KILL_SWITCH in decision.violations
    audit = [json.loads(x) for x in store.audit_path.read_text().splitlines()]
    assert audit == [{"action": "ENGAGE", "engaged": True, "changed_at": NOW.isoformat(), "reason": "test", "actor": "dashboard"}]


def test_kill_switch_reset_requires_human_confirmation(tmp_path):
    store = KillSwitchStore(tmp_path / "ks.json")
    engine = _engine()
    store.engage(engine, "stop")
    with pytest.raises(PermissionError):
        store.reset(engine, "oui")
    assert store.load().engaged and engine.kill_switch
    store.reset(engine, CONFIRMATION)
    assert not store.load().engaged and not engine.kill_switch


def test_corrupted_kill_switch_file_blocks(tmp_path):
    p = tmp_path / "ks.json"
    p.write_text("{corrompu")
    engine = _engine()
    state = KillSwitchStore(p).apply(engine)
    assert state.engaged and state.corrupted and engine.kill_switch


def test_kill_switch_cli(tmp_path, capsys):
    from bot_trading.__main__ import main

    p = str(tmp_path / "ks.json")
    assert main(["kill-switch", "--path", p, "--engage", "--reason", "cli"]) == 0
    assert KillSwitchStore(p).load().engaged
    assert main(["kill-switch", "--path", p, "--reset", "--confirm", "non"]) == 1
    assert main(["kill-switch", "--path", p, "--reset", "--confirm", CONFIRMATION]) == 0
    assert not KillSwitchStore(p).load().engaged


# ------------------------------------------------------------------ lecture seule


FORBIDDEN_CALLS = {"place_market_order", "close_position", "modify_stop", "reset_halt", "reset", "CapitalReadOnlyClient", "SimulatedBroker"}
FORBIDDEN_MODULES = {"requests", "urllib", "http", "socket", "websocket", "bot_trading.broker.simulated", "bot_trading.marketdata.sources", "bot_trading.marketdata.http"}


def test_dashboard_cannot_send_orders_or_use_network():
    files = sorted(DASHBOARD_DIR.glob("*.py"))
    assert {f.name for f in files} >= {"app.py", "runtime.py", "decision_feed.py", "charts.py", "views.py", "market.py", "demo.py"}
    for f in files:
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) or isinstance(node, ast.Name):
                name = node.attr if isinstance(node, ast.Attribute) else node.id
                assert name not in FORBIDDEN_CALLS, f"{f.name}: {name}"
            if isinstance(node, ast.Import):
                for a in node.names:
                    assert not any(a.name == m or a.name.startswith(m + ".") for m in FORBIDDEN_MODULES), f"{f.name}: import {a.name}"
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not any(node.module == m or node.module.startswith(m + ".") for m in FORBIDDEN_MODULES), f"{f.name}: from {node.module}"


def test_dashboard_launcher_binds_localhost_only():
    src = (DASHBOARD_DIR.parent / "__main__.py").read_text(encoding="utf-8")
    assert '"--server.address", "127.0.0.1"' in src
    assert '"--browser.gatherUsageStats", "false"' in src


# ------------------------------------------------------------------ démo


def test_demo_dataset_is_readable(tmp_path):
    from bot_trading.dashboard.demo import build_demo

    root = build_demo(tmp_path / "demo", days=20, now=NOW)
    r = RuntimeReader(root / "runtime")
    acc = r.account()
    assert acc is not None and acc.updated_at == NOW
    assert r.equity_curve() and r.regime()["regime"] == "RISK_ON" and r.experiments() and r.heartbeats()
    decisions = JsonlDecisionFeed(r).latest(10_000)
    assert decisions and all(d.time <= NOW for d in decisions)
    assert r.warnings == []
    store = market.open_store(root / "store")
    loaded = market.load_candles(store, "US500", Timeframe.H4, NOW, sources=("synthetic",))
    assert loaded.candles and loaded.candles[-1].time + timedelta(hours=4) <= NOW
