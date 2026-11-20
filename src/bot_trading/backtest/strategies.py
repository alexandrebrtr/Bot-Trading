"""Stratégies de référence (benchmarks, mission section 17) et filtre fondamental.

Toutes respectent les mêmes contraintes que la stratégie cible : long only,
stop garanti, sizing par fraction de risque, mêmes coûts, même Risk Engine.
Elles servent de points de comparaison, pas de stratégies recommandées.

| Benchmark (mission)                       | Classe                                   |
|-------------------------------------------|------------------------------------------|
| Buy and Hold S&P 500                      | ``BuyAndHold``                           |
| Trend Following simple                    | ``SimpleTrendFollowing``                 |
| Stratégie technique sans fondamentaux     | ``FVGTechnical``                         |
| Stratégie fondamentale sans filtre technique | ``FundamentalOnly``                   |
| Stratégie complète                        | ``FundamentalFilter(FVGTechnical(...))`` |
| Entrées aléatoires, mêmes stops (EXPERIMENTS) | ``RandomEntry``                      |

Le biais fondamental est lu **uniquement** par ``ctx.pit`` (séries point-in-time),
jamais par un tableau précalculé indexé sur la bougie.
"""

from __future__ import annotations

import random
from collections import deque
from dataclasses import dataclass

from bot_trading.analysis.fvg import FVGEngine, FVGParams
from bot_trading.analysis.indicators import IncrementalATR
from bot_trading.backtest.engine import BarContext
from bot_trading.config import Settings
from bot_trading.core.decision import Action, Decision


def max_stop_distance(price: float, settings: Settings) -> float:
    """Distance de stop maximale imposée par R8 (50 % de la marge), prime déduite."""
    r = settings.risk
    return r.max_loss_fraction_of_margin * price / r.max_leverage - settings.costs.guaranteed_stop_premium_points


def _entry_price(ctx: BarContext, settings: Settings) -> float:
    spread = settings.costs.default_spread_points if ctx.bar.spread is None else ctx.bar.spread
    return ctx.candle.close + spread


class _Base:
    name = "base"

    def __init__(self, settings: Settings | None = None, risk_fraction: float | None = None, atr_period: int = 14) -> None:
        self.settings = settings or Settings()
        self.risk_fraction = risk_fraction
        self.atr = IncrementalATR(atr_period)

    def _atr(self, ctx: BarContext) -> float | None:
        return self.atr.update(ctx.candle)

    def _long(self, ctx: BarContext, stop: float, **kw) -> Decision:
        """LONG si l'invalidation reste dans la distance autorisée, sinon NO_TRADE (R11)."""
        entry = _entry_price(ctx, self.settings)
        if entry - stop > max_stop_distance(ctx.candle.close, self.settings):
            return Decision.no_trade("Invalidation trop lointaine pour R8 (R11)", **{k: v for k, v in kw.items() if k in ("setup", "regime", "bias")})
        return Decision(Action.LONG, stop_level=stop, risk_fraction=self.risk_fraction, **kw)


class BuyAndHold(_Base):
    """Toujours investi, stop garanti à la distance maximale autorisée ; réentre après un stop."""

    name = "buy_and_hold"

    def __init__(self, settings: Settings | None = None, risk_fraction: float | None = None, stop_fraction_of_max: float = 0.95) -> None:
        super().__init__(settings, risk_fraction)
        self.k = stop_fraction_of_max

    def on_bar(self, ctx: BarContext) -> Decision:
        if ctx.in_position:
            return Decision.hold(bias="bullish")
        entry = _entry_price(ctx, self.settings)
        stop = entry - self.k * max_stop_distance(ctx.candle.close, self.settings)
        return Decision(Action.LONG, stop_level=stop, risk_fraction=self.risk_fraction, setup="BUY_AND_HOLD", bias="bullish")


class SimpleTrendFollowing(_Base):
    """Long si clôture > SMA(n) et SMA montante ; sortie si clôture < SMA(n). Stop k × ATR."""

    name = "trend_following"

    def __init__(self, settings: Settings | None = None, risk_fraction: float | None = None, sma_period: int = 200, atr_stop: float = 3.0, slope_lookback: int = 10) -> None:
        super().__init__(settings, risk_fraction)
        self.n = sma_period
        self.k = atr_stop
        self.lb = slope_lookback
        self._win: deque[float] = deque(maxlen=sma_period)
        self._sum = 0.0
        self._smas: deque[float] = deque(maxlen=slope_lookback + 1)

    def on_bar(self, ctx: BarContext) -> Decision:
        a = self._atr(ctx)
        c = ctx.candle.close
        if len(self._win) == self.n:
            self._sum -= self._win[0]
        self._win.append(c)
        self._sum += c
        if len(self._win) < self.n or a is None:
            return Decision.no_trade("Préchauffage")
        sma = self._sum / self.n
        self._smas.append(sma)
        up = len(self._smas) > self.lb and sma > self._smas[0]
        bias = "bullish" if c > sma and up else "bearish" if c < sma else "neutral"
        if ctx.in_position:
            if c < sma:
                return Decision(Action.CLOSE, reason="Clôture sous la SMA", bias=bias)
            return Decision.hold(bias=bias)
        if c > sma and up:
            return self._long(ctx, c - self.k * a, setup=f"SMA{self.n}", bias=bias)
        return Decision.no_trade("Pas de tendance haussière", bias=bias)


@dataclass(frozen=True)
class FVGEntryParams:
    """Paramètres optimisables (🎛️) de l'entrée FVG. Aucune valeur n'est présumée optimale."""

    mode: str = "half"  # "touch" | "half" | "full_reject"
    min_size_atr: float = 0.3
    max_age_bars: int = 120
    stop_buffer_atr: float = 0.5
    take_profit_r: float | None = 2.0
    min_room_r: float = 1.0  # place minimale avant le premier FVG baissier, en R


class FVGTechnical(_Base):
    """Stratégie purement technique : retour dans un FVG haussier + réaction mesurable.

    - Entrée seulement après une **réaction** : bougie haussière qui clôture au-dessus
      du milieu du FVG, sur une bougie qui a touché la zone (jamais au premier contact sans réaction).
    - ``mode`` : ``touch`` (toute pénétration), ``half`` (mitigation ≥ 50 %),
      ``full_reject`` (mitigation complète puis clôture au-dessus du haut de zone).
    - Stop sous le bas du FVG − ``stop_buffer_atr`` × ATR ; NO_TRADE si hors R8.
    - Obstacle : le bas du FVG baissier actif le plus proche au-dessus doit laisser
      au moins ``min_room_r`` R de potentiel.
    """

    name = "fvg_technical"

    def __init__(self, settings: Settings | None = None, risk_fraction: float | None = None, params: FVGEntryParams | None = None) -> None:
        super().__init__(settings, risk_fraction)
        self.p = params or FVGEntryParams()
        self.engine = FVGEngine(FVGParams(min_size_atr=self.p.min_size_atr))

    def _qualifies(self, f, i: int, c) -> bool:
        st = f.state
        if not st.is_active or st.age(i) > self.p.max_age_bars or st.age(i) < 1:
            return False
        if st.penetration(c) <= 0:  # la bougie courante doit toucher la zone
            return False
        if not (c.is_bullish and c.close >= st.midpoint):
            return False
        if self.p.mode == "touch":
            return True
        if self.p.mode == "half":
            return st.max_mitigation >= 0.5
        if self.p.mode == "full_reject":
            return st.max_mitigation >= 1.0 and c.close > st.top
        raise ValueError(f"mode inconnu : {self.p.mode}")

    def on_bar(self, ctx: BarContext) -> Decision:
        a = self._atr(ctx)
        self.engine.update(ctx.candle)
        i = len(self.engine.candles) - 1
        c = ctx.candle
        if ctx.in_position or a is None:
            return Decision.hold() if ctx.in_position else Decision.no_trade("Préchauffage")
        cands = [f for f in self.engine.active(True) if self._qualifies(f, i, c)]
        if not cands:
            return Decision.no_trade("Aucun FVG qualifié")
        f = max(cands, key=lambda z: z.confirmed_index)
        stop = f.bottom - self.p.stop_buffer_atr * a
        entry = _entry_price(ctx, self.settings)
        risk_pts = entry - stop
        obstacles = [z.bottom for z in self.engine.active(False) if z.bottom > entry]
        if obstacles and (min(obstacles) - entry) < self.p.min_room_r * risk_pts:
            return Decision.no_trade("Obstacle (FVG baissier) trop proche", setup=f"FVG_{self.p.mode}")
        tp = entry + self.p.take_profit_r * risk_pts if self.p.take_profit_r else None
        return self._long(ctx, stop, take_profit_level=tp, setup=f"FVG_{self.p.mode}", reason=f"FVG {f.bottom:.1f}-{f.top:.1f}")


class FundamentalOnly(_Base):
    """Long dès que le biais point-in-time est haussier, sortie quand il ne l'est plus. Stop k × ATR."""

    name = "fundamental_only"

    def __init__(self, settings: Settings | None = None, risk_fraction: float | None = None, bias_series: str = "bias/swing", regime_series: str = "regime", atr_stop: float = 3.0) -> None:
        super().__init__(settings, risk_fraction)
        self.bias_series = bias_series
        self.regime_series = regime_series
        self.k = atr_stop

    def on_bar(self, ctx: BarContext) -> Decision:
        a = self._atr(ctx)
        bias = ctx.pit.value(self.bias_series, "unknown")
        regime = ctx.pit.value(self.regime_series, "") or ""
        if ctx.in_position:
            if bias != "bullish":
                return Decision(Action.CLOSE, reason=f"Biais {bias}", bias=bias, regime=regime)
            return Decision.hold(bias=bias, regime=regime)
        if bias != "bullish" or a is None:
            return Decision.no_trade(f"Biais {bias}", bias=bias, regime=regime)
        return self._long(ctx, ctx.candle.close - self.k * a, setup="FUNDAMENTAL", bias=bias, regime=regime)


class FundamentalFilter:
    """Stratégie complète : la direction vient du biais fondamental, la technique choisit l'entrée.

    - Une entrée technique n'est acceptée que si le biais point-in-time est haussier.
    - Position ouverte et biais devenu baissier : CLOSE (invalidation fondamentale).
    """

    def __init__(self, inner, bias_series: str = "bias/swing", regime_series: str = "regime", close_on: tuple[str, ...] = ("bearish",)) -> None:
        self.inner = inner
        self.bias_series = bias_series
        self.regime_series = regime_series
        self.close_on = close_on
        self.name = f"full({getattr(inner, 'name', type(inner).__name__)})"

    def on_bar(self, ctx: BarContext) -> Decision:
        d = self.inner.on_bar(ctx)  # toujours appelé : l'état technique doit rester à jour
        bias = ctx.pit.value(self.bias_series, "unknown")
        regime = ctx.pit.value(self.regime_series, "") or ""
        if ctx.in_position and bias in self.close_on:
            return Decision(Action.CLOSE, reason=f"Invalidation fondamentale ({bias})", bias=bias, regime=regime, setup=d.setup)
        if d.action == Action.LONG and bias != "bullish":
            return Decision.no_trade(f"Setup {d.setup} refusé : biais {bias}", bias=bias, regime=regime, setup=d.setup)
        return Decision(
            d.action, d.stop_level, d.take_profit_level, d.risk_fraction, d.size, d.reason, d.confidence,
            d.setup, regime, bias, d.sources, d.meta,
        )


class RandomEntry(_Base):
    """Entrées aléatoires (graine fixe), même stop ATR et même TP en R : hasard de référence."""

    name = "random_entry"

    def __init__(self, settings: Settings | None = None, risk_fraction: float | None = None, probability: float = 0.02, atr_stop: float = 3.0, take_profit_r: float | None = 2.0, seed: int = 0) -> None:
        super().__init__(settings, risk_fraction)
        self.prob = probability
        self.k = atr_stop
        self.tp_r = take_profit_r
        self.rng = random.Random(seed)

    def on_bar(self, ctx: BarContext) -> Decision:
        a = self._atr(ctx)
        draw = self.rng.random()  # tiré à chaque bougie : séquence indépendante des positions
        if ctx.in_position:
            return Decision.hold()
        if a is None or draw >= self.prob:
            return Decision.no_trade()
        entry = _entry_price(ctx, self.settings)
        stop = ctx.candle.close - self.k * a
        tp = entry + self.tp_r * (entry - stop) if self.tp_r else None
        return self._long(ctx, stop, take_profit_level=tp, setup="RANDOM")
