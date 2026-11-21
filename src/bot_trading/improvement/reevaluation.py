"""Ré-évaluation walk-forward des paramètres : **propose**, n'applique jamais.

Boucle (ROADMAP Phase 5, AGENTS.md « Agent d'amélioration ») :

1. Ne garder que les bougies **closes avant** ``asof`` : la ré-évaluation d'une
   date donnée ne voit rien de postérieur (les données point-in-time sont déjà
   filtrées par ``PointInTimeView``).
2. Mettre de côté la réserve finale (``holdout_months`` derniers mois avant
   ``asof``) : elle n'est jamais utilisée ici.
3. Mesurer hors échantillon les paramètres **actuels** sur chaque fenêtre de test.
4. Lancer le walk-forward de la grille candidate (choix sur la fenêtre
   d'entraînement, mesure sur la fenêtre suivante) : c'est la performance de la
   *procédure* de choix, pas d'une combinaison choisie après coup.
5. Choisir la combinaison candidate sur la dernière fenêtre d'entraînement.
6. Appliquer les critères (gain OOS sur la référence, trades, PF, drawdown,
   Sharpe déflaté avec le nombre **cumulé** d'essais, stabilité par fenêtre).
7. Produire une ``ParameterProposal`` : statut « en attente de revue » si tous
   les critères passent, « rejeté » sinon. Dans tous les cas ``applied=False``.

Ce qui ne peut pas être proposé : les limites du Risk Engine (``RiskLimits``),
les paliers de drawdown, le plafond de marge. Une grille qui les contient est
refusée avant tout calcul (``ProtectedParameterError``). Le risque de base par
trade (R9) peut être proposé dans sa plage 2–20 %, signalé comme tel : sa
modification exige, comme toute autre, une décision humaine écrite.

Promotion d'une proposition (hors de ce module) : paper trading, puis
validation d'Alexandre, puis modification manuelle et revue de code.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta, timezone

from bot_trading.backtest.engine import Backtester, BacktestConfig
from bot_trading.backtest.metrics import compute_metrics, daily_returns, deflated_sharpe
from bot_trading.backtest.validation import Fold, add_months, default_objective, param_grid, slice_bars, walk_forward, walk_forward_folds
from bot_trading.config import RiskLimits
from bot_trading.marketdata.source import PointInTimeSource
from bot_trading.strategy.decision_engine import DecisionParams, DecisionStrategy
from bot_trading.strategy.positions import ExitParams
from bot_trading.strategy.technical import TechnicalParams

PENDING = "en attente de revue"
REJECTED = "rejeté"


class ProtectedParameterError(ValueError):
    """Tentative de faire varier une limite de risque."""


# Paramètres jamais optimisables automatiquement (AGENTS.md interdit 4, RISK_POLICY).
PROTECTED = frozenset({f.name for f in fields(RiskLimits)}) | frozenset({
    "tiers", "half_risk_at", "max_quality_only_at", "max_quality_threshold",  # R17
    "max_margin_fraction",  # R19
    "max_open_risk_fraction",
    "settings", "risk", "limits",
})

# Paramètres 🎛️ autorisés et leur plage (STRATEGY.md, EXPERIMENTS.md).
# préfixe : t. = TechnicalParams, d. = DecisionParams, x. = ExitParams
SEARCH_SPACE: dict[str, tuple] = {
    "t.entry_mode": ("touch", "half", "full_reject", "bos"),  # EXP-03
    "t.fvg_min_size_atr": (0.1, 1.0),  # G4 / EXP-05
    "t.ob_min_displacement_atr": (0.5, 2.5),  # EXP-06
    "t.ob_max_retests": (0, 3),
    "t.max_extension_atr": (1.0, 4.0),  # T3
    "t.d1_trend_ema": (50, 200),  # T1
    "t.stop_buffer_atr": (0.1, 1.0),  # E4
    "t.confirm_min_wick_ratio": (0.0, 0.5),
    "d.take_profit": ("none", "fixed_r", "obstacle"),  # X1 / EXP-07
    "d.take_profit_r": (1.0, 4.0),
    "d.max_spread_multiple": (1.5, 3.0),  # E5
    "d.base_risk_fraction": (0.02, 0.20),  # R9 / EXP-08 — signalé « risque »
    "x.breakeven_at_r": (0.5, 2.0),  # X2
    "x.max_days": (2.0, 15.0),  # X3
    "x.max_days_min_r": (0.0, 1.0),
    "x.on_risk_off": ("tighten", "ignore", "close"),  # Q2 (défaut validé : tighten)
}
RISK_SENSITIVE = frozenset({"d.base_risk_fraction"})


def validate_grid(grid: dict[str, Sequence]) -> None:
    """Refuse toute limite de risque et toute valeur hors de sa plage 🎛️."""
    for key, values in grid.items():
        name = key.split(".", 1)[-1]
        if key in PROTECTED or name in PROTECTED:
            raise ProtectedParameterError(f"{key} est une limite de risque : non optimisable (AGENTS.md, interdit 4)")
        if key not in SEARCH_SPACE:
            raise ValueError(f"{key} hors de l'espace de recherche déclaré (SEARCH_SPACE)")
        space = SEARCH_SPACE[key]
        is_range = len(space) == 2 and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in space)
        for v in values:
            if is_range:
                if v is None and key == "x.breakeven_at_r":
                    continue
                if not isinstance(v, (int, float)) or not space[0] <= v <= space[1]:
                    raise ValueError(f"{key}={v!r} hors de la plage {space}")
            elif v not in space:
                raise ValueError(f"{key}={v!r} n'est pas parmi {space}")


def split_params(flat: dict) -> tuple[dict, dict, dict]:
    t, d, x = {}, {}, {}
    for k, v in flat.items():
        pre, name = k.split(".", 1)
        {"t": t, "d": d, "x": x}[pre][name] = v
    return t, d, x


def default_values(keys) -> dict:
    """Valeurs par défaut actuelles du code pour des clés ``t.``/``d.``/``x.``."""
    objs = {"t": TechnicalParams(), "d": DecisionParams(), "x": ExitParams()}
    out = {}
    for k in keys:
        pre, name = k.split(".", 1)
        out[k] = getattr(objs[pre], name)
    return out


def decision_factory(settings, base: dict | None = None) -> Callable[[dict], DecisionStrategy]:
    """Fabrique de ``DecisionStrategy`` à partir de paramètres « plats » (``t.``, ``d.``, ``x.``)."""
    base = dict(base or {})

    def make(params: dict) -> DecisionStrategy:
        t, d, x = split_params({**base, **params})
        return DecisionStrategy(settings, technical=TechnicalParams(**t), decision=DecisionParams(**d), exits=ExitParams(**x))

    return make


# ------------------------------------------------------------------ résultat


@dataclass(frozen=True)
class Criteria:
    min_oos_trades: int = 30  # ROADMAP Phase 3
    min_profit_factor: float = 1.3
    max_drawdown: float = 0.35
    min_expectancy_gain_r: float = 0.05  # gain minimal sur la référence, en R par trade
    min_deflated_sharpe: float = 0.95
    min_fold_win_share: float = 0.5  # part des fenêtres où la procédure bat la référence


@dataclass
class ParameterProposal:
    id: str
    created_at: datetime
    asof: datetime
    current: dict
    proposed: dict
    changes: dict  # clé → (actuel, proposé)
    baseline_oos: dict
    candidate_oos: dict
    in_sample: dict
    folds: int
    n_trials: int  # essais de cette ré-évaluation
    cumulative_trials: int  # essais cumulés (Sharpe déflaté)
    deflated_sharpe: float | None
    checks: dict[str, bool]
    reasons: list[str]
    status: str
    risk_sensitive: bool
    applied: bool = False  # toujours False : ce module n'applique rien
    holdout: tuple[datetime, datetime] | None = None
    params_by_fold: list[dict] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return self.status == PENDING

    def to_experiment(self) -> dict:
        """Ligne ``experiments.jsonl`` (format lu par l'onglet Amélioration du dashboard)."""
        return {
            "time": self.created_at,
            "id": self.id,
            "loop": "ré-évaluation walk-forward",
            "hypothesis": "; ".join(f"{k} {a!r} → {b!r}" for k, (a, b) in self.changes.items()) or "paramètres actuels reconduits",
            "params": self.proposed,
            "current": self.current,
            "asof": self.asof,
            "in_sample": self.in_sample,
            "out_of_sample": self.candidate_oos,
            "baseline_out_of_sample": self.baseline_oos,
            "n_trials": self.n_trials,
            "cumulative_trials": self.cumulative_trials,
            "deflated_sharpe": self.deflated_sharpe,
            "checks": self.checks,
            "status": self.status,
            "note": " ; ".join(self.reasons) + (" ; touche au risque par trade (R9)" if self.risk_sensitive else ""),
            "applied": False,
            "holdout": list(self.holdout) if self.holdout else None,
        }


def _oos_summary(results) -> dict:
    trades = [t for r in results for t in r.trades]
    rs = [t.r_multiple for t in trades if t.r_multiple is not None]
    wins = sum(t.net_pnl_eur for t in trades if t.net_pnl_eur > 0)
    losses = -sum(t.net_pnl_eur for t in trades if t.net_pnl_eur <= 0)
    ms = [compute_metrics(r) for r in results]
    return {
        "trades": len(trades),
        "expectancy_r": sum(rs) / len(rs) if rs else None,
        "profit_factor": wins / losses if losses > 0 else None,
        "net_pnl_eur": sum(t.net_pnl_eur for t in trades),
        "max_drawdown": max((m.max_drawdown for m in ms), default=None),
        "calmar": _mean_or_none([m.calmar for m in ms if m.calmar is not None]),
        "by_fold_expectancy_r": [m.expectancy_r for m in ms],
    }


def _mean_or_none(xs):
    return sum(xs) / len(xs) if xs else None


def _jsonable(v):
    return v if isinstance(v, (str, int, float, bool, type(None))) else str(v)


def reevaluate(
    current: dict,
    grid: dict[str, Sequence],
    bars: Sequence,
    asof: datetime,
    settings,
    pit: PointInTimeSource | None = None,
    config: BacktestConfig | None = None,
    train_months: int = 24,
    test_months: int = 6,
    holdout_months: int = 12,
    warmup_bars: int = 300,
    prior_trials: int = 0,
    criteria: Criteria | None = None,
    objective=None,
) -> ParameterProposal:
    """Ré-évalue ``grid`` contre ``current`` avec les seules données connues à ``asof``."""
    validate_grid(grid)
    validate_grid({k: [v] for k, v in current.items()})
    current = {**default_values(grid), **current}
    crit = criteria or Criteria()
    config = config or BacktestConfig(settings=settings, max_margin_fraction=0.5)
    step = bars[0].timeframe.minutes if bars else 0
    known = [b for b in bars if b.time + timedelta(minutes=step) <= asof]
    if not known:
        raise ValueError("Aucune bougie close avant asof")
    folds, holdout = walk_forward_folds(known[0].time, asof, train_months, test_months, test_months, holdout_months)
    if not folds:
        raise ValueError("Historique trop court pour une fenêtre walk-forward avant asof (hors réserve)")
    factory = decision_factory(settings, current)
    combos_grid = {k: list(v) for k, v in grid.items()}
    obj = objective or default_objective

    # 3. Référence : paramètres actuels, hors échantillon, fenêtre par fenêtre.
    bt = Backtester(config, pit)
    base_res = []
    for f in folds:
        data, ts = slice_bars(known, f.test_start, f.test_end, warmup_bars)
        base_res.append(bt.run(factory({}), data, trade_start=ts))
    baseline = _oos_summary(base_res)

    # 4. Procédure de choix en walk-forward.
    rep = walk_forward(factory, combos_grid, known, folds, config, pit, warmup_bars, obj)
    cand_res = [f.oos_result for f in rep.folds]
    candidate = _oos_summary(cand_res)

    # 5. Choix final sur la dernière fenêtre d'entraînement (avant la réserve).
    last_train_end = holdout[0] if holdout else asof
    final_fold = Fold(add_months(last_train_end, -train_months), last_train_end, last_train_end, last_train_end)
    train, ts = slice_bars(known, final_fold.train_start, final_fold.train_end, warmup_bars)
    best: tuple[float, dict, object] | None = None
    combos = param_grid(combos_grid) or [{}]
    for params in combos:
        m = compute_metrics(bt.run(factory(params), train, trade_start=ts))
        s = obj(m)
        if best is None or s > best[0]:
            best = (s, params, m)
    assert best is not None
    chosen = best[1]
    n_trials = len(combos) * (len(folds) + 1)
    cumulative = prior_trials + n_trials

    # 6. Critères.
    rets: list[float] = []
    for r in cand_res:
        rets += daily_returns(r.equity_curve, r.initial_equity_eur)
    dsr = deflated_sharpe(rets, cumulative)
    be, ce = baseline["expectancy_r"], candidate["expectancy_r"]
    fold_wins = sum(
        1 for b, c in zip(baseline["by_fold_expectancy_r"], candidate["by_fold_expectancy_r"]) if c is not None and (b is None or c > b)
    )
    pf = candidate["profit_factor"]
    dd = candidate["max_drawdown"]
    proposed = {**current, **chosen}
    changes = {k: (_jsonable(current.get(k)), _jsonable(v)) for k, v in proposed.items() if current.get(k) != v}
    checks = {
        "changement proposé": bool(changes),
        f"≥ {crit.min_oos_trades} trades OOS": candidate["trades"] >= crit.min_oos_trades,
        "espérance OOS > 0": ce is not None and ce > 0,
        f"gain ≥ {crit.min_expectancy_gain_r:g} R sur la référence": ce is not None and (be is None or ce - be >= crit.min_expectancy_gain_r),
        f"PF ≥ {crit.min_profit_factor:g}": pf is not None and pf >= crit.min_profit_factor,
        f"DD < {crit.max_drawdown:.0%}": dd is not None and dd < crit.max_drawdown,
        f"Sharpe déflaté ≥ {crit.min_deflated_sharpe:g}": dsr is not None and dsr >= crit.min_deflated_sharpe,
        "stabilité par fenêtre": len(folds) > 0 and fold_wins / len(folds) >= crit.min_fold_win_share,
    }
    reasons = [f"échec : {k}" for k, ok in checks.items() if not ok]
    status = PENDING if all(checks.values()) else REJECTED
    if status == PENDING:
        reasons = ["tous les critères passent : paper trading puis validation d'Alexandre avant toute modification"]
    return ParameterProposal(
        id=f"WF-{asof:%Y%m%d}-{uuid.uuid4().hex[:6]}",
        created_at=datetime.now(timezone.utc),
        asof=asof,
        current={k: _jsonable(v) for k, v in current.items()},
        proposed={k: _jsonable(v) for k, v in proposed.items()},
        changes=changes,
        baseline_oos=baseline,
        candidate_oos=candidate,
        in_sample={"objective": best[0] if math.isfinite(best[0]) else None, "trades": best[2].trades, "calmar": best[2].calmar, "expectancy_r": best[2].expectancy_r},
        folds=len(folds),
        n_trials=n_trials,
        cumulative_trials=cumulative,
        deflated_sharpe=dsr,
        checks=checks,
        reasons=reasons,
        status=status,
        risk_sensitive=any(k in RISK_SENSITIVE for k in changes),
        holdout=holdout,
        params_by_fold=[{k: _jsonable(v) for k, v in f.best_params.items()} for f in rep.folds],
    )
