"""Backtester événementiel chronologique.

Boucle, pour chaque bougie ``i`` (bid, clôturée à ``t_i + timeframe``) :

1. **Ouverture de i** : une cotation ``open / open + spread`` est envoyée au
   broker simulé (``on_quote``). Les stops touchés par un gap d'ouverture sont
   exécutés : au niveau exact si le stop est garanti, à l'ouverture sinon.
2. **Exécution des ordres en attente** décidés à la clôture de ``i-1`` : au prix
   d'ouverture de ``i`` (jamais au prix de clôture qui a servi à décider).
   Dimensionnement (R10, R15), puis passage obligatoire par le Risk Engine via
   ``SimulatedBroker.place_market_order``. Bougie non négociable, marché fermé,
   spread trop large, taille < 0,01 lot : l'ordre est refusé et journalisé.
3. **Vie de la bougie** : ``SimulatedBroker.on_bar`` (stops/TP intra-bougie,
   financement overnight, stop-out de marge, verrou de drawdown).
4. **Clôture de i** : l'equity est enregistrée ; la stratégie reçoit un
   ``BarContext`` qui ne contient que les bougies ``0..i`` et les données
   point-in-time disponibles à ``t_i + timeframe``. Sa décision est exécutée à
   l'étape 2 de la bougie suivante (sauf la remontée de stop, posée tout de suite
   au prix courant).

Le backtester ne recalcule ni la marge, ni les coûts, ni les règles de risque :
tout passe par ``SimulatedBroker`` et ``RiskEngine``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from bot_trading.broker.simulated import SimulatedBroker
from bot_trading.config import Settings, TradingMode
from bot_trading.core import contract
from bot_trading.core.decision import Action, Decision
from bot_trading.core.models import Candle, ClosedTrade, Direction, OrderRequest, Position, Quote, Timeframe
from bot_trading.marketdata.candles import bucket_start
from bot_trading.marketdata.source import Bar, Observation, PointInTimeSource
from bot_trading.strategy.sizing import size_for_risk


class LookaheadError(IndexError):
    """Accès à une donnée postérieure à l'instant de décision."""


# ------------------------------------------------------------------ vues causales


class CausalHistory(Sequence):
    """Vue en lecture seule des bougies ``0..limit``. Tout accès au-delà lève ``LookaheadError``."""

    __slots__ = ("_bars", "_limit")

    def __init__(self, bars: Sequence[Bar], limit: int) -> None:
        self._bars = bars
        self._limit = limit

    def __len__(self) -> int:
        return self._limit + 1

    def __getitem__(self, key):  # type: ignore[override]
        n = self._limit + 1
        if isinstance(key, slice):
            start, stop, step = key.indices(n)
            return [self._bars[j] for j in range(start, stop, step)]
        if key < 0:
            key += n
        if key < 0:
            raise IndexError(key)
        if key >= n:
            raise LookaheadError(f"Bougie {key} demandée à l'instant {self._limit} : look-ahead interdit")
        return self._bars[key]


class PointInTimeView:
    """Accès aux séries point-in-time, figé à l'instant de décision."""

    def __init__(self, source: PointInTimeSource | None, now: datetime) -> None:
        self._source = source
        self.now = now

    def get(self, series: str) -> Observation | None:
        if self._source is None:
            return None
        obs = self._source.as_of(series, self.now)
        if obs is not None and obs.available_at > self.now:
            raise LookaheadError(f"{series}: observation disponible à {obs.available_at}, décision à {self.now}")
        return obs

    def value(self, series: str, default=None):
        obs = self.get(series)
        return default if obs is None else obs.value


class MultiTimeframe:
    """Agrège la série de base en bougies de timeframes supérieurs **clôturées** uniquement.

    Une bougie agrégée est émise dès que la bougie de base qui la termine est
    close (fin alignée sur la frontière du panier), ou, à défaut (séance
    interrompue), à la première bougie du panier suivant.
    """

    def __init__(self, base: Timeframe, targets: Sequence[Timeframe]) -> None:
        self.base = base
        self.targets = [t for t in targets if t.minutes > base.minutes]
        self.closed: dict[Timeframe, list[Candle]] = {t: [] for t in self.targets}
        self._cur: dict[Timeframe, list[Candle]] = {t: [] for t in self.targets}
        self._key: dict[Timeframe, datetime | None] = {t: None for t in self.targets}

    def _flush(self, tf: Timeframe) -> None:
        cur = self._cur[tf]
        if cur:
            self.closed[tf].append(
                Candle(self._key[tf], cur[0].open, max(c.high for c in cur), min(c.low for c in cur), cur[-1].close, sum(c.volume for c in cur))  # type: ignore[arg-type]
            )
        self._cur[tf] = []

    def update(self, c: Candle) -> None:
        end = c.time + timedelta(minutes=self.base.minutes)
        for tf in self.targets:
            key = bucket_start(c.time, tf)
            if self._key[tf] is not None and key != self._key[tf]:
                self._flush(tf)
            self._key[tf] = key
            self._cur[tf].append(c)
            if bucket_start(end, tf) != key:
                self._flush(tf)


@dataclass
class BarContext:
    """Tout ce qu'une stratégie peut voir à la clôture de la bougie ``index``."""

    index: int
    bar: Bar
    now: datetime  # clôture de la bougie
    history: CausalHistory
    pit: PointInTimeView
    mtf: MultiTimeframe
    positions: tuple[Position, ...]
    equity_eur: float
    balance_eur: float
    trading_enabled: bool  # False pendant le préchauffage (décisions ignorées)

    @property
    def candle(self) -> Candle:
        return self.bar.bid

    @property
    def in_position(self) -> bool:
        return bool(self.positions)

    def closed_candles(self, tf: Timeframe) -> list[Candle]:
        if tf == self.bar.timeframe:
            return [b.bid for b in self.history]
        return self.mtf.closed[tf]


@runtime_checkable
class Strategy(Protocol):
    name: str

    def on_bar(self, ctx: BarContext) -> Decision: ...


# ------------------------------------------------------------------ résultats


@dataclass(frozen=True)
class TradeRecord:
    trade: ClosedTrade
    initial_risk_eur: float
    setup: str = ""
    regime: str = ""
    bias: str = ""
    decided_at: datetime | None = None

    @property
    def net_pnl_eur(self) -> float:
        return self.trade.net_pnl_eur

    @property
    def r_multiple(self) -> float | None:
        return self.net_pnl_eur / self.initial_risk_eur if self.initial_risk_eur > 0 else None

    @property
    def holding_hours(self) -> float:
        return (self.trade.closed_at - self.trade.opened_at).total_seconds() / 3600


@dataclass(frozen=True)
class DecisionRecord:
    time: datetime
    index: int
    decision: Decision
    outcome: str  # FILLED, VETO: ..., SIZE_BELOW_MIN, CLOSED, STOP_RAISED, IGNORED_IN_POSITION, NONE…
    executed_at: datetime | None = None


@dataclass
class BacktestResult:
    strategy: str
    settings: Settings
    timeframe: Timeframe
    start: datetime | None
    end: datetime | None
    trade_start: datetime | None
    initial_equity_eur: float
    final_equity_eur: float
    trades: list[TradeRecord]
    equity_curve: list[tuple[datetime, float]]
    decisions: list[DecisionRecord]
    interruptions: list[tuple[datetime, datetime]]
    halted: bool
    bars: int
    exposure_bars: int
    ledger: list[tuple[datetime, str, float]] = field(default_factory=list)

    def metrics(self, **kw):
        from bot_trading.backtest.metrics import compute_metrics

        return compute_metrics(self, **kw)


# ------------------------------------------------------------------ moteur


@dataclass(frozen=True)
class BacktestConfig:
    settings: Settings = field(default_factory=lambda: Settings(mode=TradingMode.BACKTEST))
    # Fraction de l'equity risquée si la décision n'en précise pas. Le Risk Engine
    # plafonne de toute façon (R8 50 % de la marge, R9 20 % de l'equity).
    default_risk_fraction: float = 0.10
    eurusd: float | None = None
    # Mission : une seule position simultanée dans la première version.
    max_positions: int = 1
    close_at_end: bool = True
    # Fermetures et fenêtres d'annonce, évaluées à l'instant d'exécution.
    market_open: Callable[[datetime], bool] | None = None
    event_blackout: Callable[[datetime], bool] | None = None
    # R19 (🧪) : marge totale ≤ cette fraction de l'equity au dimensionnement. None = pas de plafond.
    max_margin_fraction: float | None = None
    # Timeframes supérieurs agrégés pour la stratégie.
    higher_timeframes: tuple[Timeframe, ...] = (Timeframe.M15, Timeframe.H1, Timeframe.H4, Timeframe.D1)


class EventLoop:
    """Cœur partagé par le backtester et le paper trading : une bougie à la fois.

    ``step(bar)`` applique les étapes 1 à 4 décrites en tête de module. Le
    backtester l'appelle sur une série historique ; la boucle de paper trading
    l'appelle sur chaque nouvelle bougie close reçue en temps réel. Les deux
    exécutent donc exactement le même code (exécution, dimensionnement, broker
    simulé, Risk Engine).

    ``history_limit`` borne la mémoire des bougies conservées pour
    ``BarContext.history`` (paper trading de longue durée) ; ``ctx.index`` reste
    l'indice global de la bougie dans le flux.
    """

    def __init__(
        self,
        config: BacktestConfig,
        strategy: Strategy,
        pit: PointInTimeSource | None = None,
        broker: SimulatedBroker | None = None,
        history_limit: int | None = None,
    ) -> None:
        self.config = config
        self.strategy = strategy
        self.pit = pit
        self.broker = broker or SimulatedBroker(config.settings, eurusd=config.eurusd)
        self.history_limit = history_limit
        self.bars: list[Bar] = []
        self.offset = 0  # indice global de self.bars[0]
        self.timeframe: Timeframe | None = None
        self.mtf: MultiTimeframe | None = None
        self.pending: Decision | None = None
        self.pending_rec: tuple[int, datetime] | None = None
        # deal_id → (risque initial en EUR, décision d'ouverture, heure de la décision)
        self.info: dict[str, tuple[float, Decision, datetime]] = {}
        self.decisions: list[DecisionRecord] = []
        self.equity_curve: list[tuple[datetime, float]] = []
        self.interruptions: list[tuple[datetime, datetime]] = []
        self.exposure_bars = 0

    @property
    def last_time(self) -> datetime | None:
        return self.bars[-1].time if self.bars else None

    @property
    def next_index(self) -> int:
        return self.offset + len(self.bars)

    def step(self, bar: Bar, trading: bool = True, decide: bool = True) -> list[DecisionRecord]:
        """Traite une bougie close. Renvoie les décisions journalisées pendant ce pas.

        ``trading=False`` : préchauffage (la stratégie voit la bougie, aucun ordre).
        ``decide=False`` : la décision prise à la clôture n'est pas mise en attente
        (dernière bougie d'un backtest).
        """
        cfg = self.config
        settings = cfg.settings
        broker = self.broker
        if self.timeframe is None:
            self.timeframe = bar.timeframe
            self.mtf = MultiTimeframe(bar.timeframe, cfg.higher_timeframes)
        tf = self.timeframe
        if bar.timeframe != tf:
            raise ValueError("Toutes les bougies doivent avoir le même timeframe")
        step = timedelta(minutes=tf.minutes)
        prev = self.bars[-1] if self.bars else None
        if prev is not None:
            if bar.time <= prev.time:
                raise ValueError(f"Bougies non chronologiques : {prev.time} puis {bar.time}")
            if bar.time - prev.time > step:
                self.interruptions.append((prev.time + step, bar.time))
        self.bars.append(bar)
        if self.history_limit is not None and len(self.bars) > self.history_limit:
            drop = len(self.bars) - self.history_limit
            del self.bars[:drop]
            self.offset += drop
        i = self.next_index - 1
        n_before = len(self.decisions)
        spread = settings.costs.default_spread_points if bar.spread is None else bar.spread

        # 1. ouverture (gaps)
        broker.on_quote(Quote(bar.time, bar.bid.open, bar.bid.open + spread))

        # 2. ordres en attente
        if self.pending is not None and self.pending_rec is not None:
            outcome = self._execute(self.pending, bar, self.pending_rec[1])
            self.decisions.append(DecisionRecord(self.pending_rec[1], self.pending_rec[0], self.pending, outcome, bar.time))
            self.pending, self.pending_rec = None, None

        # 3. vie de la bougie
        broker.on_bar(bar.bid, spread, tf.minutes)
        if broker.positions:
            self.exposure_bars += 1
        assert self.mtf is not None
        self.mtf.update(bar.bid)
        now = bar.time + step
        self.equity_curve.append((now, broker.equity_eur()))

        # 4. décision à la clôture
        ctx = BarContext(
            index=i,
            bar=bar,
            now=now,
            history=_OffsetHistory(self.bars, self.offset),
            pit=PointInTimeView(self.pit, now),
            mtf=self.mtf,
            positions=tuple(replace(p, meta=dict(p.meta)) for p in broker.positions.values()),
            equity_eur=broker.equity_eur(),
            balance_eur=broker.balance_eur,
            trading_enabled=trading,
        )
        decision = self.strategy.on_bar(ctx)
        if decision is None:
            decision = Decision.no_trade("None")
        if trading and decide:
            if decision.action in (Action.LONG, Action.CLOSE):
                self.pending, self.pending_rec = decision, (i, now)
            elif decision.action == Action.HOLD and decision.stop_level is not None:
                raised = [broker.modify_stop(d, decision.stop_level) for d in list(broker.positions)]
                self.decisions.append(DecisionRecord(now, i, decision, "STOP_RAISED" if any(raised) else "STOP_UNCHANGED", now))
            else:
                self.decisions.append(DecisionRecord(now, i, decision, decision.action.value))
        return self.decisions[n_before:]

    def trade_record(self, t: ClosedTrade) -> TradeRecord:
        risk, d, when = self.info.get(t.deal_id, (0.0, Decision.no_trade(), None))
        return TradeRecord(t, risk, d.setup, d.regime, d.bias, when)

    # -------------------------------------------------------------- exécution
    def _execute(self, d: Decision, bar: Bar, decided_at: datetime) -> str:
        cfg = self.config
        broker = self.broker
        if d.action == Action.CLOSE:
            if not broker.positions:
                return "NOTHING_TO_CLOSE"
            if not bar.tradable or (cfg.market_open and not cfg.market_open(bar.time)):
                return "MARKET_CLOSED"
            for deal_id in list(broker.positions):
                broker.close_position(deal_id, d.reason or "STRATEGY_CLOSE", when=bar.time)
            return "CLOSED"

        # LONG
        if len(broker.positions) >= cfg.max_positions:
            return "IGNORED_IN_POSITION"
        if d.stop_level is None:
            return "INVALID_DECISION: stop absent"
        q = broker.last_quote
        assert q is not None
        spec = broker.spec
        costs = broker.costs
        stop = d.stop_level
        # R10 : stop trop proche → placé à la distance minimale (le risque est recalculé).
        if q.ask - stop < spec.min_guaranteed_stop_distance:
            stop = q.ask - spec.min_guaranteed_stop_distance
        size = d.size
        if size is None:
            if (q.ask - stop) + costs.guaranteed_stop_premium_points <= 0:
                return "VETO: STOP_WRONG_SIDE (gap sous le stop)"
            sized = size_for_risk(
                equity_eur=broker.equity_eur(),
                entry=q.ask,
                stop=stop,
                risk_fraction=d.risk_fraction if d.risk_fraction is not None else cfg.default_risk_fraction,
                spec=spec,
                costs=costs,
                limits=broker.limits,
                eurusd=broker.eurusd,
                open_exposure_lots=broker.open_exposure_lots(),
                used_margin_eur=broker.used_margin_eur(),
                max_margin_fraction=cfg.max_margin_fraction,
                check_max_distance=False,  # R8 : vérifié par le Risk Engine (veto journalisé)
            )
            if not sized.ok:
                if sized.reason == "STOP_WRONG_SIDE":
                    return "VETO: STOP_WRONG_SIDE (gap sous le stop)"
                return "SIZE_BELOW_MIN"  # R11 : jamais de stop rapproché pour faire entrer la taille
            size = sized.size
        order = OrderRequest(
            epic=spec.epic,
            direction=Direction.BUY,
            size=size,
            stop_level=stop,
            guaranteed_stop=True,
            take_profit_level=d.take_profit_level,
            reason=d.reason,
        )
        market_open = bar.tradable and (cfg.market_open(bar.time) if cfg.market_open else True)
        blackout = cfg.event_blackout(bar.time) if cfg.event_blackout else False
        res = broker.place_market_order(order, now=bar.time, market_open=market_open, event_blackout=blackout)
        if not res.accepted or res.position is None:
            return res.message
        p = res.position
        risk = (p.entry_price - p.stop_level + costs.guaranteed_stop_premium_points) * contract.value_per_point_eur(p.size, spec, broker.eurusd)
        self.info[p.deal_id] = (risk, d, decided_at)
        return "FILLED"


class _OffsetHistory(CausalHistory):
    """``CausalHistory`` sur une fenêtre glissante (indices locaux, dernière bougie = instant courant)."""

    __slots__ = ("offset",)

    def __init__(self, bars: Sequence[Bar], offset: int) -> None:
        super().__init__(bars, len(bars) - 1)
        self.offset = offset


class Backtester:
    def __init__(self, config: BacktestConfig | None = None, pit: PointInTimeSource | None = None) -> None:
        self.config = config or BacktestConfig()
        if self.config.settings.mode != TradingMode.BACKTEST:
            self.config = replace(self.config, settings=replace(self.config.settings, mode=TradingMode.BACKTEST))
        self.pit = pit

    def run(self, strategy: Strategy, bars: Sequence[Bar], trade_start: datetime | None = None) -> BacktestResult:
        """Rejoue ``bars``. Avant ``trade_start``, la stratégie se préchauffe sans trader."""
        cfg = self.config
        settings = cfg.settings
        if not bars:
            raise ValueError("Aucune bougie")
        tf = bars[0].timeframe
        for a, b in zip(bars, bars[1:]):
            if b.time <= a.time:
                raise ValueError(f"Bougies non chronologiques : {a.time} puis {b.time}")
            if b.timeframe != tf:
                raise ValueError("Toutes les bougies doivent avoir le même timeframe")
        step = timedelta(minutes=tf.minutes)

        loop = EventLoop(cfg, strategy, self.pit)
        broker = loop.broker
        initial_equity: float | None = None
        last_i = len(bars) - 1
        for i, bar in enumerate(bars):
            trading = trade_start is None or bar.time >= trade_start
            if trading and initial_equity is None:
                initial_equity = broker.equity_eur()
            loop.step(bar, trading, decide=i != last_i)

        equity_curve = loop.equity_curve
        last = bars[-1]
        if cfg.close_at_end:
            for deal_id in list(broker.positions):
                broker.close_position(deal_id, "END_OF_BACKTEST", when=last.time + step)
            if equity_curve:
                equity_curve[-1] = (equity_curve[-1][0], broker.equity_eur())

        trades = [loop.trade_record(t) for t in broker.closed_trades]
        return BacktestResult(
            strategy=getattr(strategy, "name", type(strategy).__name__),
            settings=settings,
            timeframe=tf,
            start=bars[0].time,
            end=last.time + step,
            trade_start=trade_start,
            initial_equity_eur=initial_equity if initial_equity is not None else broker.equity_eur(),
            final_equity_eur=broker.equity_eur(),
            trades=trades,
            equity_curve=[(t, e) for t, e in equity_curve if trade_start is None or t > trade_start],
            decisions=loop.decisions,
            interruptions=loop.interruptions,
            halted=broker.risk.halted,
            bars=len(bars),
            exposure_bars=loop.exposure_bars,
            ledger=list(broker.ledger),
        )
