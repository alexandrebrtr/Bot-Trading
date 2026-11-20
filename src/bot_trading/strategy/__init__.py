"""Stratégie complète : Technical Engine, filtre d'obstacles, sizing, Decision Engine,
gestion des positions swing.

Dépendances : ``analysis`` (FVG, OB, structure), ``risk`` (pré-contrôle du veto),
``core``. Aucun import de ``broker`` ni de ``intelligence`` : la vue fondamentale
arrive par les séries point-in-time ou par une ``RegimeAssessment`` passée en valeur.
"""

from bot_trading.strategy.decision_engine import (
    AccountView,
    DecisionEngine,
    DecisionParams,
    DecisionStrategy,
)
from bot_trading.strategy.fundamental import FundamentalView, assessment_observation
from bot_trading.strategy.obstacles import ObstacleParams, check_obstacles
from bot_trading.strategy.positions import ExitParams, PositionManager
from bot_trading.strategy.sizing import DrawdownTiers, SizingResult, size_for_risk
from bot_trading.strategy.technical import Setup, TechnicalEngine, TechnicalParams, TechnicalSnapshot

__all__ = [
    "AccountView",
    "DecisionEngine",
    "DecisionParams",
    "DecisionStrategy",
    "DrawdownTiers",
    "ExitParams",
    "FundamentalView",
    "ObstacleParams",
    "PositionManager",
    "Setup",
    "SizingResult",
    "TechnicalEngine",
    "TechnicalParams",
    "TechnicalSnapshot",
    "assessment_observation",
    "check_obstacles",
    "size_for_risk",
]
