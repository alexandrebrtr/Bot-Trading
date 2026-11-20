"""Métriques de performance (mission, section 17) et contrôle du surapprentissage.

Toutes les valeurs monétaires sont nettes, en EUR (spread, financement, prime
de stop garanti inclus via le broker simulé). Le rendement journalier est
calculé sur l'equity marquée à la dernière clôture de chaque jour UTC.
"""

from __future__ import annotations

import bisect
import math
import random
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from statistics import NormalDist

from bot_trading.core.decision import Action

_N = NormalDist()
_EULER_GAMMA = 0.5772156649015329


@dataclass
class GroupStats:
    trades: int
    net_pnl_eur: float
    win_rate: float | None
    expectancy_r: float | None


@dataclass
class Metrics:
    strategy: str
    initial_equity_eur: float
    final_equity_eur: float
    net_pnl_eur: float
    net_return: float
    cagr: float | None
    max_drawdown: float
    max_drawdown_days: float
    sharpe: float | None
    sortino: float | None
    calmar: float | None
    trades: int
    win_rate: float | None
    avg_win_eur: float | None
    avg_loss_eur: float | None
    profit_factor: float | None
    expectancy_eur: float | None
    expectancy_r: float | None
    avg_holding_hours: float | None
    funding_eur: float
    fees_eur: float
    holding_cost_share: float | None  # (financement + primes) / |P&L brut|
    exposure: float  # part des bougies avec une position ouverte
    no_trade_share: float | None
    vetoes: dict[str, int]
    risk_of_ruin: float | None
    halted: bool
    interruptions: int
    by_regime: dict[str, GroupStats] = field(default_factory=dict)
    by_setup: dict[str, GroupStats] = field(default_factory=dict)
    by_exit: dict[str, GroupStats] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ------------------------------------------------------------------ séries


def max_drawdown(curve: Sequence[tuple[datetime, float]]) -> tuple[float, float]:
    """(drawdown max en fraction du plus haut, durée max sous l'eau en jours)."""
    peak = -math.inf
    peak_t: datetime | None = None
    mdd = 0.0
    longest = 0.0
    for t, e in curve:
        if e >= peak:
            peak, peak_t = e, t
        elif peak > 0:
            mdd = max(mdd, (peak - e) / peak)
            if peak_t is not None:
                longest = max(longest, (t - peak_t).total_seconds() / 86400)
    return mdd, longest


def daily_returns(curve: Sequence[tuple[datetime, float]], start_equity: float | None = None) -> list[float]:
    closes: dict = {}
    for t, e in curve:
        closes[t.date()] = e
    vals = list(closes.values())
    if start_equity is not None:
        vals = [start_equity] + vals
    return [(b / a - 1.0) for a, b in zip(vals, vals[1:]) if a > 0]


def _mean(x: Sequence[float]) -> float:
    return sum(x) / len(x)


def _std(x: Sequence[float]) -> float:
    if len(x) < 2:
        return 0.0
    m = _mean(x)
    return math.sqrt(sum((v - m) ** 2 for v in x) / (len(x) - 1))


def sharpe_ratio(returns: Sequence[float], periods_per_year: float = 252) -> float | None:
    if len(returns) < 2:
        return None
    s = _std(returns)
    return None if s == 0 else _mean(returns) / s * math.sqrt(periods_per_year)


def sortino_ratio(returns: Sequence[float], periods_per_year: float = 252) -> float | None:
    if len(returns) < 2:
        return None
    downside = math.sqrt(sum(min(r, 0.0) ** 2 for r in returns) / len(returns))
    return None if downside == 0 else _mean(returns) / downside * math.sqrt(periods_per_year)


def probabilistic_sharpe(returns: Sequence[float], sr_benchmark: float = 0.0) -> float | None:
    """Probabilité que le Sharpe (par période, non annualisé) dépasse ``sr_benchmark`` (Bailey & López de Prado)."""
    n = len(returns)
    if n < 3:
        return None
    s = _std(returns)
    if s == 0:
        return None
    m = _mean(returns)
    sr = m / s
    skew = sum(((r - m) / s) ** 3 for r in returns) / n
    kurt = sum(((r - m) / s) ** 4 for r in returns) / n
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr * sr
    if denom <= 0:
        return None
    return _N.cdf((sr - sr_benchmark) * math.sqrt(n - 1) / math.sqrt(denom))


def deflated_sharpe(returns: Sequence[float], n_trials: int, sr_trials_variance: float | None = None) -> float | None:
    """Sharpe déflaté : PSR contre le Sharpe maximal attendu par hasard sur ``n_trials`` essais.

    ``n_trials`` = nombre cumulé d'essais déclarés dans EXPERIMENTS.md.
    Sans variance mesurée entre essais, on prend la variance de l'estimateur (≈ 1/(T-1)).
    """
    n = len(returns)
    if n < 3 or n_trials < 1:
        return None
    if n_trials == 1:
        return probabilistic_sharpe(returns, 0.0)
    v = sr_trials_variance if sr_trials_variance is not None else 1.0 / (n - 1)
    e_max = math.sqrt(v) * ((1 - _EULER_GAMMA) * _N.inv_cdf(1 - 1 / n_trials) + _EULER_GAMMA * _N.inv_cdf(1 - 1 / (n_trials * math.e)))
    return probabilistic_sharpe(returns, e_max)


def risk_of_ruin(trade_returns: Sequence[float], ruin_drawdown: float = 0.5, horizon: int | None = None, n_sims: int = 2000, seed: int = 0) -> float | None:
    """Probabilité (bootstrap) d'atteindre ``ruin_drawdown`` depuis le plus haut en ``horizon`` trades."""
    if len(trade_returns) < 5:
        return None
    rng = random.Random(seed)
    horizon = horizon or max(len(trade_returns), 50)
    ruined = 0
    for _ in range(n_sims):
        eq = peak = 1.0
        for _ in range(horizon):
            eq *= 1 + rng.choice(trade_returns)
            peak = max(peak, eq)
            if eq <= peak * (1 - ruin_drawdown):
                ruined += 1
                break
    return ruined / n_sims


def _group(trades) -> GroupStats:
    n = len(trades)
    wins = [t for t in trades if t.net_pnl_eur > 0]
    rs = [t.r_multiple for t in trades if t.r_multiple is not None]
    return GroupStats(
        trades=n,
        net_pnl_eur=sum(t.net_pnl_eur for t in trades),
        win_rate=len(wins) / n if n else None,
        expectancy_r=_mean(rs) if rs else None,
    )


def _equity_before(curve: Sequence[tuple[datetime, float]], t: datetime, default: float) -> float:
    times = [c[0] for c in curve]
    i = bisect.bisect_right(times, t)
    return curve[i - 1][1] if i else default


# ------------------------------------------------------------------ calcul


def compute_metrics(result, periods_per_year: float = 252, ruin_drawdown: float | None = None, seed: int = 0) -> Metrics:
    curve = result.equity_curve
    init = result.initial_equity_eur
    final = result.final_equity_eur
    mdd, mdd_days = max_drawdown([(curve[0][0], init)] + curve if curve else [])
    rets = daily_returns(curve, init)
    cagr = None
    if curve and init > 0 and final > 0:
        years = (curve[-1][0] - (result.trade_start or result.start)).total_seconds() / (365.25 * 86400)
        if years > 0:
            cagr = (final / init) ** (1 / years) - 1
    trades = result.trades
    wins = [t.net_pnl_eur for t in trades if t.net_pnl_eur > 0]
    losses = [t.net_pnl_eur for t in trades if t.net_pnl_eur <= 0]
    rs = [t.r_multiple for t in trades if t.r_multiple is not None]
    gross = sum(t.trade.gross_pnl_eur for t in trades)
    funding = sum(t.trade.funding_eur for t in trades)
    fees = sum(t.trade.fees_eur for t in trades)
    trade_rets = []
    for t in trades:
        eq = _equity_before(curve, t.trade.opened_at, init)
        if eq > 0:
            trade_rets.append(t.net_pnl_eur / eq)
    vetoes: dict[str, int] = defaultdict(int)
    n_entries = 0
    n_no_trade = 0
    for d in result.decisions:
        if d.decision.action == Action.NO_TRADE:
            n_no_trade += 1
        if d.decision.action == Action.LONG:
            n_entries += 1
            if d.outcome != "FILLED":
                vetoes[d.outcome] += 1
    by_regime: dict[str, list] = defaultdict(list)
    by_setup: dict[str, list] = defaultdict(list)
    by_exit: dict[str, list] = defaultdict(list)
    for t in trades:
        by_regime[t.regime or "?"].append(t)
        by_setup[t.setup or "?"].append(t)
        by_exit[t.trade.exit_reason].append(t)
    ruin = ruin_drawdown if ruin_drawdown is not None else result.settings.risk.max_account_drawdown
    decided = n_no_trade + n_entries
    return Metrics(
        strategy=result.strategy,
        initial_equity_eur=init,
        final_equity_eur=final,
        net_pnl_eur=final - init,
        net_return=(final / init - 1) if init > 0 else 0.0,
        cagr=cagr,
        max_drawdown=mdd,
        max_drawdown_days=mdd_days,
        sharpe=sharpe_ratio(rets, periods_per_year),
        sortino=sortino_ratio(rets, periods_per_year),
        calmar=(cagr / mdd) if cagr is not None and mdd > 0 else None,
        trades=len(trades),
        win_rate=len(wins) / len(trades) if trades else None,
        avg_win_eur=_mean(wins) if wins else None,
        avg_loss_eur=_mean(losses) if losses else None,
        profit_factor=(sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None,
        expectancy_eur=_mean([t.net_pnl_eur for t in trades]) if trades else None,
        expectancy_r=_mean(rs) if rs else None,
        avg_holding_hours=_mean([t.holding_hours for t in trades]) if trades else None,
        funding_eur=funding,
        fees_eur=fees,
        holding_cost_share=((funding + fees) / abs(gross)) if gross else None,
        exposure=result.exposure_bars / result.bars if result.bars else 0.0,
        no_trade_share=(n_no_trade / decided) if decided else None,
        vetoes=dict(vetoes),
        risk_of_ruin=risk_of_ruin(trade_rets, ruin, seed=seed),
        halted=result.halted,
        interruptions=len(result.interruptions),
        by_regime={k: _group(v) for k, v in by_regime.items()},
        by_setup={k: _group(v) for k, v in by_setup.items()},
        by_exit={k: _group(v) for k, v in by_exit.items()},
    )


def directional_accuracy(result, bars, horizon_bars: int = 24) -> dict[str, float | int | None]:
    """Qualité des prévisions directionnelles, indépendamment des trades.

    Pour chaque décision portant un ``bias`` (bullish / bearish), compare au
    rendement de la clôture de décision à la clôture ``horizon_bars`` plus tard.
    Évaluation a posteriori uniquement : n'est jamais visible par la stratégie.
    """
    closes = {b.time: b.bid.close for b in bars}
    times = [b.time for b in bars]
    hits = total = 0
    for d in result.decisions:
        bias = d.decision.bias
        if bias not in ("bullish", "bearish"):
            continue
        i = d.index
        if i + horizon_bars >= len(times):
            continue
        r = closes[times[i + horizon_bars]] - closes[times[i]]
        total += 1
        hits += (r > 0) if bias == "bullish" else (r < 0)
    return {"forecasts": total, "hit_rate": hits / total if total else None, "horizon_bars": horizon_bars}
