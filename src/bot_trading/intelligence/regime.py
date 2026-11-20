"""Market Regime Engine : combine macro, entreprises et intermarchés en un
régime ``RISK_ON`` / ``NEUTRE`` / ``RISK_OFF`` (STRATEGY.md §3.1, F6, F10, F11).

Sortie consommée par le décideur :
- ``allow_new_entries`` : faux en ``RISK_OFF`` ou dans une fenêtre d'annonce ;
- ``size_multiplier`` ≤ 1 : le régime peut réduire la taille, jamais l'augmenter ;
- ``min_setup_quality`` : seuil de qualité exigé des setups FVG/OB ;
- ``event_blackout`` : à transmettre tel quel à ``MarketState.event_blackout``.

Ce module n'importe ni le Risk Engine ni le broker et ne produit aucun ordre.
Les limites de risque (RiskLimits) restent hors de portée de toute analyse.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from bot_trading.config import TradingMode
from bot_trading.intelligence import series as S
from bot_trading.intelligence.data import PointInTimeData
from bot_trading.intelligence.equity import EquityConfig, EquityIntelligenceAgent
from bot_trading.intelligence.llm import LLMAnalyst
from bot_trading.intelligence.macro import MacroConfig, MacroIntelligenceAgent
from bot_trading.intelligence.schemas import Invalidation, Regime, RegimeAssessment, SubScore
from bot_trading.intelligence.scoring import aggregate, bias_from_score, collect_sources, scenarios, uncertainty, unavailable
from bot_trading.intelligence.series import clip, squash


@dataclass(frozen=True)
class RegimeConfig:
    w_macro: float = 0.45
    w_equity: float = 0.25
    w_intermarket: float = 0.30
    intermarket_weights: dict[str, float] = field(
        default_factory=lambda: {"F6_vix": 0.35, "F6_credit": 0.30, "F6_dollar": 0.15, "F6_courbe": 0.20}
    )
    risk_on_threshold: float = 0.20
    risk_off_threshold: float = -0.20
    hysteresis: float = 0.05
    window: int = 20
    max_age_daily_days: int = 10
    # Overrides
    vix_panic: float = 35.0
    hy_widening_panic_bp: float = 100.0
    max_uncertainty_for_risk_on: float = 0.75
    # Politique par régime (🎛️, F10)
    size_multiplier: dict[str, float] = field(default_factory=lambda: {"RISK_ON": 1.0, "NEUTRE": 0.5, "RISK_OFF": 0.0})
    min_setup_quality: dict[str, float] = field(default_factory=lambda: {"RISK_ON": 0.5, "NEUTRE": 0.8, "RISK_OFF": 1.0})


class MarketRegimeEngine:
    def __init__(
        self,
        data: PointInTimeData,
        config: RegimeConfig | None = None,
        macro_config: MacroConfig | None = None,
        equity_config: EquityConfig | None = None,
        llm: LLMAnalyst | None = None,
        mode: TradingMode = TradingMode.BACKTEST,
        fallback_universe: list[str] | None = None,
    ) -> None:
        self.data = data
        self.cfg = config or RegimeConfig()
        self.macro_agent = MacroIntelligenceAgent(data, macro_config, llm=llm, mode=mode)
        self.equity_agent = EquityIntelligenceAgent(data, equity_config, fallback_universe)

    def assess(self, asof: datetime, previous: Regime | None = None) -> RegimeAssessment:
        macro = self.macro_agent.assess(asof)
        equity = self.equity_agent.assess(asof)
        inter = self._intermarket(asof)
        inter_score, inter_cov = aggregate(inter)

        blocks = [
            SubScore(component="macro", score=macro.score, weight=self.cfg.w_macro, rationale=f"Biais macro {macro.bias.value}, incertitude {macro.uncertainty:.2f}."),
            SubScore(component="entreprises", score=equity.score, weight=self.cfg.w_equity, available=any(s.available for s in equity.subscores), rationale=f"Biais entreprises {equity.bias.value}, incertitude {equity.uncertainty:.2f}."),
            SubScore(component="intermarches", score=round(inter_score, 4), weight=self.cfg.w_intermarket, available=inter_cov > 0, rationale="Synthèse VIX, crédit, dollar, courbe."),
        ]
        score, coverage = aggregate(blocks)
        subs = inter + blocks
        warnings = list(dict.fromkeys(macro.warnings + equity.warnings))

        regime = self._threshold(score, previous)
        overrides: list[str] = []
        metrics = {k: v for s in inter for k, v in s.metrics.items()}
        metrics.update({k: v for s in macro.subscores for k, v in s.metrics.items()})
        vix = metrics.get("VIXCLS")
        if vix is not None and vix >= self.cfg.vix_panic:
            regime = Regime.RISK_OFF
            overrides.append(f"VIX {vix:.1f} ≥ {self.cfg.vix_panic:g} : RISK_OFF forcé")
        hy = metrics.get("HY_OAS_CHG20_BP")
        if hy is not None and hy >= self.cfg.hy_widening_panic_bp:
            regime = Regime.RISK_OFF
            overrides.append(f"Spreads HY +{hy:.0f} pb : RISK_OFF forcé")
        sahm = metrics.get("SAHM")
        if regime == Regime.RISK_ON and sahm is not None and sahm >= 0.5:
            regime = Regime.NEUTRE
            overrides.append("Règle de Sahm déclenchée : plafonné à NEUTRE")
        if regime == Regime.RISK_ON and macro.bias.value == "BEARISH":
            regime = Regime.NEUTRE
            overrides.append("Biais macro baissier : le fondamental domine, plafonné à NEUTRE")

        events = macro.upcoming_events + equity.upcoming_events
        active = [e for e in events if e.contains(asof)]
        event_soon = any(timedelta(0) <= e.scheduled_at - asof <= timedelta(hours=48) for e in events)
        unc = uncertainty(blocks, coverage, event_soon, extra=0.25 * (1 - inter_cov) * self.cfg.w_intermarket)
        unc = max(unc, round(macro.uncertainty * self.cfg.w_macro + equity.uncertainty * self.cfg.w_equity, 4))
        if regime == Regime.RISK_ON and unc > self.cfg.max_uncertainty_for_risk_on:
            regime = Regime.NEUTRE
            overrides.append(f"Incertitude {unc:.2f} trop élevée : plafonné à NEUTRE")

        blackout = bool(active)
        allow = regime != Regime.RISK_OFF and not blackout
        return RegimeAssessment(
            asof=asof,
            score=round(score, 4),
            bias=bias_from_score(score),
            uncertainty=unc,
            subscores=subs,
            scenarios=scenarios(score, unc, [s for s in subs if s.component in {"macro", "entreprises", "intermarches"}], "Régime US500"),
            invalidations=self._invalidations(regime) + macro.invalidations,
            sources=collect_sources(subs) + macro.sources + equity.sources,
            warnings=warnings,
            regime=regime,
            previous_regime=previous,
            allow_new_entries=allow,
            size_multiplier=self.cfg.size_multiplier[regime.value] if allow else 0.0,
            min_setup_quality=self.cfg.min_setup_quality[regime.value],
            event_blackout=blackout,
            active_events=active,
            overrides=overrides,
            macro=macro,
            equity=equity,
            llm_used=macro.llm_used,
        )

    def _threshold(self, score: float, previous: Regime | None) -> Regime:
        on, off, h = self.cfg.risk_on_threshold, self.cfg.risk_off_threshold, self.cfg.hysteresis
        if previous == Regime.RISK_ON and score >= on - h:
            return Regime.RISK_ON
        if previous == Regime.RISK_OFF and score <= off + h:
            return Regime.RISK_OFF
        if score >= on:
            return Regime.RISK_ON
        if score <= off:
            return Regime.RISK_OFF
        return Regime.NEUTRE

    # --- F6 intermarchés -----------------------------------------------
    def _fresh(self, obs, asof) -> bool:
        return bool(obs) and asof - obs[-1].available_at <= timedelta(days=self.cfg.max_age_daily_days)

    def _intermarket(self, asof: datetime) -> list[SubScore]:
        w, n = self.cfg.intermarket_weights, self.cfg.window
        out = []

        vix = self.data.series(S.VIX, asof)
        if self._fresh(vix, asof) and len(vix) >= 50:
            v = vix[-1].value
            m50 = sum(o.value for o in vix[-50:]) / 50
            score = -0.6 * squash((v - 18) / 8) - 0.4 * squash((v / m50 - 1) / 0.25)
            metrics = {"VIXCLS": v, "vix_vs_50d": round(v / m50, 3)}
            srcs = [S.ref(vix[-1])]
            v3 = self.data.series(S.VIX3M, asof)
            if self._fresh(v3, asof) and v3[-1].value > 0:
                ratio = v / v3[-1].value
                metrics["vix_term_ratio"] = round(ratio, 3)
                srcs.append(S.ref(v3[-1]))
                if ratio > 1:
                    score -= 0.3  # structure inversée : stress de court terme
            out.append(SubScore(component="F6_vix", score=round(clip(score), 4), weight=w["F6_vix"], rationale=f"VIX {v:.1f} ({v / m50:.2f}× sa moyenne 50 j).", metrics=metrics, sources=srcs))
        else:
            out.append(unavailable("F6_vix", w["F6_vix"], "VIX indisponible ou historique < 50 j"))

        hy = self.data.series(S.HY_OAS, asof)
        if self._fresh(hy, asof) and len(hy) > n:
            lvl, chg = hy[-1].value, S.diff(hy, n) * 100
            score = -0.5 * squash((lvl - 4.0) / 2.0) - 0.5 * squash(chg / 50.0)
            out.append(SubScore(component="F6_credit", score=round(clip(score), 4), weight=w["F6_credit"], rationale=f"Spread HY {lvl:.2f} % ({chg:+.0f} pb sur {n} séances).", metrics={"HY_OAS": lvl, "HY_OAS_CHG20_BP": round(chg, 1)}, sources=[S.ref(hy[-1])]))
        else:
            out.append(unavailable("F6_credit", w["F6_credit"], "Spreads HY indisponibles (3 ans d'historique sur FRED)"))

        usd = self.data.series(S.DOLLAR_BROAD, asof)
        if self._fresh(usd, asof) and len(usd) > n:
            chg = S.pct_change(usd, n)
            out.append(SubScore(component="F6_dollar", score=round(-squash(chg / 2.0), 4), weight=w["F6_dollar"], rationale=f"Dollar large (proxy DXY) {chg:+.1f} % sur {n} séances.", metrics={"USD_BROAD_CHG20_PCT": round(chg, 3)}, sources=[S.ref(usd[-1])]))
        else:
            out.append(unavailable("F6_dollar", w["F6_dollar"], "Proxy dollar (DTWEXBGS) indisponible ou en retard"))

        y2, y10 = self.data.series(S.UST2Y, asof), self.data.series(S.UST10Y, asof)
        if self._fresh(y2, asof) and self._fresh(y10, asof) and len(y2) > n and len(y10) > n:
            spread = y10[-1].value - y2[-1].value
            d2 = S.diff(y2, n) * 100
            d_spread = (spread - (y10[-1 - n].value - y2[-1 - n].value)) * 100
            # pentification par chute brutale du 2 ans = marché qui anticipe une récession
            panic = d2 <= -40 and d_spread >= 25
            score = 0.3 * squash(spread / 0.75) - (0.7 if panic else 0.0)
            why = f"Pente 10 ans − 2 ans {spread:+.2f} pt ({d_spread:+.0f} pb sur {n} séances)" + (", pentification de panique." if panic else ".")
            out.append(SubScore(component="F6_courbe", score=round(clip(score), 4), weight=w["F6_courbe"], rationale=why, metrics={"CURVE_10_2": round(spread, 3), "CURVE_CHG20_BP": round(d_spread, 1)}, sources=[S.ref(y2[-1]), S.ref(y10[-1])]))
        else:
            out.append(unavailable("F6_courbe", w["F6_courbe"], "Taux 2 et 10 ans indisponibles"))
        return out

    def _invalidations(self, regime: Regime) -> list[Invalidation]:
        if regime == Regime.RISK_OFF:
            return [
                Invalidation(metric="VIXCLS", operator="<", threshold=20, description="Retour du VIX sous 20."),
                Invalidation(metric="HY_OAS_CHG20_BP", operator="<", threshold=-30, description="Resserrement des spreads HY de ≥ 30 pb."),
            ]
        return [
            Invalidation(metric="VIXCLS", operator=">=", threshold=self.cfg.vix_panic, description="VIX en zone de panique."),
            Invalidation(metric="HY_OAS_CHG20_BP", operator=">=", threshold=self.cfg.hy_widening_panic_bp, description="Écartement brutal des spreads de crédit."),
        ]


def check_invalidations(assessment: RegimeAssessment, metrics: dict[str, float]) -> list[Invalidation]:
    """Renvoie les invalidations déclenchées par des métriques plus récentes."""
    return [i for i in assessment.invalidations if i.metric in metrics and i.is_triggered(metrics[i.metric])]
