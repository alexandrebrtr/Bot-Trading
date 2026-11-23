"""Dimensionnement des positions (RISK_POLICY R8 à R11, R15, R17, R19).

Ordre (mission §11) : on fixe d'abord l'**invalidation** (stop), puis la taille
qui respecte le risque autorisé. Le stop n'est jamais éloigné ni rapproché pour
faire entrer une taille : si la taille minimale ne tient pas, c'est ``NO_TRADE``.

    taille = plancher( risque_EUR / ((entrée − stop + prime GSL) × valeur du point d'1 lot) )

bornée par l'exposition restante (R5) et, en option, par une marge maximale
(R19 : marge totale ≤ 50 % de l'equity). Le Risk Engine revérifie tout ensuite
(R8 50 % de la marge, R9 20 % de l'equity, drawdown) : ce module ne remplace
pas son veto.

Utilisé à la fois par le Decision Engine (estimation au moment de la décision)
et par le backtester (taille définitive au prix d'exécution).
"""

from __future__ import annotations

from dataclasses import dataclass

from bot_trading.config import BrokerCosts, InstrumentSpec, RiskLimits
from bot_trading.core import contract


@dataclass(frozen=True)
class SizingResult:
    ok: bool
    size: float
    risk_eur: float  # perte estimée au stop (prime incluse) pour la taille retenue
    risk_budget_eur: float  # risque autorisé avant arrondi
    risk_fraction: float  # fraction de l'equity effectivement visée
    loss_points: float
    margin_eur: float
    reason: str = ""  # code si ``ok`` est faux : STOP_WRONG_SIDE, INVALIDATION_TOO_FAR, SIZE_BELOW_MIN


def max_stop_distance(entry: float, limits: RiskLimits, costs: BrokerCosts) -> float:
    """R8 : distance de stop maximale (points), prime déduite. ≈ 5 % du prix à 10:1."""
    return limits.max_loss_fraction_of_margin * entry / limits.max_leverage - costs.guaranteed_stop_premium_points


def size_for_risk(
    *,
    equity_eur: float,
    entry: float,
    stop: float,
    risk_fraction: float,
    spec: InstrumentSpec,
    costs: BrokerCosts,
    limits: RiskLimits,
    eurusd: float,
    open_exposure_lots: float = 0.0,
    used_margin_eur: float = 0.0,
    max_margin_fraction: float | None = None,
    check_max_distance: bool = True,
) -> SizingResult:
    loss_pts = (entry - stop) + costs.guaranteed_stop_premium_points
    rf = max(0.0, min(risk_fraction, limits.max_risk_fraction_of_equity))
    budget = rf * max(equity_eur, 0.0)

    def fail(code: str) -> SizingResult:
        return SizingResult(False, 0.0, 0.0, budget, rf, loss_pts, 0.0, code)

    if entry - stop <= 0:
        return fail("STOP_WRONG_SIDE")
    if check_max_distance and entry - stop > max_stop_distance(entry, limits, costs) + 1e-9:
        return fail("INVALIDATION_TOO_FAR")  # R11 : jamais de stop rapproché sous l'invalidation
    vpp1 = contract.value_per_point_eur(1.0, spec, eurusd)
    size = contract.floor_size(budget / (loss_pts * vpp1), spec)
    room = contract.floor_size(limits.max_total_exposure_lots - open_exposure_lots, spec)
    size = min(size, room)
    # La marge disponible plafonne toujours la taille (sinon le Risk Engine oppose
    # INSUFFICIENT_MARGIN au lieu d'une taille réduite) ; max_margin_fraction resserre.
    margin_fraction = 1.0 if max_margin_fraction is None else min(max_margin_fraction, 1.0)
    per_lot = contract.margin_eur(1.0, entry, spec, eurusd, limits.max_leverage)
    allowed = margin_fraction * max(equity_eur, 0.0) - used_margin_eur
    size = min(size, contract.floor_size(max(allowed, 0.0) / per_lot, spec))
    if size < spec.min_size:
        return fail("SIZE_BELOW_MIN")
    return SizingResult(
        ok=True,
        size=size,
        risk_eur=loss_pts * vpp1 * size,
        risk_budget_eur=budget,
        risk_fraction=rf,
        loss_points=loss_pts,
        margin_eur=contract.margin_eur(size, entry, spec, eurusd, limits.max_leverage),
    )


@dataclass(frozen=True)
class DrawdownTiers:
    """R17 (🧪, paliers 🎛️) : réduction du risque avant l'arrêt à 50 %."""

    half_risk_at: float = 0.20
    max_quality_only_at: float = 0.35
    max_quality_threshold: float = 0.9

    def risk_multiplier(self, drawdown: float) -> float:
        return 0.5 if drawdown >= self.half_risk_at else 1.0

    def min_quality(self, drawdown: float) -> float | None:
        return self.max_quality_threshold if drawdown >= self.max_quality_only_at else None
