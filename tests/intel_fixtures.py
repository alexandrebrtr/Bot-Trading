"""Jeux de données synthétiques point-in-time pour les tests d'analyse."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from bot_trading.intelligence import series as S
from bot_trading.intelligence.data import (
    EarningsReport,
    FedCommunication,
    IndexWeights,
    InMemoryPITStore,
    Observation,
)

UTC = timezone.utc
ASOF = datetime(2026, 11, 20, 21, 0, tzinfo=UTC)


def month_start(y: int, m: int) -> date:
    return date(y + (m - 1) // 12, (m - 1) % 12 + 1, 1)


def monthly(series_id: str, start_value: float, monthly_growth_pct: list[float] | float, n: int = 36, end: date = date(2026, 10, 1), lag_days: int = 14) -> list[Observation]:
    """Série mensuelle de niveau ; la période m est publiée le 1er du mois suivant + lag."""
    growth = monthly_growth_pct if isinstance(monthly_growth_pct, list) else [monthly_growth_pct] * n
    out, v = [], start_value
    for i in range(n):
        p = month_start(end.year, end.month - (n - 1 - i))
        v = v * (1 + growth[i] / 100) if i else v
        nxt = month_start(p.year, p.month + 1)
        out.append(Observation(series_id, p, v, datetime(nxt.year, nxt.month, nxt.day, 13, 30, tzinfo=UTC) + timedelta(days=lag_days)))
    return out


def daily(series_id: str, values: list[float], end: date = date(2026, 11, 19)) -> list[Observation]:
    n = len(values)
    out = []
    for i, v in enumerate(values):
        d = end - timedelta(days=n - 1 - i)
        out.append(Observation(series_id, d, v, datetime(d.year, d.month, d.day, 22, 0, tzinfo=UTC)))
    return out


def ramp(a: float, b: float, n: int) -> list[float]:
    return [a + (b - a) * i / (n - 1) for i in range(n)]


def weekly_claims(level_start: float, level_end: float, n: int = 40, end: date = date(2026, 11, 15)) -> list[Observation]:
    vals = ramp(level_start, level_end, n)
    out = []
    for i, v in enumerate(vals):
        d = end - timedelta(weeks=n - 1 - i)
        rel = d + timedelta(days=5)
        out.append(Observation(S.CLAIMS, d, v, datetime(rel.year, rel.month, rel.day, 13, 30, tzinfo=UTC)))
    return out


def fed(id_: str, speaker: str, when: datetime, text: str, kind: str = "speech") -> FedCommunication:
    return FedCommunication(id=id_, speaker=speaker, kind=kind, published_at=when, title=f"{speaker} remarks", text=text, url=f"https://www.federalreserve.gov/x/{id_}.htm")


def earnings_history(ticker: str, rev0: float, rev_growth_q: float, eps0: float, eps_growth_q: float, margin: float, margin_drift: float = 0.0, guidance=None, n: int = 8) -> list[EarningsReport]:
    out = []
    for i in range(n):
        pe = date(2024, 12, 31) + timedelta(days=91 * i)
        rev = rev0 * (1 + rev_growth_q) ** i
        eps = eps0 * (1 + eps_growth_q) ** i
        m = margin + margin_drift * i
        filed = datetime(pe.year, pe.month, pe.day, 21, 0, tzinfo=UTC) + timedelta(days=30)
        out.append(EarningsReport(ticker, pe, filed, rev, eps, rev * m, guidance if i == n - 1 else None, f"https://www.sec.gov/{ticker}/{i}"))
    return out


def build_store(kind: str = "goldilocks") -> InMemoryPITStore:
    st = InMemoryPITStore()
    good = kind == "goldilocks"
    # Inflation : ~2,2 % annualisé (goldilocks) vs ~4,8 % et accélération (stress)
    infl = [0.18] * 36 if good else [0.25] * 30 + [0.45] * 6
    st.add(*monthly(S.CORE_CPI, 300.0, infl), *monthly(S.CORE_PCE, 120.0, [g - 0.02 for g in infl], lag_days=28))
    # Chômage : stable 4,1 % vs remontée à 5,0 %
    ur = [4.1] * 36 if good else [4.0] * 28 + ramp(4.1, 5.0, 8)
    st.add(*[Observation(S.UNRATE, o.period, v, o.available_at) for o, v in zip(monthly(S.UNRATE, 1, 0.0), ur)])
    st.add(*monthly(S.PAYROLLS, 158000, 0.1 if good else [0.1] * 30 + [-0.05] * 6))
    st.add(*weekly_claims(220e3, 215e3) if good else weekly_claims(220e3, 300e3))
    st.add(*monthly(S.INDPRO, 103.0, 0.2 if good else -0.3), *monthly(S.RETAIL_SALES, 700e3, 0.45 if good else 0.0))
    # Taux
    st.add(*daily(S.UST2Y, ramp(3.9, 3.6, 60) if good else ramp(3.6, 4.3, 60)))
    st.add(*daily(S.UST10Y, ramp(4.3, 4.15, 60) if good else ramp(4.2, 4.6, 60)))
    # Intermarchés
    st.add(*daily(S.VIX, ramp(16, 14, 60) if good else ramp(18, 32, 60)))
    st.add(*daily(S.VIX3M, ramp(18, 17, 60) if good else ramp(19, 27, 60)))
    st.add(*daily(S.HY_OAS, ramp(3.2, 3.0, 60) if good else ramp(3.5, 4.4, 60)))
    st.add(*daily(S.DOLLAR_BROAD, ramp(121, 120, 60) if good else ramp(120, 124, 60)))
    st.add(*daily(S.BREADTH_PCT_ABOVE_200D, ramp(58, 66, 60) if good else ramp(55, 32, 60)))
    # Fed
    if good:
        st.add(
            fed("w1", "Warsh", datetime(2026, 9, 10, 15, tzinfo=UTC), "Inflation remains elevated and we are not yet confident."),
            fed("w2", "Warsh", datetime(2026, 11, 5, 15, tzinfo=UTC), "Inflation has eased and disinflation continues; a rate cut is appropriate as the labor market is cooling."),
            fed("s1", "FOMC", datetime(2026, 10, 29, 18, tzinfo=UTC), "The Committee decided to lower the target range; inflation has moved closer to 2 percent.", "statement"),
        )
    else:
        st.add(
            fed("w1", "Warsh", datetime(2026, 9, 10, 15, tzinfo=UTC), "Inflation has eased; a rate cut may be appropriate."),
            fed("w2", "Warsh", datetime(2026, 11, 5, 15, tzinfo=UTC), "Inflation remains elevated with upside risks to inflation; it is premature to cut and further tightening may be needed."),
            fed("s1", "FOMC", datetime(2026, 10, 29, 18, tzinfo=UTC), "Inflation remains elevated. The Committee will maintain a restrictive stance; higher for longer.", "statement"),
        )
    # Entreprises
    st.add(IndexWeights(datetime(2026, 11, 18, tzinfo=UTC), {"NVDA": 0.075, "MSFT": 0.065, "AAPL": 0.064, "AMZN": 0.04, "GOOGL": 0.035, "META": 0.03}))
    for t, w in [("NVDA", 0.075), ("MSFT", 0.065), ("AAPL", 0.064), ("AMZN", 0.04)]:
        if good:
            st.add(*earnings_history(t, 50e9, 0.04, 1.0, 0.05, 0.30, 0.003, "raised"))
        else:
            st.add(*earnings_history(t, 50e9, -0.01, 1.0, -0.04, 0.30, -0.006, "lowered"))
    return st
