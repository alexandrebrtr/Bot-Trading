"""Agrégation déterministe partagée par les agents : score, biais, incertitude,
scénarios."""

from __future__ import annotations

from statistics import pstdev

from bot_trading.intelligence.schemas import Bias, Scenario, SourceRef, SubScore
from bot_trading.intelligence.series import clip


def unavailable(component: str, weight: float, reason: str) -> SubScore:
    return SubScore(component=component, score=0.0, weight=weight, available=False, rationale=reason)


def aggregate(subs: list[SubScore]) -> tuple[float, float]:
    """Moyenne pondérée des composantes disponibles et couverture (poids dispo / poids total)."""
    total = sum(s.weight for s in subs)
    avail = [s for s in subs if s.available and s.weight > 0]
    w = sum(s.weight for s in avail)
    if w == 0 or total == 0:
        return 0.0, 0.0
    return clip(sum(s.score * s.weight for s in avail) / w), w / total


def bias_from_score(score: float, threshold: float = 0.15) -> Bias:
    if score >= threshold:
        return Bias.BULLISH
    if score <= -threshold:
        return Bias.BEARISH
    return Bias.NEUTRAL


def uncertainty(subs: list[SubScore], coverage: float, event_soon: bool = False, extra: float = 0.0) -> float:
    """0 = signaux complets et concordants, 1 = données absentes ou contradictoires."""
    scores = [s.score for s in subs if s.available]
    dispersion = pstdev(scores) if len(scores) >= 2 else 0.5
    u = 0.45 * (1.0 - coverage) + 0.35 * min(1.0, dispersion / 0.6) + (0.2 if event_soon else 0.0) + extra
    return round(clip(u, 0.0, 1.0), 4)


def drivers(subs: list[SubScore], n: int = 2) -> tuple[list[SubScore], list[SubScore]]:
    avail = [s for s in subs if s.available]
    pos = sorted((s for s in avail if s.score > 0.05), key=lambda s: -s.score * s.weight)[:n]
    neg = sorted((s for s in avail if s.score < -0.05), key=lambda s: s.score * s.weight)[:n]
    return pos, neg


def scenarios(score: float, unc: float, subs: list[SubScore], subject: str) -> list[Scenario]:
    """Trois scénarios dont les probabilités dérivent du score et de l'incertitude.

    Les probabilités ne sont pas calibrées : elles ordonnent les scénarios et
    seront recalibrées par backtest (fréquences observées par niveau de score).
    """
    tilt = score * (1.0 - unc)
    p_up = clip(0.25 + 0.35 * tilt, 0.05, 0.7)
    p_down = clip(0.25 - 0.35 * tilt, 0.05, 0.7)
    p_mid = 1.0 - p_up - p_down
    pos, neg = drivers(subs)
    pos_txt = ", ".join(s.component for s in pos) or "aucun moteur positif net"
    neg_txt = ", ".join(s.component for s in neg) or "aucun frein net"
    return [
        Scenario(
            name="central",
            probability=round(p_mid, 4),
            description=f"{subject} : prolongation des tendances actuelles (soutiens : {pos_txt} ; freins : {neg_txt}).",
            implication="Suivre le régime courant ; entrées seulement sur setups conformes.",
        ),
        Scenario(
            name="haussier",
            probability=round(p_up, 4),
            description=(
                f"{subject} : les soutiens ({pos_txt}) s'amplifient et les freins s'estompent."
                if pos
                else f"{subject} : retournement favorable non anticipé (aucun soutien net aujourd'hui)."
            ),
            implication="Contexte favorable aux longs swing sur retracement.",
        ),
        Scenario(
            name="baissier",
            probability=round(1.0 - p_mid - p_up, 4),
            description=(
                f"{subject} : les freins ({neg_txt}) dominent."
                if neg
                else f"{subject} : choc non anticipé (aucun frein net aujourd'hui)."
            ),
            implication="Pas de nouvelle entrée ; protéger les positions ouvertes.",
        ),
    ]


def collect_sources(subs: list[SubScore]) -> list[SourceRef]:
    seen, out = set(), []
    for s in subs:
        for r in s.sources:
            key = (r.identifier, r.period, r.available_at)
            if key not in seen:
                seen.add(key)
                out.append(r)
    return out
