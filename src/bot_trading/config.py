"""Configuration centralisée.

Les valeurs par défaut reprennent la mission (US500, 20 EUR, levier 20:1,
0,01 lot, exposition max 1 lot, stop garanti obligatoire, shorts désactivés,
perte max 50 % de la marge engagée, drawdown max 50 %).

Les paramètres de coûts du broker simulé (spread, prime de stop garanti,
financement overnight, change EUR/USD) sont des **hypothèses à calibrer**
avec ``GET /api/v1/markets/{epic}`` (champs ``overnightFee``, ``dealingRules``,
``snapshot``) dès qu'une connexion en lecture seule sera disponible.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum


class TradingMode(str, Enum):
    """Modes d'exécution. Il n'existe volontairement aucun mode « live »."""

    BACKTEST = "backtest"
    PAPER = "paper"


@dataclass(frozen=True)
class InstrumentSpec:
    epic: str = "US500"
    quote_currency: str = "USD"
    # Valeur d'un point pour 1 lot, en devise de cotation. À confirmer via
    # instrument.lotSize / contractSize de GET /api/v1/markets/US500.
    value_per_point_per_lot: float = 1.0
    min_size: float = 0.01
    size_increment: float = 0.01
    # Distance minimale du stop garanti, en points (dealingRules.minGuaranteedStopDistance).
    min_guaranteed_stop_distance: float = 0.0


@dataclass(frozen=True)
class BrokerCosts:
    """Coûts simulés. Toutes les valeurs sont des hypothèses à calibrer."""

    # Spread utilisé quand la donnée ne fournit pas bid/ask (en points).
    default_spread_points: float = 0.6
    # Prime du stop garanti, facturée seulement si le stop est déclenché (en points).
    guaranteed_stop_premium_points: float = 0.0
    # Taux de financement overnight journalier pour une position longue,
    # en fraction du notionnel (ex. 0.00023 = 0,023 % par jour). Signe positif = coût.
    overnight_long_rate_daily: float = 0.00023
    # Heure UTC du prélèvement (Capital.com : instrument.overnightFee.swapChargeTimestamp).
    funding_hour_utc: int = 22
    # Nombre de jours facturés le vendredi (week-end). À vérifier sur le relevé SWAP.
    friday_funding_multiplier: int = 3
    # Seuil de clôture forcée : marge disponible / marge utilisée (hypothèse 50 %).
    stop_out_margin_level: float = 0.5


@dataclass(frozen=True)
class RiskLimits:
    initial_capital_eur: float = 20.0
    max_leverage: float = 20.0
    max_total_exposure_lots: float = 1.0
    # Perte maximale par position : fraction de la marge engagée.
    max_loss_fraction_of_margin: float = 0.5
    # Risque maximal par trade : fraction de l'equity (perte au stop, prime incluse).
    # Ne peut que resserrer la règle de la marge, jamais l'élargir.
    max_risk_fraction_of_equity: float = 0.2
    # Drawdown maximal du compte, mesuré depuis le plus haut de l'equity.
    max_account_drawdown: float = 0.5
    allow_shorts: bool = False
    require_guaranteed_stop: bool = True
    max_spread_points: float = 2.0
    max_data_age_seconds: int = 900


@dataclass(frozen=True)
class Settings:
    mode: TradingMode = TradingMode.PAPER
    instrument: InstrumentSpec = field(default_factory=InstrumentSpec)
    costs: BrokerCosts = field(default_factory=BrokerCosts)
    risk: RiskLimits = field(default_factory=RiskLimits)
    account_currency: str = "EUR"
    # EUR/USD utilisé pour convertir les P&L en EUR quand aucun taux n'est fourni.
    default_eurusd: float = 1.16

    @classmethod
    def from_env(cls) -> "Settings":
        mode = os.getenv("BOT_MODE", TradingMode.PAPER.value).lower()
        if mode not in {m.value for m in TradingMode}:
            raise ValueError(
                f"BOT_MODE={mode!r} refusé. Modes autorisés : backtest, paper. "
                "Aucun mode d'exécution réelle n'existe dans ce dépôt."
            )
        risk = RiskLimits(
            initial_capital_eur=float(os.getenv("INITIAL_CAPITAL_EUR", "20")),
        )
        return cls(mode=TradingMode(mode), risk=risk)
