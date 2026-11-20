"""Equity Intelligence Agent (STRATEGY.md F7).

- Les pondérations utilisées sont celles **archivées avant asof** (holdings SPY).
  Sans instantané, l'agent n'applique jamais les pondérations actuelles au
  passé : il passe en poids égaux et le signale.
- Les résultats sont ceux **déposés avant asof** (EDGAR, champ ``filed``).
- Pas de consensus EPS gratuit : la « surprise » est l'accélération de la
  croissance du BPA vs les 4 trimestres précédents de la même société.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from bot_trading.intelligence import series as S
from bot_trading.intelligence.data import EarningsReport, PointInTimeData
from bot_trading.intelligence.schemas import (
    CompanyNote,
    EquityAssessment,
    EventWindow,
    Invalidation,
    SourceRef,
    SubScore,
)
from bot_trading.intelligence.scoring import aggregate, bias_from_score, collect_sources, scenarios, uncertainty, unavailable
from bot_trading.intelligence.series import clip, squash


@dataclass(frozen=True)
class EquityConfig:
    top_n: int = 10
    blackout_top_n: int = 5
    max_report_age_days: int = 120
    max_weights_age_days: int = 10
    max_age_daily_days: int = 10
    w_earnings: float = 0.5
    w_breadth: float = 0.25
    w_sectors: float = 0.15
    w_valuation: float = 0.10
    # Résultats publiés souvent après la clôture : la réaction a lieu à l'ouverture suivante.
    earnings_blackout_before_hours: float = 6.0
    earnings_blackout_after_hours: float = 18.0
    event_horizon_days: int = 7


def _yoy(reports: list[EarningsReport], idx: int) -> EarningsReport | None:
    cur = reports[idx]
    for r in reversed(reports[:idx]):
        if timedelta(days=330) <= cur.period_end - r.period_end <= timedelta(days=400):
            return r
    return None


def _eps_growth(cur: EarningsReport, prev: EarningsReport) -> float | None:
    if abs(prev.eps_diluted) < 0.01:
        return None
    return (cur.eps_diluted - prev.eps_diluted) / abs(prev.eps_diluted)


def company_score(reports: list[EarningsReport]) -> tuple[float, dict[str, float], str] | None:
    """Score d'une société à partir de ses rapports (déjà filtrés point-in-time)."""
    if not reports:
        return None
    cur = reports[-1]
    prev = _yoy(reports, len(reports) - 1)
    terms, metrics, notes = [], {}, []
    if prev is not None and prev.revenue > 0:
        g = (cur.revenue / prev.revenue - 1) * 100
        metrics["revenue_yoy_pct"] = round(g, 2)
        terms.append((0.3, squash(g / 10.0)))
        notes.append(f"CA {g:+.1f} % a/a")
        eg = _eps_growth(cur, prev)
        if eg is not None:
            metrics["eps_yoy_pct"] = round(eg * 100, 2)
            terms.append((0.3, squash(eg * 100 / 15.0)))
            notes.append(f"BPA {eg * 100:+.1f} % a/a")
            # accélération vs les 4 trimestres précédents (substitut de surprise)
            past = []
            for i in range(len(reports) - 2, max(-1, len(reports) - 6), -1):
                p = _yoy(reports, i)
                if p is not None and (x := _eps_growth(reports[i], p)) is not None:
                    past.append(x)
            if past:
                acc = eg - sum(past) / len(past)
                metrics["eps_growth_acceleration_pct"] = round(acc * 100, 2)
                terms.append((0.1, squash(acc * 100 / 10.0)))
        if cur.operating_margin is not None and prev.operating_margin is not None:
            dm = (cur.operating_margin - prev.operating_margin) * 100
            metrics["op_margin_chg_pp"] = round(dm, 2)
            terms.append((0.15, squash(dm / 2.0)))
            notes.append(f"marge opérationnelle {dm:+.1f} pt")
    if cur.guidance is not None:
        g = {"raised": 0.8, "maintained": 0.0, "lowered": -1.0}[cur.guidance]
        terms.append((0.15, g))
        notes.append(f"guidance {cur.guidance}")
    if not terms:
        return None
    tw = sum(t[0] for t in terms)
    return clip(sum(a * b for a, b in terms) / tw), metrics, ", ".join(notes)


class EquityIntelligenceAgent:
    def __init__(self, data: PointInTimeData, config: EquityConfig | None = None, fallback_universe: list[str] | None = None) -> None:
        self.data = data
        self.cfg = config or EquityConfig()
        self.fallback_universe = fallback_universe or []

    def assess(self, asof: datetime) -> EquityAssessment:
        warnings: list[str] = []
        snap = self.data.index_weights(asof)
        if snap is None:
            universe = [(t, None) for t in self.fallback_universe[: self.cfg.top_n]]
            warnings.append("Pondérations inconnues à cette date : poids égaux (aucune pondération actuelle appliquée au passé)")
        else:
            universe = snap.top(self.cfg.top_n)
            if asof - snap.as_of > timedelta(days=self.cfg.max_weights_age_days):
                warnings.append(f"Instantané de pondérations ancien ({snap.as_of.date()})")

        earnings_sub, notes = self._earnings(asof, universe, snap)
        subs = [earnings_sub, self._breadth(asof), self._sectors(asof), self._valuation(asof)]
        score, coverage = aggregate(subs)
        events = self._earnings_events(asof, universe)
        event_soon = any(e.scheduled_at - asof <= timedelta(hours=48) for e in events)
        unc = uncertainty(subs, coverage, event_soon, extra=0.1 if snap is None else 0.0)
        bias = bias_from_score(score)
        sources = collect_sources(subs)
        if snap is not None:
            sources.append(SourceRef(source=snap.source, identifier="index_weights", available_at=snap.as_of, note=f"top {len(universe)}"))
        return EquityAssessment(
            asof=asof,
            score=round(score, 4),
            bias=bias,
            uncertainty=unc,
            subscores=subs,
            scenarios=scenarios(score, unc, subs, "Entreprises du S&P 500"),
            invalidations=[
                Invalidation(metric="BREADTH_PCT_ABOVE_200D", operator="<", threshold=40, description="Moins de 40 % des valeurs au-dessus de leur MM200 : participation trop étroite."),
                Invalidation(metric="top_weights_lowered_guidance", operator=">=", threshold=2, description="Au moins 2 des plus grosses pondérations abaissent leur guidance."),
            ],
            sources=sources,
            warnings=warnings,
            companies=notes,
            weights_asof=snap.as_of if snap else None,
            upcoming_events=events,
        )

    def _earnings(self, asof: datetime, universe, snap) -> tuple[SubScore, list[CompanyNote]]:
        notes, total_w, cov_w, acc, lowered = [], 0.0, 0.0, 0.0, 0
        sources = []
        for ticker, weight in universe:
            w = weight if weight is not None else 1.0
            total_w += w
            reports = self.data.earnings(asof, ticker)
            recent = reports and asof - reports[-1].filed_at <= timedelta(days=self.cfg.max_report_age_days)
            res = company_score(reports) if recent else None
            if res is None:
                continue
            s, metrics, why = res
            cur = reports[-1]
            lowered += cur.guidance == "lowered"
            acc += s * w
            cov_w += w
            notes.append(CompanyNote(ticker=ticker, weight=weight, filed_at=cur.filed_at, score=round(s, 4), metrics=metrics, rationale=why))
            sources.append(SourceRef(source="SEC EDGAR", identifier=cur.source_url or ticker, available_at=cur.filed_at, period=cur.period_end.isoformat(), value=cur.eps_diluted, note=f"{ticker} BPA dilué"))
        if cov_w == 0:
            return unavailable("F7_resultats", self.cfg.w_earnings, "Aucun résultat récent des plus grosses pondérations"), notes
        score = clip(acc / cov_w)
        cover = cov_w / total_w if total_w else 0.0
        metrics = {"earnings_weight_coverage": round(cover, 3), "top_weights_lowered_guidance": float(lowered)}
        why = f"Résultats de {len(notes)} grosse(s) pondération(s), couverture {cover:.0%} du poids du top {len(universe)}."
        return SubScore(component="F7_resultats", score=round(score, 4), weight=self.cfg.w_earnings, rationale=why, metrics=metrics, sources=sources), notes

    def _breadth(self, asof: datetime) -> SubScore:
        b = self.data.series(S.BREADTH_PCT_ABOVE_200D, asof)
        if not b or asof - b[-1].available_at > timedelta(days=self.cfg.max_age_daily_days):
            return unavailable("F7_breadth", self.cfg.w_breadth, "Breadth indisponible (source à brancher)")
        v = b[-1].value
        chg = S.diff(b, 20) if len(b) > 20 else 0.0
        score = clip(0.7 * squash((v - 50) / 20) + 0.3 * squash(chg / 10))
        return SubScore(component="F7_breadth", score=round(score, 4), weight=self.cfg.w_breadth, rationale=f"{v:.0f} % des valeurs au-dessus de leur MM200 ({chg:+.0f} pt sur 20 séances).", metrics={"BREADTH_PCT_ABOVE_200D": v}, sources=[S.ref(b[-1])])

    def _sectors(self, asof: datetime) -> SubScore:
        r = self.data.series(S.CYCLICAL_VS_DEFENSIVE, asof)
        if len(r) <= 20 or asof - r[-1].available_at > timedelta(days=self.cfg.max_age_daily_days):
            return unavailable("F7_secteurs", self.cfg.w_sectors, "Performance relative sectorielle indisponible")
        chg = S.pct_change(r, 20)
        return SubScore(component="F7_secteurs", score=round(squash(chg / 3.0), 4), weight=self.cfg.w_sectors, rationale=f"Cycliques vs défensives {chg:+.1f} % sur 20 séances.", metrics={"cyclical_vs_defensive_20d_pct": round(chg, 3)}, sources=[S.ref(r[-1])])

    def _valuation(self, asof: datetime) -> SubScore:
        ey = self.data.series(S.SP500_EARNINGS_YIELD, asof)
        y10 = self.data.series(S.UST10Y, asof)
        if not ey or not y10 or asof - ey[-1].available_at > timedelta(days=40):
            return unavailable("F7_valorisation", self.cfg.w_valuation, "Rendement des bénéfices de l'indice indisponible")
        erp = ey[-1].value - y10[-1].value
        return SubScore(component="F7_valorisation", score=round(squash((erp - 1.0) / 1.5), 4), weight=self.cfg.w_valuation, rationale=f"Prime de risque actions {erp:+.2f} pt (rendement des bénéfices − 10 ans).", metrics={"equity_risk_premium": round(erp, 3)}, sources=[S.ref(ey[-1]), S.ref(y10[-1])])

    def _earnings_events(self, asof: datetime, universe) -> list[EventWindow]:
        top = {t for t, _ in universe[: self.cfg.blackout_top_n]}
        before = timedelta(hours=self.cfg.earnings_blackout_before_hours)
        after = timedelta(hours=self.cfg.earnings_blackout_after_hours)
        evs = self.data.scheduled_events(asof, asof - after, asof + timedelta(days=self.cfg.event_horizon_days))
        return [
            EventWindow(kind="EARNINGS", label=e.label, scheduled_at=e.scheduled_at, blackout_start=e.scheduled_at - before, blackout_end=e.scheduled_at + after)
            for e in evs
            if e.kind == "EARNINGS" and e.ticker in top
        ]
