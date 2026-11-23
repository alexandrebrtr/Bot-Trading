"""Validation : walk-forward, réserve hors échantillon, stress tests, comparaison de benchmarks.

Protocole (EXPERIMENTS.md) : optimisation sur 24 mois, test sur les 6 mois
suivants, pas de 6 mois ; les 12 derniers mois forment une **réserve finale**
ouverte une seule fois (``HoldoutGuard``).

Chaque fenêtre de test repart du capital initial (20 €) et se préchauffe sur
les bougies **antérieures** à la fenêtre, sans trader : aucune information
de la fenêtre de test n'entre dans le choix des paramètres.
"""

from __future__ import annotations

import itertools
import json
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from bot_trading.backtest.engine import Backtester, BacktestConfig, BacktestResult
from bot_trading.backtest.metrics import Metrics, compute_metrics
from bot_trading.marketdata.source import Bar, PointInTimeSource

StrategyFactory = Callable[[dict], object]


def add_months(t: datetime, months: int) -> datetime:
    m = t.month - 1 + months
    y = t.year + m // 12
    m = m % 12 + 1
    day = min(t.day, [31, 29 if y % 4 == 0 and (y % 100 or y % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1])
    return t.replace(year=y, month=m, day=day)


@dataclass(frozen=True)
class Fold:
    train_start: datetime
    train_end: datetime  # exclu
    test_start: datetime
    test_end: datetime  # exclu


def walk_forward_folds(start: datetime, end: datetime, train_months: int = 24, test_months: int = 6, step_months: int = 6, holdout_months: int = 12) -> tuple[list[Fold], tuple[datetime, datetime] | None]:
    """Fenêtres glissantes dans [start, end − réserve), puis la réserve finale."""
    holdout_start = add_months(end, -holdout_months) if holdout_months else end
    folds = []
    t = start
    while True:
        tr_end = add_months(t, train_months)
        te_end = add_months(tr_end, test_months)
        if te_end > holdout_start:
            break
        folds.append(Fold(t, tr_end, tr_end, te_end))
        t = add_months(t, step_months)
    return folds, ((holdout_start, end) if holdout_months else None)


def slice_bars(bars: Sequence[Bar], start: datetime, end: datetime, warmup_bars: int = 0) -> tuple[list[Bar], datetime]:
    """Bougies [start, end) précédées de ``warmup_bars`` bougies antérieures (préchauffage)."""
    i0 = next((i for i, b in enumerate(bars) if b.time >= start), len(bars))
    i1 = next((i for i, b in enumerate(bars) if b.time >= end), len(bars))
    return list(bars[max(0, i0 - warmup_bars):i1]), start


def param_grid(grid: dict[str, Sequence]) -> list[dict]:
    keys = list(grid)
    return [dict(zip(keys, vals)) for vals in itertools.product(*(grid[k] for k in keys))]


def default_objective(m: Metrics, min_trades: int = 5) -> float:
    """Calmar si assez de trades, sinon −∞. Jamais le rendement brut seul."""
    if m.trades < min_trades or m.calmar is None:
        return -math.inf
    return m.calmar


@dataclass
class FoldResult:
    fold: Fold
    best_params: dict
    in_sample_score: float
    in_sample: Metrics | None  # None : paramètres fixés d'avance (fixed_out_of_sample)
    out_of_sample: Metrics
    oos_result: BacktestResult = field(repr=False)


@dataclass
class WalkForwardReport:
    folds: list[FoldResult]
    n_trials: int  # combinaisons × fenêtres : à reporter dans EXPERIMENTS.md

    @property
    def oos_trades(self):
        return [t for f in self.folds for t in f.oos_result.trades]

    def summary(self) -> dict:
        trades = self.oos_trades
        rs = [t.r_multiple for t in trades if t.r_multiple is not None]
        wins = sum(t.net_pnl_eur for t in trades if t.net_pnl_eur > 0)
        losses = -sum(t.net_pnl_eur for t in trades if t.net_pnl_eur <= 0)
        returns = [f.out_of_sample.net_return for f in self.folds]
        return {
            "folds": len(self.folds),
            "n_trials": self.n_trials,
            "oos_trades": len(trades),
            "oos_expectancy_r": sum(rs) / len(rs) if rs else None,
            "oos_profit_factor": wins / losses if losses > 0 else None,
            "oos_net_pnl_eur": sum(t.net_pnl_eur for t in trades),
            "oos_positive_folds": sum(r > 0 for r in returns),
            "oos_worst_drawdown": max((f.out_of_sample.max_drawdown for f in self.folds), default=None),
            "params_by_fold": [f.best_params for f in self.folds],
        }


def walk_forward(
    factory: StrategyFactory,
    grid: dict[str, Sequence],
    bars: Sequence[Bar],
    folds: Sequence[Fold],
    config: BacktestConfig | None = None,
    pit: PointInTimeSource | None = None,
    warmup_bars: int = 300,
    objective: Callable[[Metrics], float] = default_objective,
) -> WalkForwardReport:
    bt = Backtester(config, pit)
    combos = param_grid(grid) or [{}]
    out: list[FoldResult] = []
    for fold in folds:
        train, ts = slice_bars(bars, fold.train_start, fold.train_end, warmup_bars)
        best: tuple[float, dict, Metrics] | None = None
        for params in combos:
            m = compute_metrics(bt.run(factory(params), train, trade_start=ts))
            s = objective(m)
            if best is None or s > best[0]:
                best = (s, params, m)
        assert best is not None
        test, ts2 = slice_bars(bars, fold.test_start, fold.test_end, warmup_bars)
        res = bt.run(factory(best[1]), test, trade_start=ts2)
        out.append(FoldResult(fold, best[1], best[0], best[2], compute_metrics(res), res))
    return WalkForwardReport(out, n_trials=len(combos) * len(folds))


def fixed_out_of_sample(
    factory: StrategyFactory,
    params: dict,
    bars: Sequence[Bar],
    folds: Sequence[Fold],
    config: BacktestConfig | None = None,
    pit: PointInTimeSource | None = None,
    warmup_bars: int = 300,
) -> WalkForwardReport:
    """Mêmes fenêtres de test que le walk-forward, paramètres fixés d'avance (aucune sélection).

    Sert à comparer quelques valeurs d'un paramètre fenêtre par fenêtre ; chaque
    valeur testée compte comme un essai de plus (``n_trials``).
    """
    bt = Backtester(config, pit)
    out: list[FoldResult] = []
    for fold in folds:
        test, ts = slice_bars(bars, fold.test_start, fold.test_end, warmup_bars)
        res = bt.run(factory(params), test, trade_start=ts)
        out.append(FoldResult(fold, params, float("nan"), None, compute_metrics(res), res))
    return WalkForwardReport(out, n_trials=len(folds))


class HoldoutAlreadyUsed(RuntimeError):
    pass


class HoldoutGuard:
    """La réserve finale ne s'ouvre qu'une fois. Avec ``journal``, le verrou survit au processus."""

    def __init__(self, journal: str | Path | None = None) -> None:
        self.journal = Path(journal) if journal else None
        self._used = bool(self.journal and self.journal.exists())

    @property
    def used(self) -> bool:
        return self._used

    def open(self, reason: str, params: dict | None = None) -> None:
        if self._used:
            raise HoldoutAlreadyUsed("La réserve finale a déjà été ouverte : toute nouvelle mesure dessus est biaisée.")
        self._used = True
        if self.journal:
            self.journal.parent.mkdir(parents=True, exist_ok=True)
            self.journal.write_text(json.dumps({"opened_at": datetime.now(timezone.utc).isoformat(), "reason": reason, "params": params or {}}, default=str))


def run_holdout(guard: HoldoutGuard, factory: StrategyFactory, params: dict, bars: Sequence[Bar], window: tuple[datetime, datetime], reason: str, config: BacktestConfig | None = None, pit: PointInTimeSource | None = None, warmup_bars: int = 300) -> Metrics:
    guard.open(reason, params)
    data, ts = slice_bars(bars, window[0], window[1], warmup_bars)
    return compute_metrics(Backtester(config, pit).run(factory(params), data, trade_start=ts))


# ------------------------------------------------------------------ stress tests


def _scale_spread(bars: Sequence[Bar], k: float, default: float) -> list[Bar]:
    return [replace(b, spread=(default if b.spread is None else b.spread) * k) for b in bars]


def stress_scenarios(config: BacktestConfig) -> dict[str, tuple[BacktestConfig, float]]:
    """Scénarios défavorables : (configuration, multiplicateur de spread)."""
    s = config.settings
    c = s.costs
    return {
        "base": (config, 1.0),
        "spread_x2": (config, 2.0),
        "spread_x3": (config, 3.0),
        "financement_x2": (replace(config, settings=replace(s, costs=replace(c, overnight_long_rate_daily=c.overnight_long_rate_daily * 2))), 1.0),
        "prime_gsl_1pt": (replace(config, settings=replace(s, costs=replace(c, guaranteed_stop_premium_points=c.guaranteed_stop_premium_points + 1.0))), 1.0),
        "eurusd_1.30": (replace(config, eurusd=1.30), 1.0),
        "eurusd_1.00": (replace(config, eurusd=1.00), 1.0),
        "cumul_defavorable": (replace(config, settings=replace(s, costs=replace(c, overnight_long_rate_daily=c.overnight_long_rate_daily * 2, guaranteed_stop_premium_points=c.guaranteed_stop_premium_points + 1.0))), 2.0),
    }


def run_stress(factory: StrategyFactory, params: dict, bars: Sequence[Bar], config: BacktestConfig | None = None, pit: PointInTimeSource | None = None, trade_start: datetime | None = None) -> dict[str, Metrics]:
    config = config or BacktestConfig()
    out = {}
    for name, (cfg, k) in stress_scenarios(config).items():
        data = _scale_spread(bars, k, cfg.settings.costs.default_spread_points) if k != 1.0 else list(bars)
        out[name] = compute_metrics(Backtester(cfg, pit).run(factory(params), data, trade_start=trade_start))
    return out


# ------------------------------------------------------------------ benchmarks


def compare(factories: dict[str, Callable[[], object]], bars: Sequence[Bar], config: BacktestConfig | None = None, pit: PointInTimeSource | None = None, trade_start: datetime | None = None) -> dict[str, Metrics]:
    """Lance chaque stratégie sur les mêmes bougies, mêmes coûts, même Risk Engine."""
    bt = Backtester(config, pit)
    return {name: compute_metrics(bt.run(f(), bars, trade_start=trade_start)) for name, f in factories.items()}


def metrics_table(results: dict[str, Metrics]) -> str:
    """Tableau Markdown des métriques principales."""

    def f(v, pct=False, nd=2):
        if v is None:
            return "—"
        return f"{v * 100:.1f} %" if pct else f"{v:.{nd}f}"

    head = "| Stratégie | Trades | P&L net € | Rendement | DD max | Sharpe | Sortino | Calmar | PF | Espérance R | Win rate | Financement € | Ruine |"
    sep = "|" + "---|" * 13
    rows = [head, sep]
    for name, m in results.items():
        rows.append(
            f"| {name} | {m.trades} | {f(m.net_pnl_eur)} | {f(m.net_return, True)} | {f(m.max_drawdown, True)} | {f(m.sharpe)} | {f(m.sortino)} | {f(m.calmar)} | {f(m.profit_factor)} | {f(m.expectancy_r)} | {f(m.win_rate, True)} | {f(m.funding_eur)} | {f(m.risk_of_ruin, True)} |"
        )
    return "\n".join(rows)
