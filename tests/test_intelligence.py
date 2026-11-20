"""Tests du Macro Intelligence Agent, de l'Equity Intelligence Agent et du
Market Regime Engine : déterminisme, point-in-time, règles F8/F10/F11/F12."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from intel_fixtures import ASOF, build_store, daily, monthly, ramp
from bot_trading.intelligence import (
    Bias,
    EquityIntelligenceAgent,
    InMemoryPITStore,
    MacroIntelligenceAgent,
    MarketRegimeEngine,
    Observation,
    Regime,
    ScheduledEvent,
    check_invalidations,
)
from bot_trading.intelligence import series as S
from bot_trading.intelligence.data import EarningsReport, IndexWeights
from bot_trading.intelligence.fed import tone
from bot_trading.intelligence.schemas import MacroAssessment, SourceRef

UTC = timezone.utc


# --- données point-in-time ----------------------------------------------------

def test_store_returns_vintage_known_at_asof():
    p = date(2026, 9, 1)
    first = Observation(S.PAYROLLS, p, 100.0, datetime(2026, 10, 3, 12, 30, tzinfo=UTC))
    rev = Observation(S.PAYROLLS, p, 80.0, datetime(2026, 11, 7, 13, 30, tzinfo=UTC))
    st = InMemoryPITStore().add(first, rev)
    assert st.series(S.PAYROLLS, datetime(2026, 10, 2, tzinfo=UTC)) == []
    assert st.series(S.PAYROLLS, datetime(2026, 10, 10, tzinfo=UTC))[0].value == 100.0
    assert st.series(S.PAYROLLS, datetime(2026, 11, 8, tzinfo=UTC))[0].value == 80.0


def test_naive_datetime_refused():
    with pytest.raises(ValueError):
        Observation("X", date(2026, 1, 1), 1.0, datetime(2026, 1, 2))


def test_event_dates_must_be_known():
    ev = ScheduledEvent("CPI", "CPI oct.", datetime(2026, 11, 13, 13, 30, tzinfo=UTC), known_since=datetime(2026, 11, 1, tzinfo=UTC))
    st = InMemoryPITStore().add(ev)
    win = (datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 11, 30, tzinfo=UTC))
    assert st.scheduled_events(datetime(2026, 10, 30, tzinfo=UTC), *win) == []
    assert st.scheduled_events(datetime(2026, 11, 2, tzinfo=UTC), *win) == [ev]


# --- anti look-ahead ---------------------------------------------------------

def _future_noise(store: InMemoryPITStore) -> InMemoryPITStore:
    """Ajoute des données publiées après ASOF, dont une révision et un krach."""
    after = ASOF + timedelta(hours=1)
    store.add(
        Observation(S.CORE_CPI, date(2026, 10, 1), 999.0, after),  # révision absurde
        Observation(S.CORE_CPI, date(2026, 11, 1), 999.0, after + timedelta(days=20)),
        Observation(S.VIX, date(2026, 11, 20), 80.0, after),
        Observation(S.HY_OAS, date(2026, 11, 20), 9.0, after),
        IndexWeights(after, {"ZZZ": 0.5}),
        EarningsReport("NVDA", date(2026, 10, 31), after, 1.0, -10.0, -5.0, "lowered"),
    )
    return store


@pytest.mark.parametrize("kind", ["goldilocks", "stress"])
def test_future_data_does_not_change_the_past(kind):
    a = MarketRegimeEngine(build_store(kind)).assess(ASOF)
    b = MarketRegimeEngine(_future_noise(build_store(kind))).assess(ASOF)
    assert a.model_dump() == b.model_dump()


def test_all_sources_available_before_asof():
    r = MarketRegimeEngine(build_store()).assess(ASOF)
    refs = r.sources + [s for sub in r.subscores + r.macro.subscores + r.equity.subscores for s in sub.sources]
    assert refs and all(s.available_at <= ASOF for s in refs)


def test_schema_rejects_lookahead_source():
    m = MacroIntelligenceAgent(build_store()).assess(ASOF)
    late = SourceRef(source="ALFRED", identifier="X", available_at=ASOF + timedelta(seconds=1))
    with pytest.raises(ValidationError):
        MacroAssessment(**{**m.model_dump(), "sources": [late.model_dump()]})


def test_deterministic():
    st = build_store()
    assert MarketRegimeEngine(st).assess(ASOF).model_dump_json() == MarketRegimeEngine(st).assess(ASOF).model_dump_json()


# --- Macro --------------------------------------------------------------------

def test_macro_bias_follows_fundamentals():
    good = MacroIntelligenceAgent(build_store("goldilocks")).assess(ASOF)
    bad = MacroIntelligenceAgent(build_store("stress")).assess(ASOF)
    assert good.bias == Bias.BULLISH and bad.bias == Bias.BEARISH
    for a in (good, bad):
        assert {s.component for s in a.subscores} == {"F1_inflation", "F2_emploi", "F3_activite", "F4_fed", "F5_taux"}
        assert abs(sum(s.probability for s in a.scenarios) - 1) < 1e-9
        assert a.invalidations and a.sources
        assert any("ISM" in w for w in a.warnings)
    assert any("Sahm" in w for w in bad.warnings)


def test_missing_data_raises_uncertainty_not_a_guess():
    empty = MacroIntelligenceAgent(InMemoryPITStore()).assess(ASOF)
    assert empty.bias == Bias.NEUTRAL and empty.score == 0
    assert empty.uncertainty >= 0.45
    assert not any(s.available for s in empty.subscores)


def _labor_store(infl_growth: float) -> InMemoryPITStore:
    st = InMemoryPITStore()
    st.add(*monthly(S.CORE_CPI, 300, infl_growth), *monthly(S.PAYROLLS, 158000, 0.25))
    return st


def test_strong_jobs_not_mechanically_bullish_when_inflation_high():
    """F8 : un emploi très fort est pénalisé quand l'inflation est élevée."""
    low = MacroIntelligenceAgent(_labor_store(0.17)).assess(ASOF)
    high = MacroIntelligenceAgent(_labor_store(0.40)).assess(ASOF)
    f2 = lambda a: next(s for s in a.subscores if s.component == "F2_emploi")
    assert f2(low).score > 0.5
    assert f2(high).score < 0
    assert "inflation élevée" in f2(high).rationale


def test_surprise_uses_nowcast_only_if_published_before_release():
    base = monthly(S.CORE_CPI, 300, 0.2)
    last = base[-1]
    actual_mom = (last.value / base[-2].value - 1) * 100
    before = Observation(S.NOWCAST_CORE_CPI_MOM, last.period, actual_mom - 0.2, last.available_at - timedelta(days=2))
    after = Observation(S.NOWCAST_CORE_CPI_MOM, last.period, actual_mom + 0.5, last.available_at + timedelta(hours=1))
    a = MacroIntelligenceAgent(InMemoryPITStore().add(*base, before)).assess(ASOF)
    b = MacroIntelligenceAgent(InMemoryPITStore().add(*base, after)).assess(ASOF)
    m_a = next(s for s in a.subscores if s.component == "F1_inflation").metrics
    m_b = next(s for s in b.subscores if s.component == "F1_inflation").metrics
    assert m_a["core_cpi_surprise_vs_nowcast_pp"] == pytest.approx(0.2, abs=1e-3)
    assert "core_cpi_surprise_vs_nowcast_pp" not in m_b and "core_cpi_surprise_z" in m_b


def test_fed_tone_lexicon_and_negation():
    assert tone("Inflation remains elevated; further tightening may be needed.")[0] > 0.3
    assert tone("A rate cut is appropriate; inflation has eased.")[0] < -0.3
    assert tone("It is not premature to cut.")[0] < 0
    assert tone("The weather was nice.") == (0.0, 0)


def test_fed_component_reads_change_of_tone():
    st = build_store("goldilocks")
    f4 = next(s for s in MacroIntelligenceAgent(st).assess(ASOF).subscores if s.component == "F4_fed")
    assert f4.metrics["fed_tone_delta"] < 0 and f4.score > 0  # virage dovish de Warsh
    assert any("Warsh" in s.note for s in f4.sources)


def test_event_windows():
    st = build_store()
    cpi = datetime(2026, 11, 21, 13, 30, tzinfo=UTC)
    st.add(ScheduledEvent("CPI", "CPI octobre", cpi, known_since=datetime(2026, 10, 1, tzinfo=UTC)))
    ev = MacroIntelligenceAgent(st).assess(ASOF).upcoming_events
    assert len(ev) == 1 and ev[0].blackout_start == cpi - timedelta(hours=12)
    assert ev[0].contains(cpi - timedelta(hours=1)) and not ev[0].contains(cpi + timedelta(hours=3))


# --- Entreprises -------------------------------------------------------------

def test_equity_bias():
    assert EquityIntelligenceAgent(build_store("goldilocks")).assess(ASOF).bias == Bias.BULLISH
    bad = EquityIntelligenceAgent(build_store("stress")).assess(ASOF)
    assert bad.bias == Bias.BEARISH
    f7 = next(s for s in bad.subscores if s.component == "F7_resultats")
    assert f7.metrics["top_weights_lowered_guidance"] >= 2


def test_equity_never_uses_current_weights_for_the_past():
    st = build_store()
    past = datetime(2026, 11, 1, tzinfo=UTC)  # avant l'instantané du 18/11
    a = EquityIntelligenceAgent(st, fallback_universe=["AAPL", "MSFT"]).assess(past)
    assert a.weights_asof is None
    assert all(c.weight is None for c in a.companies)
    assert any("poids égaux" in w for w in a.warnings)


def test_equity_ignores_reports_filed_after_asof():
    st = InMemoryPITStore().add(IndexWeights(datetime(2026, 11, 1, tzinfo=UTC), {"AAA": 0.1}))
    st.add(EarningsReport("AAA", date(2026, 9, 30), ASOF + timedelta(days=1), 10.0, 1.0, guidance="raised"))
    a = EquityIntelligenceAgent(st).assess(ASOF)
    assert a.companies == []


def test_earnings_blackout_for_top_weights():
    st = build_store()
    t = ASOF + timedelta(hours=2)
    st.add(ScheduledEvent("EARNINGS", "NVDA T3", t, known_since=ASOF - timedelta(days=30), ticker="NVDA"))
    st.add(ScheduledEvent("EARNINGS", "XYZ T3", t, known_since=ASOF - timedelta(days=30), ticker="XYZ"))
    r = MarketRegimeEngine(st).assess(ASOF)
    assert r.event_blackout and not r.allow_new_entries and r.size_multiplier == 0
    assert [e.label for e in r.active_events] == ["NVDA T3"]


# --- Régime --------------------------------------------------------------------

def test_regimes():
    on = MarketRegimeEngine(build_store("goldilocks")).assess(ASOF)
    off = MarketRegimeEngine(build_store("stress")).assess(ASOF)
    assert on.regime == Regime.RISK_ON and on.allow_new_entries and on.size_multiplier == 1.0
    assert off.regime == Regime.RISK_OFF and not off.allow_new_entries and off.size_multiplier == 0.0
    assert 0 <= on.size_multiplier <= 1 and on.min_setup_quality < off.min_setup_quality


def test_vix_panic_forces_risk_off():
    st = build_store("goldilocks")
    st._obs[S.VIX] = daily(S.VIX, ramp(16, 40, 60))
    r = MarketRegimeEngine(st).assess(ASOF)
    assert r.regime == Regime.RISK_OFF and any("VIX" in o for o in r.overrides)


def test_bearish_macro_caps_regime_even_if_markets_calm():
    st = build_store("stress")
    good = build_store("goldilocks")
    for sid in (S.VIX, S.VIX3M, S.HY_OAS, S.DOLLAR_BROAD, S.BREADTH_PCT_ABOVE_200D):
        st._obs[sid] = good._obs[sid]
    st._earnings, st._weights = good._earnings, good._weights
    r = MarketRegimeEngine(st).assess(ASOF)
    assert r.macro.bias == Bias.BEARISH
    assert r.regime != Regime.RISK_ON


def test_hysteresis():
    eng = MarketRegimeEngine(InMemoryPITStore())
    assert eng._threshold(0.17, None) == Regime.NEUTRE
    assert eng._threshold(0.17, Regime.RISK_ON) == Regime.RISK_ON
    assert eng._threshold(-0.17, Regime.RISK_OFF) == Regime.RISK_OFF
    assert eng._threshold(0.10, Regime.RISK_ON) == Regime.NEUTRE


def test_check_invalidations():
    r = MarketRegimeEngine(build_store("goldilocks")).assess(ASOF)
    hit = check_invalidations(r, {"VIXCLS": 36.0, "CPILFESL_MOM": 0.5})
    assert {i.metric for i in hit} == {"VIXCLS", "CPILFESL_MOM"}
    assert check_invalidations(r, {"VIXCLS": 15.0}) == []


def test_regime_output_has_no_order_fields():
    fields = set(MarketRegimeEngine(build_store()).assess(ASOF).model_dump())
    assert not fields & {"epic", "size", "stop_level", "direction", "order", "take_profit_level"}
