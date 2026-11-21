"""Gestion des positions swing (mission §12, STRATEGY X1 à X4, RISK_POLICY R13, R22).

Méthodes de sortie disponibles, chacune activable séparément pour être
comparée par backtest (EXP-07) :

| Méthode | Paramètre | Action |
|---|---|---|
| Invalidation fondamentale | ``close_on_bias`` | CLOSE si le biais devient baissier |
| Changement de régime | ``on_risk_off`` | ``tighten`` (Q2 par défaut : stop au break-even ou sous le dernier plus bas H1), ``close`` ou ``ignore`` |
| Rupture de structure | ``structure_exit_tf`` | CLOSE sur BOS baissier H1 ou H4 postérieur à l'entrée |
| Risque devenu excessif | ``max_open_risk_fraction`` | CLOSE si la perte au stop dépasse cette fraction de l'equity |
| Sortie temporelle (X3) | ``max_days`` / ``max_days_min_r`` | CLOSE si +b R non atteint après N jours |
| Coût du financement (R22) | ``max_funding_fraction_of_risk`` | CLOSE si le financement cumulé dépasse cette part du risque initial |
| Take profit fixe / dynamique | Decision Engine (``take_profit``) | posé à l'entrée : k R ou premier obstacle |
| Remontée du stop garanti | ``breakeven_at_r``, ``trail_tf`` | HOLD avec nouveau stop, uniquement vers le haut |

Garanties :
- le stop n'est **jamais** retiré ni éloigné : un nouveau niveau n'est proposé
  que s'il est strictement plus haut que le stop courant (R13, X4) ;
- le nouveau stop reste sous le bid moins la distance minimale du stop garanti ;
  sinon aucune modification (le broker refuserait, et la position garde son
  stop actuel) ;
- aucune modification ne passe par une suppression temporaire du stop : il n'y a
  pas d'opération « retirer » dans ce module ;
- en cas d'indisponibilité de l'analyse (fondamental absent), la position garde
  son stop garanti et n'est pas modifiée par défaut.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from bot_trading.config import Settings
from bot_trading.core import contract
from bot_trading.core.decision import Action, Decision
from bot_trading.core.models import Position, Timeframe
from bot_trading.strategy.fundamental import RISK_OFF, FundamentalView
from bot_trading.strategy.technical import TechnicalEngine

ON_RISK_OFF = ("tighten", "close", "ignore")


@dataclass(frozen=True)
class ExitParams:
    breakeven_at_r: float | None = 1.0  # X2 a ∈ [0,5 ; 2]
    breakeven_offset_r: float = 0.05  # stop à entrée + offset × R (couvre la prime éventuelle)
    trail_tf: Timeframe | None = Timeframe.H1  # stop suiveur sous les swing lows confirmés après le BE
    trail_buffer_atr: float = 0.2
    close_on_bias: tuple[str, ...] = ("bearish",)
    on_risk_off: str = "tighten"
    structure_exit_tf: Timeframe | None = Timeframe.H4
    max_days: float | None = 10.0  # X3 N ∈ [2 ; 15]
    max_days_min_r: float = 0.5  # X3 b
    max_funding_fraction_of_risk: float | None = 0.25  # R22
    max_open_risk_fraction: float | None = None

    def __post_init__(self) -> None:
        if self.on_risk_off not in ON_RISK_OFF:
            raise ValueError(f"on_risk_off inconnu : {self.on_risk_off}")


@dataclass
class _Track:
    initial_stop: float
    breakeven_done: bool = False
    setup: str = ""


@dataclass
class PositionManager:
    settings: Settings
    params: ExitParams = field(default_factory=ExitParams)
    eurusd: float | None = None
    _tracks: dict[str, _Track] = field(default_factory=dict)

    def register(self, position: Position, setup: str = "") -> _Track:
        """Mémorise le stop initial (le premier stop vu est le stop d'ouverture : il ne peut que monter)."""
        t = self._tracks.get(position.deal_id)
        if t is None:
            t = _Track(initial_stop=position.stop_level, setup=setup or str(position.meta.get("reason", "")))
            self._tracks[position.deal_id] = t
        return t

    def manage(self, position: Position, bid: float, now: datetime, equity_eur: float, fund: FundamentalView, tech: TechnicalEngine) -> Decision:
        p = self.params
        t = self.register(position)
        entry = position.entry_price
        r_pts = entry - t.initial_stop
        profit_r = (bid - entry) / r_pts if r_pts > 0 else 0.0
        eurusd = self.eurusd or self.settings.default_eurusd
        vpp = contract.value_per_point_eur(position.size, self.settings.instrument, eurusd)
        initial_risk = (r_pts + self.settings.costs.guaranteed_stop_premium_points) * vpp
        held_days = (now - position.opened_at).total_seconds() / 86400
        common = dict(
            bias=fund.bias,
            regime=fund.regime or "",
            setup=t.setup or "POSITION_OUVERTE",
            sources=fund.sources or ("aucune source fondamentale : stop garanti conservé",),
            entry_conditions=(f"Position ouverte à {entry:.1f} le {position.opened_at:%Y-%m-%d %H:%M} UTC, {position.size:g} lot",),
            estimated_risk_eur=round(max(bid - position.stop_level, 0.0) * vpp, 4),
            invalidation_conditions=(f"Stop garanti {position.stop_level:.1f}",) + fund.invalidations,
            holding_period=f"swing, détenue depuis {held_days:.1f} j",
            confidence=fund.confidence,
        )

        def close(why: str) -> Decision:
            return Decision(Action.CLOSE, reason=why, **common)

        # 1. Invalidation fondamentale (le fondamental domine).
        if fund.available and fund.bias in p.close_on_bias:
            return close(f"Invalidation fondamentale : biais {fund.bias} ({fund.describe()})")
        if fund.available and fund.regime == RISK_OFF and p.on_risk_off == "close":
            return close("Changement de régime : RISK_OFF")
        # 2. Rupture de structure H1/H4 postérieure à l'entrée.
        if p.structure_exit_tf is not None:
            st = tech.state(p.structure_exit_tf)
            lb = st.last_bos() if st is not None else None
            if lb is not None and not lb[0] and lb[1] >= position.opened_at:
                return close(f"Rupture de structure {p.structure_exit_tf.value} (BOS baissier depuis l'entrée)")
        # 3. Risque devenu excessif.
        if p.max_open_risk_fraction is not None and equity_eur > 0:
            open_risk = max(bid - position.stop_level, 0.0) * vpp
            if open_risk > p.max_open_risk_fraction * equity_eur:
                return close(f"Risque au stop {open_risk:.2f} € > {p.max_open_risk_fraction:.0%} de l'equity")
        # 4. Sortie temporelle (X3) et coût du financement (R22).
        if p.max_days is not None and held_days >= p.max_days and profit_r < p.max_days_min_r:
            return close(f"Sortie temporelle : {held_days:.1f} j sans atteindre +{p.max_days_min_r:g} R ({profit_r:+.2f} R)")
        if p.max_funding_fraction_of_risk is not None and initial_risk > 0 and position.funding_paid_eur > p.max_funding_fraction_of_risk * initial_risk:
            return close(f"Financement {position.funding_paid_eur:.2f} € > {p.max_funding_fraction_of_risk:.0%} du risque initial")

        # 5. Remontée du stop garanti (jamais vers le bas, jamais retiré).
        candidates: list[tuple[float, str]] = []
        be_level = entry + p.breakeven_offset_r * r_pts
        if p.breakeven_at_r is not None and profit_r >= p.breakeven_at_r:
            candidates.append((be_level, f"break-even après +{p.breakeven_at_r:g} R"))
            t.breakeven_done = True
        if fund.available and fund.regime == RISK_OFF and p.on_risk_off == "tighten":
            candidates.append((be_level, "RISK_OFF : stop au break-even"))
            h1 = tech.state(Timeframe.H1) or tech.state(tech.base)
            low = h1.last_swing_low() if h1 else None
            a = h1.atr_value if h1 else None
            if low is not None:
                candidates.append((low - (p.trail_buffer_atr * a if a else 0.0), "RISK_OFF : stop sous le dernier plus bas H1"))
        if t.breakeven_done and p.trail_tf is not None:
            st = tech.state(p.trail_tf) or tech.state(tech.base)
            if st is not None:
                lows = st.swing_lows_since(position.opened_at)
                a = st.atr_value
                if lows:
                    candidates.append((lows[-1] - (p.trail_buffer_atr * a if a else 0.0), f"stop suiveur sous le swing low {st.tf.value}"))
        min_dist = self.settings.instrument.min_guaranteed_stop_distance
        # Arrondi vers le bas (au centième de point) avant contrôle : jamais au-dessus du niveau calculé.
        rounded = [(math.floor(lvl * 100) / 100, why) for lvl, why in candidates]
        feasible = [(lvl, why) for lvl, why in rounded if position.stop_level < lvl < bid - max(min_dist, 0.0) - 1e-9]
        if feasible:
            lvl, why = max(feasible)
            d = Decision(Action.HOLD, stop_level=lvl, reason=f"Remontée du stop garanti : {why} ({profit_r:+.2f} R)", **common)
            return d
        return Decision(Action.HOLD, reason=f"Position conservée ({profit_r:+.2f} R), stop garanti {position.stop_level:.1f} inchangé", **common)

    def forget(self, open_ids: set[str]) -> None:
        for k in [k for k in self._tracks if k not in open_ids]:
            del self._tracks[k]
