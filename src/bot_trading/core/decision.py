"""Décision produite par une stratégie (mission, section 9 : LONG, HOLD, CLOSE, NO_TRADE).

Une ``Decision`` est une **intention** : elle ne contient pas de taille en lots
par défaut. Le dimensionnement (``risk_fraction`` → taille) et le veto sont
faits ensuite par le backtester / l'Execution Engine et le Risk Engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Action(str, Enum):
    LONG = "LONG"
    HOLD = "HOLD"
    CLOSE = "CLOSE"
    NO_TRADE = "NO_TRADE"


@dataclass(frozen=True)
class Decision:
    action: Action
    # Niveau d'invalidation (stop garanti) pour LONG ; nouveau stop (remontée seulement) pour HOLD.
    stop_level: float | None = None
    take_profit_level: float | None = None
    # Fraction de l'equity risquée au stop. Le Risk Engine applique ensuite ses plafonds.
    risk_fraction: float | None = None
    # Taille explicite en lots (prioritaire sur risk_fraction si fournie).
    size: float | None = None
    reason: str = ""
    confidence: float | None = None
    setup: str = ""  # configuration technique (ex. "FVG_H1_50")
    regime: str = ""  # régime / biais fondamental au moment de la décision
    bias: str = ""  # "bullish" | "neutral" | "bearish" (prévision directionnelle)
    sources: tuple[str, ...] = ()
    meta: dict = field(default_factory=dict)
    # Champs exigés par la mission (§9, Decision Engine) en plus des précédents.
    entry_conditions: tuple[str, ...] = ()
    invalidation_conditions: tuple[str, ...] = ()
    estimated_risk_eur: float | None = None
    holding_period: str = ""  # durée de détention envisagée

    # Champs que toute décision du Decision Engine doit renseigner (mission §9).
    REQUIRED_FIELDS = (
        "reason",  # justification
        "bias",  # biais fondamental
        "setup",  # configuration technique
        "confidence",  # niveau de confiance
        "entry_conditions",
        "invalidation_conditions",
        "estimated_risk_eur",  # risque estimé
        "holding_period",
        "sources",
    )

    def missing_fields(self) -> list[str]:
        """Champs de la mission non renseignés (vide = décision complète)."""
        out = []
        for name in self.REQUIRED_FIELDS:
            v = getattr(self, name)
            if v is None or v == "" or v == ():
                out.append(name)
        return out

    def to_dict(self) -> dict:
        """Forme sérialisable pour le journal de décision (JSON)."""
        return {
            "action": self.action.value,
            "justification": self.reason,
            "biais_fondamental": self.bias,
            "regime": self.regime,
            "configuration_technique": self.setup,
            "confiance": self.confidence,
            "conditions_entree": list(self.entry_conditions),
            "conditions_invalidation": list(self.invalidation_conditions),
            "risque_estime_eur": self.estimated_risk_eur,
            "fraction_risque": self.risk_fraction,
            "taille": self.size,
            "stop": self.stop_level,
            "take_profit": self.take_profit_level,
            "duree_envisagee": self.holding_period,
            "sources": list(self.sources),
            "meta": {k: v for k, v in self.meta.items() if isinstance(v, (str, int, float, bool, type(None), list, dict))},
        }

    @classmethod
    def no_trade(cls, reason: str = "", **kw) -> "Decision":
        return cls(Action.NO_TRADE, reason=reason, **kw)

    @classmethod
    def hold(cls, reason: str = "", **kw) -> "Decision":
        return cls(Action.HOLD, reason=reason, **kw)
