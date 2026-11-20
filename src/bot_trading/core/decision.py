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

    @classmethod
    def no_trade(cls, reason: str = "", **kw) -> "Decision":
        return cls(Action.NO_TRADE, reason=reason, **kw)

    @classmethod
    def hold(cls, reason: str = "", **kw) -> "Decision":
        return cls(Action.HOLD, reason=reason, **kw)
