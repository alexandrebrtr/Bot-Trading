"""Boucles d'amélioration encadrées (ROADMAP Phase 5).

- ``journal`` : décisions reliées à leurs résultats, lecture causale (``as_of``) ;
- ``post_trade`` : statistiques par setup, régime, qualité, sortie, et constats ;
- ``reevaluation`` : walk-forward des paramètres 🎛️, proposition jamais appliquée ;
- ``registry`` : ``experiments.jsonl`` en ajout seul, revue humaine tracée ;
- ``loop`` : un cycle complet daté.

Aucune de ces boucles ne modifie les limites du Risk Engine ni n'applique un
paramètre : elles proposent, Alexandre décide.
"""

from bot_trading.improvement.journal import DecisionJournal, JournalEntry, TradeOutcome, attach_forward_moves, from_backtest, from_runtime
from bot_trading.improvement.loop import CycleResult, run_cycle
from bot_trading.improvement.post_trade import Finding, PostTradeReport, analyze
from bot_trading.improvement.reevaluation import (
    PROTECTED,
    SEARCH_SPACE,
    Criteria,
    ParameterProposal,
    ProtectedParameterError,
    reevaluate,
    validate_grid,
)
from bot_trading.improvement.registry import ExperimentRegistry

__all__ = [
    "PROTECTED",
    "SEARCH_SPACE",
    "Criteria",
    "CycleResult",
    "DecisionJournal",
    "ExperimentRegistry",
    "Finding",
    "JournalEntry",
    "ParameterProposal",
    "PostTradeReport",
    "ProtectedParameterError",
    "TradeOutcome",
    "analyze",
    "attach_forward_moves",
    "from_backtest",
    "from_runtime",
    "reevaluate",
    "run_cycle",
    "validate_grid",
]
