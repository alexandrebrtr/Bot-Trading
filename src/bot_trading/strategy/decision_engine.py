"""Decision Engine (mission §9, STRATEGY §2) : LONG, HOLD, CLOSE, NO_TRADE.

Pipeline top-down, chaque étage peut répondre ``NO_TRADE`` et dit pourquoi
(``meta["stage"]``) :

    1. fondamental  régime / biais / fenêtre d'annonce (dominant : décide si l'on peut acheter)
    2. D1           tendance principale (T1) et extension (T3)
    3. H4           structure : dernier BOS haussier (T2)
    4. setup        zone H1/H4 qualifiée et confirmée en M15/M5 (G1–G7, O1–O4, E1–E2)
    5. exécution    stop sous l'invalidation ≤ R8 (E4), spread (E5)
    6. obstacles    place jusqu'au premier obstacle ≥ min R (T4, E6)
    7. sizing       risque = base × multiplicateur du régime × palier de drawdown (R9, R15, R17, R19)
    8. risk_veto    pré-contrôle par ``RiskEngine.check_order`` : toute violation donne NO_TRADE

Le veto **définitif** reste celui du Risk Engine du broker au moment de
l'exécution : le pré-contrôle évite seulement de proposer un ordre déjà
refusable, il ne le remplace pas.

La technique ne peut jamais ouvrir une position sans un fondamental favorable,
et ne peut jamais transformer un biais baissier en achat (S2).
"""

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime

from bot_trading.config import Settings
from bot_trading.core.decision import Action, Decision
from bot_trading.core.models import Direction, OrderRequest, Position, Timeframe
from bot_trading.risk.engine import AccountState, MarketState, RiskEngine
from bot_trading.strategy.fundamental import RISK_OFF, FundamentalTracker, FundamentalView
from bot_trading.strategy.obstacles import ObstacleParams, check_obstacles
from bot_trading.strategy.positions import ExitParams, PositionManager
from bot_trading.strategy.sizing import DrawdownTiers, max_stop_distance, size_for_risk
from bot_trading.strategy.technical import TechnicalEngine, TechnicalParams, TechnicalSnapshot

TAKE_PROFIT_MODES = ("none", "fixed_r", "obstacle")

ENTRY_CONDITIONS = (
    "Régime fondamental RISK_ON (ou NEUTRE avec setup de qualité maximale), biais non baissier, hors fenêtre d'annonce",
    "D1 haussier (close > EMA, pente positive, structure non baissière) et extension ≤ k ATR",
    "H4 : dernier BOS haussier",
    "Zone H1/H4 (FVG ou OB haussier) touchée puis réaction mesurable en M15/M5",
    "Stop garanti sous l'invalidation à ≤ 2,5 % du prix (R8)",
    "Place jusqu'au premier obstacle ≥ min R, spread normal",
    "Taille ≥ 0,01 lot dans les limites du Risk Engine",
)


@dataclass(frozen=True)
class DecisionParams:
    base_risk_fraction: float = 0.10  # R9 🎛️ 2–20 %, avant multiplicateur du régime
    w_fundamental: float = 0.6  # poids du fondamental dans la confiance (> 0,5 : dominant)
    min_confidence: float = 0.0
    require_d1_uptrend: bool = True
    require_h4_bullish: bool = True
    max_spread_multiple: float = 2.0  # E5 s ∈ [1,5 ; 3]
    spread_window: int = 240
    take_profit: str = "obstacle"  # X1 : none | fixed_r | obstacle
    take_profit_r: float = 2.0
    tp_buffer_spreads: float = 1.0  # TP posé sous l'obstacle
    max_margin_fraction: float | None = 0.5  # R19
    holding_days: tuple[float, float] = (1.0, 10.0)
    tiers: DrawdownTiers = field(default_factory=DrawdownTiers)
    obstacles: ObstacleParams = field(default_factory=ObstacleParams)

    def __post_init__(self) -> None:
        if self.take_profit not in TAKE_PROFIT_MODES:
            raise ValueError(f"take_profit inconnu : {self.take_profit}")
        if not 0.0 < self.base_risk_fraction <= 0.2:
            raise ValueError("base_risk_fraction ∈ ]0 ; 0,2] (R9)")


@dataclass(frozen=True)
class AccountView:
    equity_eur: float
    peak_equity_eur: float
    used_margin_eur: float = 0.0
    open_exposure_lots: float = 0.0
    open_risk_to_stops_eur: float = 0.0

    @property
    def drawdown(self) -> float:
        return 1.0 - self.equity_eur / self.peak_equity_eur if self.peak_equity_eur > 0 else 0.0


class DecisionEngine:
    """Combine fondamental et technique en une ``Decision`` complète (mission §9)."""

    def __init__(self, settings: Settings | None = None, params: DecisionParams | None = None, eurusd: float | None = None) -> None:
        self.settings = settings or Settings()
        self.p = params or DecisionParams()
        self.eurusd = eurusd or self.settings.default_eurusd
        # Instance dédiée au pré-contrôle : mêmes limites, aucune possibilité de les modifier ici.
        self.risk = RiskEngine(self.settings.risk, self.settings.instrument, self.settings.costs)

    # ------------------------------------------------------------ helpers
    def _holding(self) -> str:
        lo, hi = self.p.holding_days
        return f"swing {lo:g} à {hi:g} jours (sortie temporelle, structure ou fondamental)"

    def _confidence(self, fund: FundamentalView, quality: float | None) -> float:
        w = self.p.w_fundamental
        return round(w * fund.confidence + (1 - w) * (quality or 0.0), 4)

    def _no_trade(self, stage: str, why: str, fund: FundamentalView, setup: str = "AUCUN", quality: float | None = None, **meta) -> Decision:
        return Decision(
            Action.NO_TRADE,
            reason=why,
            confidence=self._confidence(fund, quality),
            setup=setup,
            regime=fund.regime or "",
            bias=fund.bias,
            sources=fund.sources or ("aucune source fondamentale",),
            entry_conditions=ENTRY_CONDITIONS,
            invalidation_conditions=("Sans objet : aucune position ouverte",) + fund.invalidations,
            estimated_risk_eur=0.0,
            holding_period="aucune position",
            meta={"stage": stage, **meta},
        )

    # ------------------------------------------------------------ entrée
    def decide_entry(
        self,
        fund: FundamentalView,
        tech: TechnicalEngine,
        account: AccountView,
        bid: float,
        ask: float,
        now: datetime,
        median_spread: float | None = None,
        snapshot: TechnicalSnapshot | None = None,
    ) -> Decision:
        p = self.p
        # 1. Fondamental (dominant)
        if not fund.available:
            return self._no_trade("fondamental", "Fondamental indisponible : la technique seule ne peut pas ouvrir de position (S2)", fund, "NON_EVALUE")
        if fund.event_blackout:
            return self._no_trade("fondamental", "Fenêtre d'annonce macro (F11)", fund, "NON_EVALUE")
        if fund.regime == RISK_OFF or not fund.allow_new_entries:
            return self._no_trade("fondamental", f"Pas de nouvelle entrée : {fund.describe()} (F10)", fund, "NON_EVALUE")
        if fund.bias == "bearish":
            return self._no_trade("fondamental", f"Biais fondamental baissier : {fund.describe()}", fund, "NON_EVALUE")
        if fund.size_multiplier <= 0:
            return self._no_trade("fondamental", "Multiplicateur de taille nul pour ce régime", fund, "NON_EVALUE")

        snap = snapshot or tech.snapshot()
        tnotes = list(snap.notes)
        # 2. D1
        if p.require_d1_uptrend and snap.d1_trend != "bullish":
            return self._no_trade("D1", f"Tendance D1 {snap.d1_trend} (T1)", fund, notes=tnotes)
        mx = tech.p.max_extension_atr
        if snap.extension_atr is not None and snap.extension_atr > mx:
            return self._no_trade("D1", f"Entrée tardive : extension {snap.extension_atr:.2f} ATR D1 > {mx:g} (T3)", fund, notes=tnotes)
        if tech.p.max_vol_percentile is not None and snap.vol_percentile is not None and snap.vol_percentile > tech.p.max_vol_percentile:
            return self._no_trade("D1", f"Volatilité D1 au percentile {snap.vol_percentile:.0%}", fund, notes=tnotes)
        # 3. H4
        if p.require_h4_bullish and snap.h4_structure != "bullish":
            return self._no_trade("H4", f"Structure H4 {snap.h4_structure} : pas de BOS haussier récent (T2)", fund, notes=tnotes)
        # 4. Setup + confirmation
        min_q = max(fund.min_setup_quality, p.tiers.min_quality(account.drawdown) or 0.0)
        confirmed = [s for s in snap.confirmed_setups if s.quality >= min_q]
        if not confirmed:
            if snap.confirmed_setups:
                b = max(snap.confirmed_setups, key=lambda s: s.quality)
                return self._no_trade("setup", f"Setup {b.name} confirmé mais qualité {b.quality:.2f} < {min_q:.2f} exigée ({fund.regime})", fund, b.name, b.quality)
            if snap.setups:
                s = max(snap.setups, key=lambda s: s.quality)
                return self._no_trade("confirmation", f"Zone {s.name} {s.bottom:.1f}-{s.top:.1f} en jeu, pas de confirmation : {s.confirmation}", fund, s.name, s.quality)
            return self._no_trade("setup", "Aucune zone H1/H4 haussière en jeu", fund)
        best = max(confirmed, key=lambda s: (s.quality, s.stop))
        # 5. Exécution : stop, spread
        spread = ask - bid
        entry = ask
        stop = best.stop
        min_dist = self.settings.instrument.min_guaranteed_stop_distance
        if entry - stop < min_dist:
            stop = entry - min_dist  # R10 : stop porté à la distance minimale, risque recalculé
        max_d = max_stop_distance(entry, self.settings.risk, self.settings.costs)
        if entry - stop > max_d:
            return self._no_trade("execution", f"Invalidation à {entry - stop:.1f} pts > {max_d:.1f} pts autorisés (R8, R11)", fund, best.name, best.quality)
        if entry - stop <= 0:
            return self._no_trade("execution", "Prix déjà sous l'invalidation", fund, best.name, best.quality)
        if median_spread and spread > p.max_spread_multiple * median_spread:
            return self._no_trade("execution", f"Spread {spread:.2f} > {p.max_spread_multiple:g} × médiane {median_spread:.2f} (E5)", fund, best.name, best.quality)
        # 6. Obstacles
        st = tech.state(best.timeframe)
        obs = check_obstacles(tech, entry, stop, spread, st.atr_value if st else None, p.obstacles)
        if not obs.passed:
            return self._no_trade("obstacles", f"Potentiel insuffisant : {obs.describe()}", fund, best.name, best.quality)
        tp = None
        risk_pts = entry - stop
        if p.take_profit == "fixed_r":
            tp = entry + p.take_profit_r * risk_pts
        elif p.take_profit == "obstacle" and obs.first is not None:
            tp = obs.first.bottom - p.tp_buffer_spreads * spread
            if tp <= entry:
                tp = None
        # 7. Sizing
        rf = p.base_risk_fraction * fund.size_multiplier * p.tiers.risk_multiplier(account.drawdown)
        sized = size_for_risk(
            equity_eur=account.equity_eur,
            entry=entry,
            stop=stop,
            risk_fraction=rf,
            spec=self.settings.instrument,
            costs=self.settings.costs,
            limits=self.settings.risk,
            eurusd=self.eurusd,
            open_exposure_lots=account.open_exposure_lots,
            used_margin_eur=account.used_margin_eur,
            max_margin_fraction=p.max_margin_fraction,
        )
        if not sized.ok:
            return self._no_trade("sizing", f"Dimensionnement impossible ({sized.reason}) : risque autorisé {sized.risk_budget_eur:.2f} € pour {sized.loss_points:.1f} pts", fund, best.name, best.quality)
        # 8. Pré-contrôle du Risk Engine (veto)
        order = OrderRequest(self.settings.instrument.epic, Direction.BUY, sized.size, stop, True, tp, reason=best.name)
        acct = AccountState(account.equity_eur, account.peak_equity_eur, account.used_margin_eur, account.open_exposure_lots, account.open_risk_to_stops_eur)
        mkt = MarketState(bid=bid, ask=ask, quote_time=now, now=now, eurusd=self.eurusd, market_open=True, event_blackout=fund.event_blackout)
        verdict = self.risk.check_order(order, acct, mkt)
        if not verdict.approved:
            return self._no_trade("risk_veto", f"Veto du Risk Engine : {', '.join(v.value for v in verdict.violations)}", fund, best.name, best.quality, risk=verdict.details)

        conf = self._confidence(fund, best.quality)
        if conf < p.min_confidence:
            return self._no_trade("confiance", f"Confiance {conf:.2f} < {p.min_confidence:.2f}", fund, best.name, best.quality)
        why = (
            f"{fund.describe()}. D1 {snap.d1_trend} (extension {snap.extension_atr if snap.extension_atr is not None else float('nan'):.2f} ATR), "
            f"H4 {snap.h4_structure}. Setup {best.name} {best.bottom:.1f}-{best.top:.1f} qualité {best.quality:.2f}, {best.confirmation}. "
            f"{obs.describe()}."
        )
        return Decision(
            Action.LONG,
            stop_level=round(stop, 2),
            take_profit_level=round(tp, 2) if tp is not None else None,
            risk_fraction=sized.risk_fraction,
            reason=why,
            confidence=conf,
            setup=best.name,
            regime=fund.regime or "",
            bias=fund.bias,
            sources=fund.sources + (f"technique:{best.name}@{now.isoformat()}",),
            entry_conditions=(
                f"Achat au marché à l'ouverture suivante (ask ≈ {entry:.1f})",
                f"Zone {best.name} {best.bottom:.1f}-{best.top:.1f} confirmée : {best.confirmation}",
                f"Spread {spread:.2f}" + (f" (médiane {median_spread:.2f})" if median_spread else ""),
            ),
            invalidation_conditions=(
                f"Stop garanti {stop:.1f} (bas de zone − buffer ATR), {risk_pts:.1f} pts",
                "Biais fondamental baissier ou régime RISK_OFF",
                "BOS baissier H4 après l'entrée",
            ) + fund.invalidations,
            estimated_risk_eur=round(sized.risk_eur, 4),
            holding_period=self._holding(),
            meta={
                "stage": "LONG",
                "size_estimate": sized.size,
                "margin_estimate_eur": round(sized.margin_eur, 4),
                "quality": best.quality,
                "quality_components": best.components,
                "room_r": obs.room_r,
                "first_obstacle": obs.first.name if obs.first else None,
                "risk_multiplier_regime": fund.size_multiplier,
                "drawdown": round(account.drawdown, 4),
            },
        )


class DecisionStrategy:
    """Stratégie complète branchée sur le backtester (``Strategy.on_bar``).

    Assemble : vue fondamentale (point-in-time), Technical Engine, Decision
    Engine, PositionManager. Toutes les données passent par ``BarContext``.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        technical: TechnicalParams | None = None,
        decision: DecisionParams | None = None,
        exits: ExitParams | None = None,
        base: Timeframe | None = None,
        eurusd: float | None = None,
        fundamental_provider=None,
        name: str = "decision_engine",
    ) -> None:
        self.settings = settings or Settings()
        self.name = name
        self.technical_params = technical or TechnicalParams()
        self.engine = DecisionEngine(self.settings, decision, eurusd)
        self.manager = PositionManager(self.settings, exits or ExitParams(), eurusd)
        self.tech: TechnicalEngine | None = None
        self.base = base
        self.fundamental_provider = fundamental_provider  # callable(ctx) -> FundamentalView
        self.tracker = FundamentalTracker()
        self.peak = 0.0
        self.spreads: deque[float] = deque(maxlen=self.engine.p.spread_window)
        self._pending_setup = ""

    def restore_position(self, position: Position, initial_stop: float | None = None, setup: str = "") -> None:
        """Reprise du paper trading après redémarrage.

        Mémorise le stop d'ouverture (référence du R, le stop courant a pu
        monter) et le setup d'une position rechargée. Un stop déjà au-dessus du
        prix d'entrée signifie que le break-even a été fait : le stop suiveur
        reprend donc là où il en était.
        """
        opening = replace(position, stop_level=initial_stop) if initial_stop is not None else position
        track = self.manager.register(opening, setup)
        if position.stop_level >= position.entry_price:
            track.breakeven_done = True
        if setup:
            self._pending_setup = setup

    def _fund(self, ctx) -> FundamentalView:
        if self.fundamental_provider is not None:
            return self.fundamental_provider(ctx)
        return FundamentalView.from_pit(ctx.pit)

    def on_bar(self, ctx) -> Decision:
        if self.tech is None:
            self.tech = TechnicalEngine(self.technical_params, self.base or ctx.bar.timeframe)
        self.tech.update(ctx)
        spread = self.settings.costs.default_spread_points if ctx.bar.spread is None else ctx.bar.spread
        self.spreads.append(spread)
        self.peak = max(self.peak, ctx.equity_eur, self.settings.risk.initial_capital_eur)
        fund = self._fund(ctx)
        self.tracker.update(fund)
        bid = ctx.candle.close
        if ctx.positions:
            self.manager.forget({p.deal_id for p in ctx.positions})
            pos: Position = ctx.positions[0]
            self.manager.register(pos, self._pending_setup)
            return self.manager.manage(pos, bid, ctx.now, ctx.equity_eur, fund, self.tech)
        median = statistics.median(self.spreads) if len(self.spreads) >= 20 else None
        account = AccountView(ctx.equity_eur, self.peak)
        d = self.engine.decide_entry(fund, self.tech, account, bid, bid + spread, ctx.now, median)
        if d.action == Action.LONG:
            self._pending_setup = d.setup
        return d

