"""Calculs d'affichage du dashboard : purs, sans Streamlit, testables."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from bot_trading.backtest.metrics import max_drawdown
from bot_trading.config import RiskLimits
from bot_trading.dashboard.runtime import AccountSnapshot, parse_time


@dataclass(frozen=True)
class PortfolioSummary:
    equity_eur: float
    initial_capital_eur: float
    net_pnl_eur: float
    net_return: float
    peak_equity_eur: float
    drawdown: float  # drawdown courant depuis le plus haut (0..1)
    max_drawdown: float  # sur la courbe d'equity disponible
    drawdown_floor_eur: float  # equity sous laquelle le Risk Engine verrouille
    distance_to_floor_eur: float
    used_margin_eur: float
    free_margin_eur: float
    open_exposure_lots: float
    exposure_capacity_lots: float


def portfolio_summary(account: AccountSnapshot, curve: Sequence[tuple[datetime, float]], limits: RiskLimits) -> PortfolioSummary:
    initial = limits.initial_capital_eur
    peak = max([account.peak_equity_eur, account.equity_eur, initial] + [v for _, v in curve])
    floor = peak * (1.0 - limits.max_account_drawdown)
    dd = 0.0 if peak <= 0 else max(0.0, 1.0 - account.equity_eur / peak)
    mdd = max_drawdown(list(curve))[0] if curve else dd
    return PortfolioSummary(
        equity_eur=account.equity_eur,
        initial_capital_eur=initial,
        net_pnl_eur=account.equity_eur - initial,
        net_return=(account.equity_eur / initial - 1.0) if initial else 0.0,
        peak_equity_eur=peak,
        drawdown=dd,
        max_drawdown=max(mdd, dd),
        drawdown_floor_eur=floor,
        distance_to_floor_eur=account.equity_eur - floor,
        used_margin_eur=account.used_margin_eur,
        free_margin_eur=account.equity_eur - account.used_margin_eur,
        open_exposure_lots=account.open_exposure_lots,
        exposure_capacity_lots=max(0.0, limits.max_total_exposure_lots - account.open_exposure_lots),
    )


def drawdown_series(curve: Sequence[tuple[datetime, float]]) -> list[tuple[datetime, float]]:
    """Drawdown (fraction négative ou nulle) à chaque point de la courbe."""
    out: list[tuple[datetime, float]] = []
    peak = float("-inf")
    for t, v in curve:
        peak = max(peak, v)
        out.append((t, (v / peak - 1.0) if peak > 0 else 0.0))
    return out


@dataclass(frozen=True)
class TradeStats:
    trades: int
    net_pnl_eur: float
    win_rate: float | None
    profit_factor: float | None
    expectancy_eur: float | None
    avg_r: float | None
    funding_eur: float
    fees_eur: float


def _f(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def trade_pnl(t: dict) -> float | None:
    """P&L net d'une ligne de ``trades.jsonl`` (``net_pnl_eur`` ou brut − financement − frais)."""
    v = _f(t.get("net_pnl_eur"))
    if v is not None:
        return v
    g = _f(t.get("gross_pnl_eur"))
    if g is None:
        return None
    return g - (_f(t.get("funding_eur")) or 0.0) - (_f(t.get("fees_eur")) or 0.0)


def trade_stats(trades: Sequence[dict]) -> TradeStats:
    pnls = [p for p in (trade_pnl(t) for t in trades) if p is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    rs = [r for r in (_f(t.get("r_multiple")) for t in trades) if r is not None]
    gross_loss = -sum(losses)
    return TradeStats(
        trades=len(pnls),
        net_pnl_eur=sum(pnls),
        win_rate=len(wins) / len(pnls) if pnls else None,
        profit_factor=(sum(wins) / gross_loss) if gross_loss > 0 else None,
        expectancy_eur=sum(pnls) / len(pnls) if pnls else None,
        avg_r=sum(rs) / len(rs) if rs else None,
        funding_eur=sum(_f(t.get("funding_eur")) or 0.0 for t in trades),
        fees_eur=sum(_f(t.get("fees_eur")) or 0.0 for t in trades),
    )


# ------------------------------------------------------------------ santé


class Status:
    OK = "OK"
    STALE = "EN RETARD"
    DOWN = "ARRÊTÉ"
    ERROR = "ERREUR"
    MISSING = "AUCUNE DONNÉE"


def heartbeat_status(hb: dict, now: datetime, stale_after: timedelta = timedelta(minutes=15), down_after: timedelta = timedelta(hours=2)) -> tuple[str, timedelta | None]:
    t = parse_time(hb.get("time"))
    if t is None:
        return Status.MISSING, None
    age = now - t
    declared = str(hb.get("status", "")).upper()
    if declared in {"ERROR", "ERREUR", "FAILED"}:
        return Status.ERROR, age
    if age > down_after:
        return Status.DOWN, age
    if age > stale_after:
        return Status.STALE, age
    return Status.OK, age


def account_freshness(account: AccountSnapshot | None, now: datetime, stale_after: timedelta = timedelta(minutes=15)) -> str:
    if account is None or account.updated_at is None:
        return Status.MISSING
    return Status.STALE if now - account.updated_at > stale_after else Status.OK


def fmt_age(age: timedelta | None) -> str:
    if age is None:
        return "—"
    s = int(age.total_seconds())
    if s < 0:
        return "dans le futur"
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        return f"{s // 3600} h {(s % 3600) // 60:02d}"
    return f"{s // 86400} j {(s % 86400) // 3600} h"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
