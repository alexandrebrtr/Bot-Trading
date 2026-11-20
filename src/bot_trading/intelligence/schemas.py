"""Sorties structurées (Pydantic) des agents d'analyse et du moteur de régime.

Ces modèles ne contiennent **aucun** champ d'ordre (epic, taille, stop) : un
agent d'analyse produit un biais, un régime et des conditions d'invalidation,
jamais un ordre. Le décideur et le Risk Engine restent seuls responsables de
l'exécution.

Garantie point-in-time : chaque ``SourceRef`` porte ``available_at`` (heure à
laquelle la donnée était publiquement disponible). Les évaluations refusent à la
construction toute source postérieure à ``asof``.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Bias(str, Enum):
    BULLISH = "BULLISH"
    NEUTRAL = "NEUTRAL"
    BEARISH = "BEARISH"


class Regime(str, Enum):
    """Régime de marché (STRATEGY.md §3.1)."""

    RISK_ON = "RISK_ON"
    NEUTRE = "NEUTRE"
    RISK_OFF = "RISK_OFF"


class SourceRef(_Frozen):
    """Donnée utilisée, avec son heure de disponibilité."""

    source: str  # ex. "ALFRED", "Cboe", "Fed RSS", "EDGAR"
    identifier: str  # série FRED, ticker, URL…
    available_at: datetime
    period: str | None = None  # période observée (ex. "2026-10")
    value: float | None = None
    note: str = ""


class SubScore(_Frozen):
    """Composante explicable d'un score (STRATEGY.md F13)."""

    component: str  # ex. "F1_inflation"
    score: float = Field(ge=-1.0, le=1.0)  # > 0 favorable aux actions US
    weight: float = Field(ge=0.0)
    available: bool = True
    rationale: str
    metrics: dict[str, float] = Field(default_factory=dict)
    sources: list[SourceRef] = Field(default_factory=list)


class Scenario(_Frozen):
    name: Literal["central", "haussier", "baissier"]
    probability: float = Field(ge=0.0, le=1.0)
    description: str
    implication: str


class Invalidation(_Frozen):
    """Condition vérifiable par machine qui invaliderait le biais."""

    metric: str  # identifiant de série ou de métrique
    operator: Literal[">", ">=", "<", "<="]
    threshold: float
    description: str

    def is_triggered(self, value: float) -> bool:
        return {
            ">": value > self.threshold,
            ">=": value >= self.threshold,
            "<": value < self.threshold,
            "<=": value <= self.threshold,
        }[self.operator]


class EventWindow(_Frozen):
    """Événement à risque et sa fenêtre d'interdiction d'entrée (F11)."""

    kind: str
    label: str
    scheduled_at: datetime
    blackout_start: datetime
    blackout_end: datetime

    def contains(self, t: datetime) -> bool:
        return self.blackout_start <= t <= self.blackout_end


class _Assessment(_Frozen):
    asof: datetime
    score: float = Field(ge=-1.0, le=1.0)
    bias: Bias
    uncertainty: float = Field(ge=0.0, le=1.0)
    subscores: list[SubScore]
    scenarios: list[Scenario]
    invalidations: list[Invalidation]
    sources: list[SourceRef]
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _point_in_time(self):
        all_sources = list(self.sources) + [s for sub in self.subscores for s in sub.sources]
        late = [s for s in all_sources if s.available_at > self.asof]
        if late:
            raise ValueError(
                "Look-ahead : sources postérieures à asof "
                + ", ".join(f"{s.identifier}@{s.available_at.isoformat()}" for s in late)
            )
        return self

    @field_validator("scenarios")
    @classmethod
    def _probabilities(cls, v: list[Scenario]):
        if v and abs(sum(s.probability for s in v) - 1.0) > 1e-6:
            raise ValueError("Les probabilités des scénarios doivent sommer à 1")
        return v


class MacroAssessment(_Assessment):
    """Sortie du Macro Intelligence Agent (F1 à F5)."""

    upcoming_events: list[EventWindow] = Field(default_factory=list)
    llm_used: bool = False


class CompanyNote(_Frozen):
    ticker: str
    weight: float | None  # pondération connue à asof, None si inconnue
    filed_at: datetime | None
    score: float = Field(ge=-1.0, le=1.0)
    metrics: dict[str, float] = Field(default_factory=dict)
    rationale: str


class EquityAssessment(_Assessment):
    """Sortie de l'Equity Intelligence Agent (F7)."""

    companies: list[CompanyNote] = Field(default_factory=list)
    weights_asof: datetime | None = None
    upcoming_events: list[EventWindow] = Field(default_factory=list)


class RegimeAssessment(_Assessment):
    """Sortie du Market Regime Engine : ce que le décideur consomme."""

    regime: Regime
    previous_regime: Regime | None = None
    allow_new_entries: bool
    # Multiplicateur appliqué à la taille avant le Risk Engine. Toujours ≤ 1 :
    # le régime peut réduire le risque, jamais l'augmenter.
    size_multiplier: float = Field(ge=0.0, le=1.0)
    # Qualité minimale de setup exigée (score FVG/OB ∈ [0 ; 1]).
    min_setup_quality: float = Field(ge=0.0, le=1.0)
    event_blackout: bool
    active_events: list[EventWindow] = Field(default_factory=list)
    overrides: list[str] = Field(default_factory=list)
    macro: MacroAssessment
    equity: EquityAssessment
    llm_used: bool = False

    @model_validator(mode="after")
    def _consistency(self):
        if self.regime == Regime.RISK_OFF and self.allow_new_entries:
            raise ValueError("RISK_OFF interdit toute nouvelle entrée (F10)")
        if self.event_blackout and self.allow_new_entries:
            raise ValueError("Fenêtre d'annonce : pas de nouvelle entrée (F11)")
        return self
