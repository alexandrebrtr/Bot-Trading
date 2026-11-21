"""Régime calculé automatiquement depuis le store (StorePITData → Market Regime Engine → regime.json),
alertes Telegram et comparaison aux trades manuels. Aucun réseau."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from intel_fixtures import ASOF, build_store
from bot_trading.__main__ import main as cli
from bot_trading.config import TradingMode
from bot_trading.intelligence.regime import MarketRegimeEngine
from bot_trading.marketdata.common import Provenance
from bot_trading.marketdata.engine import DataEngine
from bot_trading.marketdata.pit import StorePITData, floor_time, kind_of, speaker_of
from bot_trading.marketdata.storage import ParquetStore
from bot_trading.notify.telegram import NullNotifier, TelegramNotifier, notifier_from_env
from bot_trading.paper import compare as cmp
from bot_trading.paper.regime import RegimePublisher, RuntimeRegimeProvider, data_coverage, guard_coverage

UTC = timezone.utc


def prov(dataset: str, t: datetime) -> Provenance:
    return Provenance("test", dataset, "test://", t, license_note="test")


def to_store(mem, root) -> ParquetStore:
    """Écrit le jeu point-in-time des tests d'analyse dans un store Parquet, au format des connecteurs."""
    store = ParquetStore(root)
    eng = DataEngine(store)
    for sid, obs in mem._obs.items():
        rows = [{
            "series_id": sid, "observation_date": o.period, "realtime_start": o.available_at.date(), "realtime_end": date(9999, 12, 31),
            "value": o.value, "event_time": datetime(o.period.year, o.period.month, o.period.day, tzinfo=UTC), "available_at": o.available_at,
        } for o in obs]
        eng.ingest_fred(rows, prov(f"macro.fred.{sid.lower()}", ASOF), sid)
    rss = []
    for c in mem._fed:
        feed = "press_monetary" if c.kind == "statement" else "speeches"
        title = "Federal Reserve issues FOMC statement" if c.kind == "statement" else c.title
        rss.append({"guid": c.id, "feed": feed, "title": title, "summary": c.text, "category": "", "link": c.url,
                    "event_time": c.published_at, "available_at": c.published_at, "trust": "untrusted"})
    eng.ingest_fed_rss(rss, prov("news.fed_rss", ASOF))
    cal = [{"release_id": 10, "release_name": "Consumer Price Index", "release_date": date(2026, 11, 21),
            "event_time": datetime(2026, 11, 21, 5, tzinfo=UTC), "available_at": datetime(2026, 11, 1, tzinfo=UTC), "allow_available_before_event": True}]
    eng.ingest(cal, prov("macro.fred.release_calendar", ASOF), ("release_id", "release_date"))
    return store


def test_inmemory_fixture_has_expected_internals():
    mem = build_store()
    assert hasattr(mem, "_obs") and hasattr(mem, "_fed")


@pytest.mark.parametrize("kind", ["goldilocks", "stress"])
def test_store_adapter_reproduces_inmemory_assessment(tmp_path, kind):
    mem = build_store(kind)
    store = to_store(mem, tmp_path / "store")
    ref = MarketRegimeEngine(mem, mode=TradingMode.PAPER).assess(ASOF)
    got = MarketRegimeEngine(StorePITData(store), mode=TradingMode.PAPER).assess(ASOF)
    ref_sub = {s.component: s for s in ref.macro.subscores + [x for x in ref.subscores if x.component.startswith("F6")]}
    got_sub = {s.component: s for s in got.macro.subscores + [x for x in got.subscores if x.component.startswith("F6")]}
    for comp in ("F1_inflation", "F2_emploi", "F3_activite", "F5_taux", "F6_vix", "F6_credit", "F6_dollar", "F6_courbe"):
        assert got_sub[comp].available == ref_sub[comp].available, comp
        assert got_sub[comp].score == pytest.approx(ref_sub[comp].score, abs=1e-9), comp
    assert got_sub["F4_fed"].available
    assert got.macro.bias == ref.macro.bias
    if kind == "stress":
        assert got.regime.value == "RISK_OFF" and not got.allow_new_entries


def test_store_adapter_is_point_in_time(tmp_path):
    store = to_store(build_store(), tmp_path / "store")
    data = StorePITData(store)
    cpi = data.series("CPILFESL", ASOF)
    before = data.series("CPILFESL", cpi[-1].available_at - timedelta(seconds=1))
    assert len(before) == len(cpi) - 1
    assert all(o.available_at <= ASOF for o in cpi)
    comms = data.fed_communications(ASOF, ASOF - timedelta(days=120))
    assert {c.speaker for c in comms} == {"Warsh", "FOMC"} and all(c.published_at <= ASOF for c in comms)
    assert data.fed_communications(datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC))[-1].published_at < datetime(2026, 10, 1, tzinfo=UTC)
    ev = data.scheduled_events(ASOF, ASOF, ASOF + timedelta(days=3))
    assert [e.kind for e in ev] == ["CPI"] and ev[0].scheduled_at == datetime(2026, 11, 21, 13, 30, tzinfo=UTC)
    assert data.scheduled_events(datetime(2026, 10, 1, tzinfo=UTC), ASOF, ASOF + timedelta(days=3)) == []  # calendrier pas encore connu


def test_fallbacks_and_rss_parsing(tmp_path):
    store = ParquetStore(tmp_path / "store")
    eng = DataEngine(store)
    d = date(2026, 11, 19)
    eng.ingest_vix([{"symbol": "VIX", "date": d, "open": 15.0, "high": 16.0, "low": 14.0, "close": 15.5,
                     "event_time": datetime(2026, 11, 19, 14, 30, tzinfo=UTC), "available_at": datetime(2026, 11, 19, 22, tzinfo=UTC)}],
                   prov("market.cboe.vix", ASOF))
    vix = StorePITData(store).series("VIXCLS", ASOF)
    assert len(vix) == 1 and vix[0].value == 15.5
    assert StorePITData(store).series("VIXCLS", datetime(2026, 11, 19, 21, tzinfo=UTC)) == []
    assert speaker_of("Speech by Governor Waller on the economic outlook", "speeches") == "Waller"
    assert speaker_of("Jefferson, Monetary policy", "speeches") == "Jefferson"
    assert speaker_of("Federal Reserve issues FOMC statement", "press_monetary") == "FOMC"
    assert kind_of("Minutes of the Federal Open Market Committee", "press_monetary") == "minutes"
    assert kind_of("Federal Reserve Board announces approval", "press_monetary") is None
    assert kind_of("anything", "press_all") is None


def test_empty_store_never_allows_entries(tmp_path):
    from bot_trading.marketdata.pit import assess_regime

    a = guard_coverage(assess_regime(ParquetStore(tmp_path / "vide"), ASOF))
    assert data_coverage(a) == (0, 0)
    assert not a.allow_new_entries and a.size_multiplier == 0
    assert any("Données insuffisantes" in o for o in a.overrides)


def test_publisher_writes_point_in_time_history(tmp_path):
    store = to_store(build_store(), tmp_path / "store")
    runtime = tmp_path / "runtime"
    pub = RegimePublisher(store, runtime, minutes=60)
    t1 = datetime(2026, 11, 20, 15, 0, 30, tzinfo=UTC)
    a1 = pub.maybe_publish(t1)
    assert a1 is not None and a1.asof == datetime(2026, 11, 20, 15, tzinfo=UTC)
    assert a1.allow_new_entries and a1.regime.value == "RISK_ON"
    assert pub.maybe_publish(t1 + timedelta(minutes=20)) is None  # même heure
    a2 = pub.maybe_publish(t1 + timedelta(hours=1))
    assert a2 is not None and a2.previous_regime == a1.regime
    hist = (runtime / "regime_history.jsonl").read_text().splitlines()
    assert len(hist) == 2
    provider = RuntimeRegimeProvider(runtime / "regime.json")
    # Décision à la clôture de 15:00 : l'évaluation de 16:00 est refusée, celle de 15:00 est reprise de l'historique.
    v = provider.view(datetime(2026, 11, 20, 15, tzinfo=UTC))
    assert v.available and v.asof == a1.asof
    assert not provider.view(datetime(2026, 11, 20, 14, tzinfo=UTC)).available
    # Un nouveau publisher (redémarrage) ne republie pas une heure déjà évaluée.
    assert RegimePublisher(store, runtime, minutes=60).maybe_publish(t1 + timedelta(hours=1, minutes=5)) is None
    assert floor_time(datetime(2026, 11, 20, 15, 47, tzinfo=UTC), 15) == datetime(2026, 11, 20, 15, 45, tzinfo=UTC)


def test_cli_regime_publish(tmp_path, capsys):
    to_store(build_store(), tmp_path / "store")
    assert cli(["regime", "--asof", ASOF.isoformat(), "--store-dir", str(tmp_path / "store"), "--runtime-dir", str(tmp_path / "rt"), "--publish"]) == 0
    out = capsys.readouterr().out
    assert "RISK_ON" in out and "Publié" in out
    assert json.loads((tmp_path / "rt" / "regime.json").read_text())["regime"] == "RISK_ON"


# ------------------------------------------------------------------ Telegram


class FakeResp:
    def __init__(self, status=200):
        self.status_code = status


def test_telegram_disabled_without_env_and_never_leaks_token():
    assert isinstance(notifier_from_env({}), NullNotifier)
    assert isinstance(notifier_from_env({"TELEGRAM_BOT_TOKEN": "x"}), NullNotifier)
    sent = []
    n = TelegramNotifier("123:SECRET", "42", post=lambda url, **kw: sent.append((url, kw)) or FakeResp())
    assert n("ACHAT", "0.01 lot") is True
    url, kw = sent[0]
    assert url == "https://api.telegram.org/bot123:SECRET/sendMessage"
    assert kw["json"]["chat_id"] == "42" and "ACHAT" in kw["json"]["text"]
    assert "SECRET" not in repr(n)

    def boom(url, **kw):
        raise ConnectionError(f"impossible de joindre {url}")

    bad = TelegramNotifier("123:SECRET", "42", post=boom)
    assert bad("ACHAT", "x") is False and bad.errors == 1 and "SECRET" not in bad.last_error
    assert TelegramNotifier("123:SECRET", "42", post=lambda url, **kw: FakeResp(401))("A", "x") is False


def test_paper_loop_sends_alerts(tmp_path):
    from test_paper import AlwaysLong, Clock, close_of, drive, flat

    from bot_trading.config import Settings
    from bot_trading.marketdata.source import Bar
    from bot_trading.core.models import Candle, Timeframe
    from bot_trading.paper import PaperConfig, PaperTrader
    from bot_trading.paper.feeds import InMemoryBarFeed

    bars = flat(5)
    bars.append(Bar(Candle(bars[-1].time + timedelta(hours=1), 6000, 6000, 5900, 5910), Timeframe.H1, 0.6))
    got = []
    clock = Clock(close_of(bars[1]))
    trader = PaperTrader(InMemoryBarFeed(bars), Settings(), PaperConfig(runtime_dir=tmp_path), strategy=AlwaysLong(), clock=clock,
                         notifier=lambda kind, text: got.append(kind))
    trader.start()
    drive(trader, clock, bars[2:])
    assert "ACHAT" in got and "CLÔTURE" in got

    def broken(kind, text):
        raise RuntimeError("panne")

    t2 = PaperTrader(InMemoryBarFeed(bars), Settings(), PaperConfig(runtime_dir=tmp_path / "b"), strategy=AlwaysLong(), clock=Clock(close_of(bars[1])), notifier=broken)
    t2.start()
    drive(t2, t2.clock, bars[2:])  # une alerte en panne n'arrête pas la boucle


# ------------------------------------------------------------------ comparaison manuelle


def test_compare_with_manual_journal(tmp_path, capsys):
    rt = tmp_path / "rt"
    rt.mkdir()
    rows = [
        {"opened_at": "2026-11-02T14:00:00+00:00", "closed_at": "2026-11-04T10:00:00+00:00", "net_pnl_eur": 1.5},
        {"opened_at": "2026-11-10T09:00:00+00:00", "closed_at": "2026-11-11T09:00:00+00:00", "net_pnl_eur": -0.8},
    ]
    (rt / "trades.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    csv_path = tmp_path / "manuel.csv"
    csv_path.write_text(
        "date,direction,entry_price,exit_price,pnl\n"
        "2026-11-02T16:00:00,LONG,6800,6820,20\n"
        "2026-11-06,SHORT,6850,6830,15\n"
        "2026-10-01,LONG,6700,6650,-50\n"
    )
    rep = cmp.compare(cmp.paper_trades(rt), cmp.load_manual(csv_path))
    assert rep["paper"]["trades"] == 2 and rep["paper"]["pnl"] == pytest.approx(0.7)
    assert rep["manual"]["trades"] == 2 and rep["manual"]["short_trades"] == 1  # le trade d'octobre est hors période
    assert rep["matched"] == 1 and rep["paper_only"] == 1 and rep["manual_only"] == 1
    assert cli(["paper", "--runtime-dir", str(rt), "compare", "--journal", str(csv_path)]) == 0
    out = capsys.readouterr().out
    assert "Entrées concordantes" in out and (rt / "manual_comparison.json").exists()
    assert cmp.compare([], [])["period"] is None


def test_compare_reads_journal_xlsx(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Trades"
    ws.append(["-", "Type Compte", "Symbole", "Direction", "Taille Position", "Prix Entrée", "Prix Sortie", "P&L Net ($)"])
    ws.append([datetime(2026, 11, 2, 15), "Live", "SP500", "LONG", 0.01, 6800, 6820, 2.0])
    ws.append([datetime(2026, 11, 3, 15), "Prop Firm", "MES", "LONG", 1, 6800, 6790, -5.0])
    ws.append([datetime(2026, 11, 4), "Live", "SP500", None, None, None, None, 0.3])  # dividende
    p = tmp_path / "TJ_test.xlsx"
    wb.save(p)
    m = cmp.load_manual(p)
    assert len(m) == 1 and m[0].pnl == 2.0 and m[0].direction == "LONG"
