"""Calculs de contrat CFD : notionnel, marge, valeur du point, P&L.

Conventions :
- les prix sont en points d'indice, cotés en devise de l'instrument (USD pour US500) ;
- ``eurusd`` = nombre d'USD pour 1 EUR ; un montant USD se convertit en EUR par ``/ eurusd`` ;
- 1 lot vaut ``value_per_point_per_lot`` USD par point.

Ordre de grandeur (US500 ≈ 6 800, EUR/USD ≈ 1,0 à 1,2, levier 20) :
1 lot ≈ 6 800 USD de notionnel, soit ≈ 290 à 340 EUR de marge ; 0,01 lot ≈ 3 EUR.
"""

from __future__ import annotations

import math

from bot_trading.config import InstrumentSpec

_EPS = 1e-9


def usd_to_eur(amount_usd: float, eurusd: float) -> float:
    if eurusd <= 0:
        raise ValueError("eurusd doit être > 0")
    return amount_usd / eurusd


def notional_eur(size: float, price: float, spec: InstrumentSpec, eurusd: float) -> float:
    return usd_to_eur(size * spec.value_per_point_per_lot * price, eurusd)


def margin_eur(size: float, price: float, spec: InstrumentSpec, eurusd: float, leverage: float) -> float:
    if leverage <= 0:
        raise ValueError("levier doit être > 0")
    return notional_eur(size, price, spec, eurusd) / leverage


def value_per_point_eur(size: float, spec: InstrumentSpec, eurusd: float) -> float:
    return usd_to_eur(size * spec.value_per_point_per_lot, eurusd)


def pnl_eur(size: float, entry: float, exit_: float, long: bool, spec: InstrumentSpec, eurusd: float) -> float:
    points = (exit_ - entry) if long else (entry - exit_)
    return points * value_per_point_eur(size, spec, eurusd)


def is_valid_size(size: float, spec: InstrumentSpec) -> bool:
    if size + _EPS < spec.min_size:
        return False
    steps = size / spec.size_increment
    return abs(steps - round(steps)) < 1e-6


def floor_size(size: float, spec: InstrumentSpec) -> float:
    """Arrondit une taille vers le bas au pas autorisé (0 si sous le minimum)."""
    steps = math.floor(size / spec.size_increment + 1e-9)
    rounded = round(steps * spec.size_increment, 10)
    return rounded if rounded + _EPS >= spec.min_size else 0.0
