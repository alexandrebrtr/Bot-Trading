"""Macro Intelligence Agent (STRATEGY.md F1 à F5, F8, F11, F12).

Cœur déterministe : même données point-in-time, même ``asof`` → même sortie.
Aucune donnée n'est lue sans passer par ``PointInTimeData`` filtré à ``asof``.

Lecture non mécanique (F8) : une donnée d'activité forte n'est pas
automatiquement haussière. Quand l'inflation est au-dessus de la cible, un
emploi très fort est pénalisé (risque de taux plus hauts), et les composantes
taux (F5) et Fed (F4) traduisent la réaction effective des anticipations.

Pas de consensus (F12) : la surprise est mesurée contre le nowcast de la
Cleveland Fed quand il est archivé, sinon contre un modèle naïf (moyenne des
12 variations précédentes).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from bot_trading.config import TradingMode
from bot_trading.intelligence import series as S
from bot_trading.intelligence.data import Observation, PointInTimeData
from bot_trading.intelligence.fed import DEFAULT_SPEAKER_WEIGHTS, read_communications
from bot_trading.intelligence.llm import LLMAnalyst, NullAnalyst
from bot_trading.intelligence.schemas import EventWindow, Invalidation, MacroAssessment, SourceRef, SubScore
from bot_trading.intelligence.scoring import (
    aggregate,
    bias_from_score,
    collect_sources,
    scenarios,
    uncertainty,
    unavailable,
)
from bot_trading.intelligence.series import clip, squash

HIGH_IMPACT = ("CPI", "NFP", "PCE", "FOMC", "GDP", "RETAIL_SALES", "ISM")


@dataclass(frozen=True)
class MacroConfig:
    weights: dict[str, float] = field(
        default_factory=lambda: {
            "F1_inflation": 0.25,
            "F2_emploi": 0.20,
            "F3_activite": 0.15,
            "F4_fed": 0.20,
            "F5_taux": 0.20,
        }
    )
    inflation_target: float = 2.0
    inflation_comfort: float = 2.5  # niveau neutre pour les actions
    # Âge max de la dernière donnée (jours) avant de la déclarer périmée.
    max_age_monthly_days: int = 75
    max_age_weekly_days: int = 21
    max_age_daily_days: int = 10
    max_age_quarterly_days: int = 200
    fed_lookback_days: int = 60
    rate_change_window: int = 20  # observations quotidiennes
    # Fenêtre d'interdiction d'entrée autour des annonces (F11, 🎛️).
    blackout_before_hours: float = 12.0
    blackout_after_hours: float = 2.0
    event_horizon_days: int = 7
    event_kinds: tuple[str, ...] = HIGH_IMPACT
    # Marge maximale de l'avis LLM sur la composante Fed (part du score F4).
    llm_max_share: float = 0.3
    speaker_weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_SPEAKER_WEIGHTS))


def _fresh(obs: list[Observation], asof: datetime, max_days: int) -> bool:
    return bool(obs) and (asof - obs[-1].available_at) <= timedelta(days=max_days)


class MacroIntelligenceAgent:
    """Produit un ``MacroAssessment`` daté, sourcé et explicable."""

    def __init__(
        self,
        data: PointInTimeData,
        config: MacroConfig | None = None,
        llm: LLMAnalyst | None = None,
        mode: TradingMode = TradingMode.BACKTEST,
    ) -> None:
        self.data = data
        self.cfg = config or MacroConfig()
        # En backtest, le LLM est toujours ignoré : il connaît le futur.
        self.llm: LLMAnalyst = llm if (llm is not None and mode != TradingMode.BACKTEST) else NullAnalyst()
        self.mode = mode

    # ------------------------------------------------------------------
    def assess(self, asof: datetime) -> MacroAssessment:
        warnings: list[str] = []
        w = self.cfg.weights
        f1, infl_level = self._inflation(asof, w["F1_inflation"], warnings)
        f2, sahm = self._labor(asof, w["F2_emploi"], infl_level, warnings)
        f3 = self._activity(asof, w["F3_activite"], infl_level, warnings)
        f4, llm_used = self._fed(asof, w["F4_fed"], warnings)
        f5 = self._rates(asof, w["F5_taux"], warnings)
        subs = [f1, f2, f3, f4, f5]
        score, coverage = aggregate(subs)
        events = self.event_windows(asof)
        event_soon = any(e.scheduled_at - asof <= timedelta(hours=48) for e in events)
        unc = uncertainty(subs, coverage, event_soon)
        bias = bias_from_score(score)
        metrics = {k: v for s in subs for k, v in s.metrics.items()}
        return MacroAssessment(
            asof=asof,
            score=round(score, 4),
            bias=bias,
            uncertainty=unc,
            subscores=subs,
            scenarios=scenarios(score, unc, subs, "Macro US"),
            invalidations=self._invalidations(bias, metrics, sahm),
            sources=collect_sources(subs),
            warnings=warnings,
            upcoming_events=events,
            llm_used=llm_used,
        )

    # --- F1 inflation ---------------------------------------------------
    def _inflation(self, asof: datetime, weight: float, warnings: list[str]) -> tuple[SubScore, float | None]:
        core_cpi = self.data.series(S.CORE_CPI, asof)
        core_pce = self.data.series(S.CORE_PCE, asof)
        usable = [o for o in (core_cpi, core_pce) if _fresh(o, asof, self.cfg.max_age_monthly_days) and len(o) >= 7]
        if not usable:
            return unavailable("F1_inflation", weight, "Core CPI / Core PCE indisponibles ou périmés"), None
        ann3 = [S.annualized(o, 3) for o in usable]
        ann6 = [S.annualized(o, 6) for o in usable]
        level = sum(ann3) / len(ann3)
        trend = level - sum(ann6) / len(ann6)
        metrics = {"core_infl_3m_ann": round(level, 3), "core_infl_trend_3m_vs_6m": round(trend, 3)}
        sources = [S.ref(o[-1]) for o in usable] + [S.ref(o[-7]) for o in usable]

        surprise_term, surprise_note = 0.0, "pas de mesure de surprise"
        if _fresh(core_cpi, asof, self.cfg.max_age_monthly_days) and len(core_cpi) >= 2:
            mom = S.pct_change(core_cpi, 1)
            metrics["CPILFESL_MOM"] = round(mom, 3)
            nowcasts = [o for o in self.data.series(S.NOWCAST_CORE_CPI_MOM, asof) if o.period == core_cpi[-1].period]
            # le nowcast doit être antérieur à la publication pour être une attente
            nowcasts = [o for o in nowcasts if o.available_at < core_cpi[-1].available_at]
            if nowcasts:
                s = mom - nowcasts[-1].value
                metrics["core_cpi_surprise_vs_nowcast_pp"] = round(s, 3)
                surprise_term = -squash(s / 0.1)
                surprise_note = f"surprise vs nowcast Cleveland {s:+.2f} pt"
                sources.append(S.ref(nowcasts[-1], "attente (nowcast)"))
            else:
                st = S.statistical_surprise(core_cpi)
                if st is not None:
                    metrics["core_cpi_surprise_z"] = round(st[1], 3)
                    surprise_term = -squash(st[1] / 1.5)
                    surprise_note = f"surprise statistique z={st[1]:+.2f} (pas de consensus gratuit)"
        score = clip(
            -0.5 * squash((level - self.cfg.inflation_comfort) / 1.0)
            - 0.3 * squash(trend / 0.75)
            + 0.2 * surprise_term
        )
        why = (
            f"Inflation sous-jacente {level:.2f} % annualisée sur 3 mois (cible {self.cfg.inflation_target:.0f} %), "
            f"{'accélération' if trend > 0 else 'décélération'} de {abs(trend):.2f} pt vs 6 mois ; {surprise_note}."
        )
        return SubScore(component="F1_inflation", score=round(score, 4), weight=weight, rationale=why, metrics=metrics, sources=sources), level

    # --- F2 emploi -----------------------------------------------------
    def _labor(self, asof: datetime, weight: float, infl_level: float | None, warnings: list[str]) -> tuple[SubScore, float | None]:
        ur = self.data.series(S.UNRATE, asof)
        nfp = self.data.series(S.PAYROLLS, asof)
        claims = self.data.series(S.CLAIMS, asof)
        terms, metrics, sources, notes = [], {}, [], []
        sahm = None
        if _fresh(ur, asof, self.cfg.max_age_monthly_days) and len(ur) >= 15:
            avg3 = [sum(o.value for o in ur[i - 2 : i + 1]) / 3 for i in range(len(ur) - 13, len(ur))]
            sahm = avg3[-1] - min(avg3[:-1])
            metrics["SAHM"] = round(sahm, 3)
            terms.append((0.4, -squash(max(0.0, sahm) / 0.3)))
            sources.append(S.ref(ur[-1]))
            notes.append(f"indicateur de Sahm {sahm:.2f}")
        pay3 = None
        if _fresh(nfp, asof, self.cfg.max_age_monthly_days) and len(nfp) >= 4:
            pay3 = (nfp[-1].value - nfp[-4].value) / 3.0
            metrics["payrolls_3m_avg_k"] = round(pay3, 1)
            growth = squash(pay3 / 150.0)
            if infl_level is not None and infl_level > 3.0 and pay3 > 200:
                # F8 : emploi trop fort + inflation élevée → taux plus hauts
                growth -= 1.2 * squash((pay3 - 200) / 100.0) + 0.3
                notes.append("emploi fort lu négativement (inflation élevée, risque de taux)")
            terms.append((0.4, clip(growth)))
            sources.append(S.ref(nfp[-1]))
            notes.append(f"créations d'emplois {pay3:+.0f} k/mois sur 3 mois")
        if _fresh(claims, asof, self.cfg.max_age_weekly_days) and len(claims) >= 26:
            avg4 = sum(o.value for o in claims[-4:]) / 4
            avg26 = sum(o.value for o in claims[-26:]) / 26
            chg = (avg4 / avg26 - 1) * 100
            metrics["claims_4w_vs_26w_pct"] = round(chg, 2)
            terms.append((0.2, -squash(chg / 10.0)))
            sources.append(S.ref(claims[-1]))
            notes.append(f"inscriptions au chômage {chg:+.1f} % vs moyenne 26 sem.")
        if not terms:
            return unavailable("F2_emploi", weight, "Chômage, NFP et claims indisponibles ou périmés"), None
        tw = sum(t[0] for t in terms)
        score = clip(sum(a * b for a, b in terms) / tw)
        if sahm is not None and sahm >= 0.5:
            warnings.append(f"Règle de Sahm déclenchée ({sahm:.2f}) : risque de récession")
        return SubScore(component="F2_emploi", score=round(score, 4), weight=weight, rationale="; ".join(notes) + ".", metrics=metrics, sources=sources), sahm

    # --- F3 activité ----------------------------------------------------
    def _activity(self, asof: datetime, weight: float, infl_level: float | None, warnings: list[str]) -> SubScore:
        terms, metrics, sources, notes = [], {}, [], []
        ip = self.data.series(S.INDPRO, asof)
        if _fresh(ip, asof, self.cfg.max_age_monthly_days) and len(ip) >= 7:
            g = S.pct_change(ip, 6)
            metrics["indpro_6m_pct"] = round(g, 3)
            terms.append(squash(g / 2.0))
            sources.append(S.ref(ip[-1]))
            notes.append(f"production industrielle {g:+.1f} % sur 6 mois")
        rs = self.data.series(S.RETAIL_SALES, asof)
        if _fresh(rs, asof, self.cfg.max_age_monthly_days) and len(rs) >= 4:
            g = S.annualized(rs, 3)
            real = g - (infl_level if infl_level is not None else self.cfg.inflation_target)
            metrics["retail_real_3m_ann"] = round(real, 3)
            terms.append(squash(real / 3.0))
            sources.append(S.ref(rs[-1]))
            notes.append(f"ventes au détail réelles {real:+.1f} % annualisé")
        gdp = self.data.series(S.REAL_GDP, asof)
        if _fresh(gdp, asof, self.cfg.max_age_quarterly_days) and len(gdp) >= 2:
            q = ((gdp[-1].value / gdp[-2].value) ** 4 - 1) * 100
            metrics["gdp_qoq_ann"] = round(q, 3)
            terms.append(squash((q - 1.8) / 2.0))
            sources.append(S.ref(gdp[-1]))
            notes.append(f"PIB réel {q:+.1f} % annualisé")
        warnings_isms = "ISM non intégré (données propriétaires, DATA_SOURCES.md §8)"
        if warnings_isms not in warnings:
            warnings.append(warnings_isms)
        if not terms:
            return unavailable("F3_activite", weight, "Production, ventes et PIB indisponibles ou périmés")
        score = clip(sum(terms) / len(terms))
        return SubScore(component="F3_activite", score=round(score, 4), weight=weight, rationale="; ".join(notes) + ".", metrics=metrics, sources=sources)

    # --- F4 Fed ---------------------------------------------------------
    def _fed(self, asof: datetime, weight: float, warnings: list[str]) -> tuple[SubScore, bool]:
        since = asof - timedelta(days=self.cfg.fed_lookback_days * 3)
        comms = self.data.fed_communications(asof, since)
        readings = read_communications(comms, self.cfg.speaker_weights)
        recent = [r for r in readings if asof - r.comm.published_at <= timedelta(days=self.cfg.fed_lookback_days)]
        if not recent:
            return unavailable("F4_fed", weight, "Aucune communication Fed archivée sur la période"), False
        tw = sum(r.weight for r in recent)
        level = sum(r.tone * r.weight for r in recent) / tw
        with_prev = [r for r in recent if r.delta is not None]
        delta = sum(r.delta * r.weight for r in with_prev) / sum(r.weight for r in with_prev) if with_prev else None
        det = clip(-(0.4 * level + 0.6 * delta) if delta is not None else -0.6 * level)
        metrics = {"fed_tone_level": round(level, 3)}
        if delta is not None:
            metrics["fed_tone_delta"] = round(delta, 3)
        sources = [
            SourceRef(source="Fed RSS", identifier=r.comm.url or r.comm.id, available_at=r.comm.published_at, value=round(r.tone, 3), note=f"{r.comm.speaker} ({r.comm.kind})")
            for r in recent
        ]
        why = (
            f"Ton moyen {'hawkish' if level > 0 else 'dovish'} {level:+.2f}"
            + (f", évolution {delta:+.2f} vs interventions précédentes" if delta is not None else ", pas d'intervention antérieure comparable")
            + f" ({len(recent)} orateur(s))."
        )
        score, llm_used = det, False
        reading = self.llm.read_fed_tone([r.comm for r in recent])
        if reading is not None:
            share = self.cfg.llm_max_share * reading.confidence
            score = clip((1 - share) * det + share * (-reading.tone))
            llm_used = True
            metrics["llm_fed_tone"] = round(reading.tone, 3)
            metrics["llm_share"] = round(share, 3)
            why += f" Avis LLM ({self.llm.name}) ton {reading.tone:+.2f}, part {share:.0%}."
        alerts = getattr(self.llm, "last_alerts", [])
        for a in alerts:
            warnings.append(f"Contenu Fed suspect : {a}")
        return SubScore(component="F4_fed", score=round(score, 4), weight=weight, rationale=why, metrics=metrics, sources=sources), llm_used

    # --- F5 taux --------------------------------------------------------
    def _rates(self, asof: datetime, weight: float, warnings: list[str]) -> SubScore:
        n = self.cfg.rate_change_window
        y2 = self.data.series(S.UST2Y, asof)
        y10 = self.data.series(S.UST10Y, asof)
        mpt = self.data.series(S.MPT_CUTS_12M_BP, asof)
        terms, metrics, sources, notes = [], {}, [], []
        if _fresh(y2, asof, self.cfg.max_age_daily_days) and len(y2) > n:
            d = S.diff(y2, n) * 100
            metrics["DGS2_CHG20_BP"] = round(d, 1)
            terms.append((0.5, -squash(d / 25.0)))
            sources.append(S.ref(y2[-1]))
            notes.append(f"taux 2 ans {d:+.0f} pb sur {n} séances")
        if _fresh(y10, asof, self.cfg.max_age_daily_days) and len(y10) > n:
            d = S.diff(y10, n) * 100
            metrics["DGS10_CHG20_BP"] = round(d, 1)
            terms.append((0.2, -squash(d / 30.0)))
            sources.append(S.ref(y10[-1]))
            notes.append(f"taux 10 ans {d:+.0f} pb")
        if _fresh(mpt, asof, self.cfg.max_age_daily_days) and len(mpt) > n:
            d = S.diff(mpt, n)
            metrics["MPT_CUTS_CHG20_BP"] = round(d, 1)
            terms.append((0.3, squash(d / 25.0)))
            sources.append(S.ref(mpt[-1]))
            notes.append(f"baisses attendues sur 12 mois {d:+.0f} pb")
        if not terms:
            return unavailable("F5_taux", weight, "Taux 2/10 ans et anticipations indisponibles")
        tw = sum(t[0] for t in terms)
        score = clip(sum(a * b for a, b in terms) / tw)
        return SubScore(component="F5_taux", score=round(score, 4), weight=weight, rationale="; ".join(notes) + ".", metrics=metrics, sources=sources)

    # --- événements -----------------------------------------------------
    def event_windows(self, asof: datetime) -> list[EventWindow]:
        before = timedelta(hours=self.cfg.blackout_before_hours)
        after = timedelta(hours=self.cfg.blackout_after_hours)
        evs = self.data.scheduled_events(asof, asof - after, asof + timedelta(days=self.cfg.event_horizon_days))
        return [
            EventWindow(kind=e.kind, label=e.label, scheduled_at=e.scheduled_at, blackout_start=e.scheduled_at - before, blackout_end=e.scheduled_at + after)
            for e in evs
            if e.kind in self.cfg.event_kinds
        ]

    # --- invalidation ---------------------------------------------------
    @staticmethod
    def _invalidations(bias, metrics: dict[str, float], sahm: float | None) -> list[Invalidation]:
        from bot_trading.intelligence.schemas import Bias

        out = [
            Invalidation(metric="SAHM", operator=">=", threshold=0.5, description="Règle de Sahm déclenchée : risque de récession."),
        ]
        if bias == Bias.BEARISH:
            out += [
                Invalidation(metric="CPILFESL_MOM", operator="<=", threshold=0.2, description="Core CPI mensuel revenu à ≤ 0,2 % : pression inflationniste levée."),
                Invalidation(metric="DGS2_CHG20_BP", operator="<=", threshold=-25, description="Taux 2 ans en baisse de ≥ 25 pb sur 20 séances : détente monétaire."),
            ]
        else:
            out += [
                Invalidation(metric="CPILFESL_MOM", operator=">=", threshold=0.4, description="Core CPI mensuel ≥ 0,4 % : réaccélération de l'inflation."),
                Invalidation(metric="DGS2_CHG20_BP", operator=">=", threshold=30, description="Taux 2 ans en hausse de ≥ 30 pb sur 20 séances : resserrement anticipé."),
                Invalidation(metric="fed_tone_delta", operator=">=", threshold=0.4, description="Virage hawkish marqué de la Fed."),
            ]
        return out
