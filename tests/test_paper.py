"""Paper trading : même code que le backtest, reprise après redémarrage, kill switch,
fondamental point-in-time, publication pour le dashboard. Aucun réseau."""

from __future__ import annotations

import ast
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bot_trading.__main__ import main as cli
from bot_trading.backtest.engine import Backtester, BacktestConfig, PointInTimeView
from bot_trading.config import Settings, TradingMode
from bot_trading.core.decision import Action, Decision
from bot_trading.core.models import Candle, Timeframe
from bot_trading.dashboard.decision_feed import JsonlDecisionFeed
from bot_trading.dashboard.runtime import RuntimeReader
from bot_trading.marketdata.source import Bar, InMemoryPointInTimeSource
from bot_trading.marketdata.synthetic import generate_bars, synthetic_bias
from bot_trading.paper import PaperConfig, PaperTrader, RuntimeRegimeProvider
from bot_trading.paper.feeds import InMemoryBarFeed, StoreBarFeed
from bot_trading.paper.trader import STATE_FILE, archive_runtime, violations_of
from bot_trading.risk.kill_switch import KillSwitchStore
from bot_trading.strategy import DecisionStrategy, FundamentalView

UTC = timezone.utc
START = datetime(2018, 1, 1, tzinfo=UTC)
PAPER_DIR = Path(__file__).resolve().parents[1] / "src" / "bot_trading" / "paper"


class Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


def close_of(b: Bar) -> datetime:
    return b.time + timedelta(minutes=b.timeframe.minutes)


@pytest.fixture(scope="module")
def market():
    bars, labels = generate_bars(START, datetime(2019, 6, 1, tzinfo=UTC), Timeframe.H1, seed=6)
    pit = InMemoryPointInTimeSource(synthetic_bias(labels))
    return bars, pit


def pit_strategy(pit) -> DecisionStrategy:
    return DecisionStrategy(Settings(), fundamental_provider=lambda ctx: FundamentalView.from_pit(PointInTimeView(pit, ctx.now)))


def paper(tmp_path, bars, pit, clock, warmup_days=5000) -> PaperTrader:
    cfg = PaperConfig(runtime_dir=tmp_path, timeframe=Timeframe.H1, warmup=timedelta(days=warmup_days))
    return PaperTrader(InMemoryBarFeed(bars), Settings(), cfg, strategy=pit_strategy(pit), clock=clock)


def drive(trader: PaperTrader, clock: Clock, bars: list[Bar]) -> None:
    """Simule le temps réel : une bougie close à la fois, un cycle à chaque fois."""
    for b in bars:
        clock.t = close_of(b) + timedelta(seconds=30)
        trader.cycle()


def trades_of(path: Path) -> list[dict]:
    return [json.loads(x) for x in (path / "trades.jsonl").read_text().splitlines()] if (path / "trades.jsonl").exists() else []


def key(t) -> tuple:
    return (t["opened_at"], round(t["entry_price"], 6), t["size"], t["closed_at"], round(t["exit_price"], 6), t["exit_reason"])


def bt_key(t) -> tuple:
    return (t.opened_at.isoformat(), round(t.entry_price, 6), t.size, t.closed_at.isoformat(), round(t.exit_price, 6), t.exit_reason)


@pytest.fixture(scope="module")
def reference(market):
    """Backtest de référence : préchauffage jusqu'à ``split``, trading ensuite, sans clôture finale."""
    bars, pit = market
    split = 6000  # ≈ septembre 2018 : préchauffage D1 (EMA 200) suffisant
    cfg = BacktestConfig(settings=Settings(mode=TradingMode.BACKTEST), max_margin_fraction=0.5, close_at_end=False)
    res = Backtester(cfg, pit).run(pit_strategy(pit), bars, trade_start=bars[split].time)
    return split, res


def test_paper_reproduces_backtest_exactly(tmp_path, market, reference):
    bars, pit = market
    split, res = reference
    assert len(res.trades) >= 2, "la série doit produire des trades pour que la comparaison ait un sens"
    clock = Clock(close_of(bars[split - 1]) + timedelta(seconds=30))
    trader = paper(tmp_path, bars, pit, clock)
    assert trader.start() == []  # premier lancement : tout l'historique sert au préchauffage
    drive(trader, clock, bars[split:])
    got = [key(t) for t in trades_of(tmp_path)]
    assert got == [bt_key(t.trade) for t in res.trades]
    final = json.loads((tmp_path / "account.json").read_text())
    assert final["equity_eur"] == pytest.approx(res.final_equity_eur, abs=1e-9)
    assert final["mode"] == "paper" and final["real_orders"] is False


def test_restart_resumes_state_and_catches_up(tmp_path, market, reference):
    bars, pit = market
    split, res = reference
    clock = Clock(close_of(bars[split - 1]) + timedelta(seconds=30))
    first = paper(tmp_path, bars, pit, clock)
    first.start()
    # Arrêt au milieu d'une position ouverte, si possible (reprise la plus exigeante).
    stop_at = next(
        (i for i, t in enumerate(bars) if i > split + 200 and any(tr.trade.opened_at < t.time < tr.trade.closed_at for tr in res.trades)),
        split + 500,
    )
    drive(first, clock, bars[split:stop_at])
    assert first.broker.positions, "l'arrêt doit tomber pendant une position ouverte"
    state = json.loads((tmp_path / STATE_FILE).read_text())
    assert state["last_bar_time"] == bars[stop_at - 1].time.isoformat()

    # Redémarrage 30 bougies plus tard : nouvel objet, nouvelle stratégie, rattrapage dans l'ordre.
    gap = 30
    clock.t = close_of(bars[stop_at + gap - 1]) + timedelta(seconds=30)
    second = paper(tmp_path, bars, pit, clock)
    catch_up = second.start()
    assert [b.time for b in catch_up] == [b.time for b in bars[stop_at:stop_at + gap]]
    assert set(second.broker.positions) == {p["deal_id"] for p in state["broker"]["positions"]}
    second.cycle(catch_up)
    drive(second, clock, bars[stop_at + gap:])
    assert [key(t) for t in trades_of(tmp_path)] == [bt_key(t.trade) for t in res.trades]


def test_cycle_is_idempotent_and_never_processes_open_bar(tmp_path, market):
    bars, pit = market
    clock = Clock(close_of(bars[500]) + timedelta(seconds=1))
    trader = paper(tmp_path, bars[:600], pit, clock)
    trader.start()
    clock.t = close_of(bars[501]) - timedelta(seconds=1)  # bougie 501 encore en cours
    assert trader.cycle().new_bars == 0
    clock.t = close_of(bars[501])
    assert trader.cycle().new_bars == 1
    assert trader.cycle().new_bars == 0
    eq = (tmp_path / "equity.jsonl").read_text().splitlines()
    assert len(eq) == 1


class AlwaysLong:
    name = "always_long"

    def on_bar(self, ctx) -> Decision:
        if ctx.positions:
            return Decision.hold("en position")
        return Decision(Action.LONG, stop_level=ctx.candle.close - 40, reason="test", setup="TEST", meta={"stage": "LONG", "quality": 0.9})


def flat(n: int, start=datetime(2026, 11, 16, tzinfo=UTC)) -> list[Bar]:
    return [Bar(Candle(start + timedelta(hours=i), 6000, 6002, 5998, 6000), Timeframe.H1, 0.6) for i in range(n)]


def test_kill_switch_blocks_new_orders_and_is_logged(tmp_path):
    bars = flat(10)
    clock = Clock(close_of(bars[1]))
    cfg = PaperConfig(runtime_dir=tmp_path, timeframe=Timeframe.H1)
    trader = PaperTrader(InMemoryBarFeed(bars), Settings(), cfg, strategy=AlwaysLong(), clock=clock)
    trader.start()
    # Engagé « ailleurs » (dashboard) : la boucle doit le recharger avant tout ordre.
    from bot_trading.risk.engine import RiskEngine

    s = Settings()
    KillSwitchStore(tmp_path / "kill_switch.json").engage(RiskEngine(s.risk, s.instrument, s.costs), "test", actor="dashboard")
    drive(trader, clock, bars[2:6])
    assert not trader.broker.positions
    reader = RuntimeReader(tmp_path)
    decisions = JsonlDecisionFeed(reader).latest()
    assert decisions and all(d.outcome != "FILLED" for d in decisions)
    assert any("KILL_SWITCH" in d.violations for d in decisions)
    kinds = [e["kind"] for e in reader.risk_events()]
    assert "KILL_SWITCH" in kinds and "VETO" in kinds
    assert reader.account().mode == "paper"
    assert json.loads((tmp_path / "account.json").read_text())["kill_switch"] is True


def test_fill_published_with_costs_positions_and_dashboard_fields(tmp_path):
    bars = flat(12)
    clock = Clock(close_of(bars[1]))
    cfg = PaperConfig(runtime_dir=tmp_path, timeframe=Timeframe.H1)
    trader = PaperTrader(InMemoryBarFeed(bars), Settings(), cfg, strategy=AlwaysLong(), clock=clock)
    trader.start()
    drive(trader, clock, bars[2:5])
    reader = RuntimeReader(tmp_path)
    pos = reader.positions()
    assert len(pos) == 1
    p = pos[0]
    assert p["entry_price"] == pytest.approx(6000.6)  # achat à l'ask (spread payé)
    assert p["initial_stop"] == p["stop_level"]
    assert p["unrealized_pnl_eur"] < 0  # marqué au bid
    filled = [d for d in reader.read_jsonl("decisions.jsonl") if d["outcome"] == "FILLED"]
    assert filled and filled[0]["stage"] == "LONG" and filled[0]["quality"] == 0.9 and filled[0]["executed_at"]
    acc = reader.account()
    assert acc.open_exposure_lots == p["size"] and acc.used_margin_eur > 0
    # Reprise : la position et son stop d'ouverture sont rechargés.
    clock.t = close_of(bars[5])
    again = PaperTrader(InMemoryBarFeed(bars), Settings(), cfg, strategy=AlwaysLong(), clock=clock)
    again.start()
    assert list(again.broker.positions) == [p["deal_id"]]
    assert again.initial_stops[p["deal_id"]] == p["initial_stop"]


def test_trade_rows_carry_fields_for_improvement_loops(tmp_path):
    bars = flat(6)
    drop = Bar(Candle(bars[-1].time + timedelta(hours=1), 6000, 6000, 5900, 5910), Timeframe.H1, 0.6)
    bars = bars + [drop]
    clock = Clock(close_of(bars[1]))
    cfg = PaperConfig(runtime_dir=tmp_path, timeframe=Timeframe.H1)
    trader = PaperTrader(InMemoryBarFeed(bars), Settings(), cfg, strategy=AlwaysLong(), clock=clock)
    trader.start()
    drive(trader, clock, bars[2:])
    rows = RuntimeReader(tmp_path).trades()
    assert rows and rows[0]["exit_reason"] == "STOP"
    for k in ("deal_id", "opened_at", "closed_at", "net_pnl_eur", "r_multiple", "setup", "regime", "bias", "exit_reason", "decided_at", "initial_risk_eur"):
        assert k in rows[0]
    assert rows[0]["setup"] == "TEST" and rows[0]["initial_risk_eur"] > 0


def test_state_timeframe_mismatch_and_corruption_refuse_to_start(tmp_path):
    bars = flat(4)
    clock = Clock(close_of(bars[-1]))
    PaperTrader(InMemoryBarFeed(bars), Settings(), PaperConfig(runtime_dir=tmp_path), strategy=AlwaysLong(), clock=clock).start()
    with pytest.raises(RuntimeError, match="timeframe"):
        PaperTrader(InMemoryBarFeed(bars), Settings(), PaperConfig(runtime_dir=tmp_path, timeframe=Timeframe.M15), strategy=AlwaysLong(), clock=clock).start()
    (tmp_path / STATE_FILE).write_text("{cassé")
    with pytest.raises(RuntimeError, match="illisible"):
        PaperTrader(InMemoryBarFeed(bars), Settings(), PaperConfig(runtime_dir=tmp_path), strategy=AlwaysLong(), clock=clock).start()
    dest = archive_runtime(tmp_path, now=clock.t)
    assert dest is not None and (dest / STATE_FILE).exists() and not (tmp_path / STATE_FILE).exists()


# ------------------------------------------------------------------ fondamental


def regime_json(asof: datetime, **over) -> dict:
    from bot_trading.dashboard.demo import demo_regime

    d = demo_regime(asof)
    d.pop("demo")
    d.update(over)
    return d


def test_regime_provider_point_in_time(tmp_path):
    now = datetime(2026, 11, 20, 15, tzinfo=UTC)
    path = tmp_path / "regime.json"
    prov = RuntimeRegimeProvider(path, max_age=timedelta(hours=48))
    assert not prov.view(now).available and "absent" in prov.last_problem
    path.write_text(json.dumps(regime_json(now - timedelta(hours=1))))
    v = prov.view(now)
    assert v.available and v.regime == "RISK_ON" and v.allow_new_entries and 0 < v.size_multiplier <= 1
    assert not prov.view(now - timedelta(hours=2)).available  # publié après la décision : refusé
    assert not prov.view(now + timedelta(days=3)).available  # périmé
    demo = regime_json(now - timedelta(minutes=5))
    demo["demo"] = True
    path.write_text(json.dumps(demo))
    assert not prov.view(now).available and "démonstration" in prov.last_problem
    path.write_text("{pas du json")
    assert not prov.view(now).available


def test_unfavourable_regime_blocks_entries_and_tightens_without_close(tmp_path):
    """Défauts validés : RISK_OFF bloque les entrées et réduit la taille ; stop resserré, pas de clôture forcée."""
    from bot_trading.strategy.positions import ExitParams

    assert ExitParams().on_risk_off == "tighten"
    now = datetime(2026, 11, 20, 15, tzinfo=UTC)
    path = tmp_path / "regime.json"
    path.write_text(json.dumps(regime_json(
        now - timedelta(hours=1), regime="RISK_OFF", bias="BEARISH", allow_new_entries=False, size_multiplier=0.0,
    )))
    v = RuntimeRegimeProvider(path).view(now)
    assert v.available and v.regime == "RISK_OFF" and not v.allow_new_entries and v.size_multiplier == 0

    # Boucle complète avec la stratégie par défaut : chaque décision est refusée à l'étage fondamental.
    bars = flat(8, start=now)
    clock = Clock(close_of(bars[1]))
    trader = PaperTrader(InMemoryBarFeed(bars), Settings(), PaperConfig(runtime_dir=tmp_path), clock=clock)
    trader.start()
    drive(trader, clock, bars[2:])
    rows = RuntimeReader(tmp_path).read_jsonl("decisions.jsonl")
    assert rows and all(r["action"] == "NO_TRADE" and r["stage"] == "fondamental" for r in rows)
    assert "RISK_OFF" in rows[-1]["reason"]


def test_no_regime_means_no_trade(tmp_path):
    bars = flat(6)
    clock = Clock(close_of(bars[1]))
    trader = PaperTrader(InMemoryBarFeed(bars), Settings(), PaperConfig(runtime_dir=tmp_path), clock=clock)
    trader.start()
    drive(trader, clock, bars[2:])
    rows = RuntimeReader(tmp_path).read_jsonl("decisions.jsonl")
    assert rows and all(r["action"] == "NO_TRADE" for r in rows)
    assert "Fondamental indisponible" in rows[-1]["reason"]
    beat = json.loads((tmp_path / "heartbeat.json").read_text())["paper_loop"]
    assert "regime.json absent" in beat["message"]


# ------------------------------------------------------------------ données et sécurité


def test_store_feed_reads_closed_bars_with_spread(tmp_path):
    from bot_trading.marketdata.bars import bar_record
    from bot_trading.marketdata.common import Provenance
    from bot_trading.marketdata.engine import DataEngine
    from bot_trading.marketdata.storage import ParquetStore

    store = ParquetStore(tmp_path / "store")
    t0 = datetime(2026, 11, 20, 10, tzinfo=UTC)
    recs = []
    for i in range(4):
        bid = Candle(t0 + timedelta(hours=i), 6000 + i, 6003 + i, 5999 + i, 6001 + i)
        ask = Candle(bid.time, bid.open + 0.5, bid.high + 0.5, bid.low + 0.5, bid.close + 0.7)
        recs.append(bar_record("US500", Timeframe.H1, bid, ask))
    DataEngine(store).ingest_candles(recs, Provenance("capital", "candles.capital.us500.h1", "test://", t0, license_note="test"), Timeframe.H1)
    feed = StoreBarFeed(store, "US500", Timeframe.H1)
    bars = feed.bars(None, t0 + timedelta(hours=3))  # la 4ᵉ bougie n'est pas close
    assert [b.time for b in bars] == [t0, t0 + timedelta(hours=1), t0 + timedelta(hours=2)]
    assert bars[0].spread == pytest.approx(0.7) and bars[0].bid.close == 6001
    assert feed.last_source == "capital (H1)"
    assert StoreBarFeed(store, "US500", Timeframe.M15).bars(None, t0 + timedelta(days=1)) == []


def test_violations_parsing():
    assert violations_of("VETO: KILL_SWITCH, SPREAD_TOO_WIDE") == ["KILL_SWITCH", "SPREAD_TOO_WIDE"]
    assert violations_of("VETO: STOP_WRONG_SIDE (gap sous le stop)") == ["STOP_WRONG_SIDE"]
    assert violations_of("FILLED") == []


def test_paper_package_cannot_send_orders():
    """Seul ``feeds.CapitalRefresher`` touche Capital.com, et uniquement via le client en lecture seule."""
    for f in PAPER_DIR.glob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [node.module or ""] if isinstance(node, ast.ImportFrom) else [a.name for a in node.names]
                for n in names:
                    assert not n.startswith(("requests", "urllib", "http", "socket", "websocket")), (f.name, n)
                    if "capital" in n:
                        assert f.name == "feeds.py" and n in ("bot_trading.broker.capital_readonly", "bot_trading.marketdata.sources.capital"), (f.name, n)
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for frag in ("/positions", "/workingorders", "/confirms", "/accounts"):
                    assert frag not in node.value, (f.name, node.value)


def test_cli_status_and_reset(tmp_path, capsys):
    assert cli(["paper", "--runtime-dir", str(tmp_path), "status"]) == 0
    assert "Aucun compte paper" in capsys.readouterr().out
    bars = flat(4)
    PaperTrader(InMemoryBarFeed(bars), Settings(), PaperConfig(runtime_dir=tmp_path), strategy=AlwaysLong(), clock=Clock(close_of(bars[-1]))).start()
    assert cli(["paper", "--runtime-dir", str(tmp_path), "status"]) == 0
    out = capsys.readouterr().out
    assert "Equity 20.00" in out and "aucun ordre réel" in out
    assert cli(["paper", "--runtime-dir", str(tmp_path), "reset"]) == 1
    assert cli(["paper", "--runtime-dir", str(tmp_path), "reset", "--confirm", "JE_CONFIRME_LA_REPRISE"]) == 0
    assert not (tmp_path / "account.json").exists()


def test_dashboard_paper_status_line(tmp_path):
    from bot_trading.dashboard import views

    assert views.paper_status(None) == "" and views.paper_status({"equity_eur": 20}) == ""
    bars = flat(5)
    clock = Clock(close_of(bars[1]))
    trader = PaperTrader(InMemoryBarFeed(bars), Settings(), PaperConfig(runtime_dir=tmp_path), strategy=AlwaysLong(), clock=clock)
    trader.start()
    drive(trader, clock, bars[2:3])
    line = views.paper_status(RuntimeReader(tmp_path).read_json("account.json"))
    assert "simulés" in line and "LONG" in line and "dernière bougie" in line
