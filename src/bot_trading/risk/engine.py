"""Risk Engine déterministe, indépendant de la stratégie et des agents.

Principes :
- aucune dépendance vers la stratégie, les modèles ou les agents : il ne reçoit
  que des faits (ordre proposé, état du compte, état du marché) ;
- aucune aléa, aucune I/O : même entrée, même décision ;
- droit de veto absolu : un ordre n'atteint le broker que si ``approved`` est vrai ;
- toutes les violations sont listées (pas seulement la première), pour l'audit.

Règle de perte maximale par position
------------------------------------
La perte au stop garanti (écart ask d'entrée → stop, plus la prime éventuelle)
ne doit pas dépasser ``max_loss_fraction_of_margin`` (50 %) de la marge engagée.
Comme perte et marge sont toutes deux proportionnelles à la taille, cette règle
revient à une distance de stop maximale indépendante de la taille :

    distance_max = 0,5 × prix / levier   (≈ 2,5 % du prix avec un levier de 20)

Règle de risque maximal par trade
---------------------------------
La même perte au stop ne doit pas non plus dépasser ``max_risk_fraction_of_equity``
(20 %) de l'equity au moment de l'ordre. La perte autorisée est donc :

    perte_max = min(50 % de la marge engagée, 20 % de l'equity)

La règle des 50 % reste le plafond absolu : la règle des 20 % ne peut que la
resserrer. Avec 20 € et 0,01 lot (marge ≈ 3 €), c'est la règle des 50 % qui
s'applique ; la règle des 20 % devient active dès que la taille augmente
(marge > 40 % de l'equity, soit à partir d'environ 0,03 lot).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from bot_trading.config import BrokerCosts, InstrumentSpec, RiskLimits
from bot_trading.core import contract
from bot_trading.core.models import Direction, OrderRequest

_EPS = 1e-9


class Violation(str, Enum):
    KILL_SWITCH = "KILL_SWITCH"
    DRAWDOWN_HALT = "DRAWDOWN_HALT"
    DRAWDOWN_WOULD_BREACH = "DRAWDOWN_WOULD_BREACH"
    SHORT_DISABLED = "SHORT_DISABLED"
    WRONG_EPIC = "WRONG_EPIC"
    NO_STOP = "NO_STOP"
    STOP_NOT_GUARANTEED = "STOP_NOT_GUARANTEED"
    STOP_WRONG_SIDE = "STOP_WRONG_SIDE"
    STOP_TOO_CLOSE = "STOP_TOO_CLOSE"
    MAX_LOSS_EXCEEDED = "MAX_LOSS_EXCEEDED"
    MAX_RISK_PER_TRADE_EXCEEDED = "MAX_RISK_PER_TRADE_EXCEEDED"
    TAKE_PROFIT_WRONG_SIDE = "TAKE_PROFIT_WRONG_SIDE"
    INVALID_SIZE = "INVALID_SIZE"
    EXPOSURE_EXCEEDED = "EXPOSURE_EXCEEDED"
    INSUFFICIENT_MARGIN = "INSUFFICIENT_MARGIN"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    STALE_DATA = "STALE_DATA"
    EVENT_BLACKOUT = "EVENT_BLACKOUT"
    MARKET_CLOSED = "MARKET_CLOSED"


@dataclass(frozen=True)
class AccountState:
    equity_eur: float
    peak_equity_eur: float
    used_margin_eur: float
    open_exposure_lots: float
    # Perte totale si tous les stops des positions ouvertes sont touchés (EUR, >= 0).
    open_risk_to_stops_eur: float = 0.0

    @property
    def available_margin_eur(self) -> float:
        return self.equity_eur - self.used_margin_eur


@dataclass(frozen=True)
class MarketState:
    bid: float
    ask: float
    quote_time: datetime
    now: datetime
    eurusd: float
    market_open: bool = True
    event_blackout: bool = False

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    violations: tuple[Violation, ...]
    details: dict = field(default_factory=dict)

    def explain(self) -> str:
        if self.approved:
            return "APPROUVÉ"
        return "VETO: " + ", ".join(v.value for v in self.violations)


class RiskEngine:
    def __init__(self, limits: RiskLimits, spec: InstrumentSpec, costs: BrokerCosts) -> None:
        self.limits = limits
        self.spec = spec
        self.costs = costs
        self._kill_switch = False
        self._halted = False

    # ------------------------------------------------------------------ état
    @property
    def halted(self) -> bool:
        """Vrai si le drawdown max a été atteint. Verrou : ne se lève pas seul."""
        return self._halted

    @property
    def kill_switch(self) -> bool:
        return self._kill_switch

    def engage_kill_switch(self) -> None:
        self._kill_switch = True

    def reset_halt(self, confirmation: str) -> None:
        """Lever le verrou demande une confirmation humaine explicite."""
        if confirmation != "JE_CONFIRME_LA_REPRISE":
            raise PermissionError("Reprise refusée : confirmation humaine absente")
        self._halted = False
        self._kill_switch = False

    # ------------------------------------------------------------- calculs
    def drawdown_floor_eur(self, peak_equity_eur: float) -> float:
        peak = max(peak_equity_eur, self.limits.initial_capital_eur)
        return peak * (1.0 - self.limits.max_account_drawdown)

    def max_loss_allowed_eur(self, margin_eur: float, equity_eur: float) -> float:
        """Perte maximale autorisée au stop : min(50 % de la marge, 20 % de l'equity)."""
        return min(
            self.limits.max_loss_fraction_of_margin * margin_eur,
            self.limits.max_risk_fraction_of_equity * max(equity_eur, 0.0),
        )

    def max_stop_distance(
        self,
        entry_price: float,
        size: float | None = None,
        equity_eur: float | None = None,
        eurusd: float = 1.0,
    ) -> float:
        """Distance de stop maximale (points), prime du stop garanti déduite.

        Sans ``size`` ni ``equity_eur`` : seule la règle des 50 % de la marge
        (indépendante de la taille). Avec les deux : la règle combinée.
        """
        by_margin = self.limits.max_loss_fraction_of_margin * entry_price / self.limits.max_leverage
        if size is not None and equity_eur is not None:
            vpp = contract.value_per_point_eur(size, self.spec, eurusd)
            by_equity = self.limits.max_risk_fraction_of_equity * max(equity_eur, 0.0) / vpp
            by_margin = min(by_margin, by_equity)
        return by_margin - self.costs.guaranteed_stop_premium_points

    def evaluate_account(self, account: AccountState) -> RiskDecision:
        """Surveillance continue. Déclenche le verrou si le drawdown max est atteint."""
        floor = self.drawdown_floor_eur(account.peak_equity_eur)
        if account.equity_eur <= floor + _EPS:
            self._halted = True
        violations: list[Violation] = []
        if self._halted:
            violations.append(Violation.DRAWDOWN_HALT)
        if self._kill_switch:
            violations.append(Violation.KILL_SWITCH)
        return RiskDecision(
            approved=not violations,
            violations=tuple(violations),
            details={"equity_eur": account.equity_eur, "drawdown_floor_eur": floor},
        )

    # ------------------------------------------------------------- pré-trade
    def check_order(self, order: OrderRequest, account: AccountState, market: MarketState) -> RiskDecision:
        v: list[Violation] = []
        d: dict = {}

        account_check = self.evaluate_account(account)
        v.extend(account_check.violations)

        if order.epic != self.spec.epic:
            v.append(Violation.WRONG_EPIC)

        long = order.direction == Direction.BUY
        if not long and not self.limits.allow_shorts:
            v.append(Violation.SHORT_DISABLED)

        if not market.market_open:
            v.append(Violation.MARKET_CLOSED)
        if market.event_blackout:
            v.append(Violation.EVENT_BLACKOUT)
        age = (market.now - market.quote_time).total_seconds()
        d["data_age_s"] = age
        if age > self.limits.max_data_age_seconds or age < 0:
            v.append(Violation.STALE_DATA)
        d["spread"] = market.spread
        if market.spread > self.limits.max_spread_points + _EPS:
            v.append(Violation.SPREAD_TOO_WIDE)

        if not contract.is_valid_size(order.size, self.spec):
            v.append(Violation.INVALID_SIZE)
        new_exposure = account.open_exposure_lots + order.size
        d["exposure_after_lots"] = new_exposure
        if new_exposure > self.limits.max_total_exposure_lots + _EPS:
            v.append(Violation.EXPOSURE_EXCEEDED)

        entry = market.ask if long else market.bid
        margin = contract.margin_eur(order.size, entry, self.spec, market.eurusd, self.limits.max_leverage)
        d["margin_eur"] = margin
        if margin > account.available_margin_eur + _EPS:
            v.append(Violation.INSUFFICIENT_MARGIN)

        if self.limits.require_guaranteed_stop and not order.guaranteed_stop:
            v.append(Violation.STOP_NOT_GUARANTEED)

        if order.stop_level is None:
            v.append(Violation.NO_STOP)
        else:
            distance = (entry - order.stop_level) if long else (order.stop_level - entry)
            d["stop_distance"] = distance
            if distance <= 0:
                v.append(Violation.STOP_WRONG_SIDE)
            else:
                if order.guaranteed_stop and distance + _EPS < self.spec.min_guaranteed_stop_distance:
                    v.append(Violation.STOP_TOO_CLOSE)
                loss_points = distance + (self.costs.guaranteed_stop_premium_points if order.guaranteed_stop else 0.0)
                max_loss = contract.value_per_point_eur(order.size, self.spec, market.eurusd) * loss_points
                d["max_loss_eur"] = max_loss
                by_margin = self.limits.max_loss_fraction_of_margin * margin
                by_equity = self.limits.max_risk_fraction_of_equity * max(account.equity_eur, 0.0)
                d["max_loss_allowed_eur"] = min(by_margin, by_equity)
                d["max_loss_by_margin_eur"] = by_margin
                d["max_loss_by_equity_eur"] = by_equity
                if max_loss > by_margin + _EPS:
                    v.append(Violation.MAX_LOSS_EXCEEDED)
                if max_loss > by_equity + _EPS:
                    v.append(Violation.MAX_RISK_PER_TRADE_EXCEEDED)
                floor = self.drawdown_floor_eur(account.peak_equity_eur)
                if account.equity_eur - account.open_risk_to_stops_eur - max_loss < floor - _EPS:
                    v.append(Violation.DRAWDOWN_WOULD_BREACH)

        if order.take_profit_level is not None:
            ok = order.take_profit_level > entry if long else order.take_profit_level < entry
            if not ok:
                v.append(Violation.TAKE_PROFIT_WRONG_SIDE)

        # dédoublonnage en conservant l'ordre
        seen: list[Violation] = []
        for item in v:
            if item not in seen:
                seen.append(item)
        return RiskDecision(approved=not seen, violations=tuple(seen), details=d)
