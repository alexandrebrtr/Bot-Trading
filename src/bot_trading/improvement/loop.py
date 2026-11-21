"""Un cycle d'amélioration : journal → analyse post-trade → ré-évaluation → rapport.

Le cycle produit des fichiers et une proposition ; il ne modifie ni le code,
ni la configuration, ni les limites de risque. Chaque cycle est daté par
``asof`` et ne lit que ce qui était connu à cette date.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from bot_trading.improvement.journal import DecisionJournal, attach_forward_moves
from bot_trading.improvement.post_trade import PostTradeReport, analyze
from bot_trading.improvement.reevaluation import Criteria, ParameterProposal, reevaluate
from bot_trading.improvement.registry import ExperimentRegistry


@dataclass
class CycleResult:
    asof: datetime
    report: PostTradeReport
    proposal: ParameterProposal | None
    report_path: Path | None = None

    def to_markdown(self) -> str:
        md = [self.report.to_markdown()]
        p = self.proposal
        if p is not None:
            md += [
                "",
                "# Ré-évaluation walk-forward",
                "",
                f"Proposition `{p.id}` : **{p.status}** (jamais appliquée automatiquement).",
                "",
                "Changements : " + ("; ".join(f"`{k}` {a!r} → {b!r}" for k, (a, b) in p.changes.items()) or "aucun"),
                "",
                f"Fenêtres : {p.folds}, essais : {p.n_trials} (cumulés {p.cumulative_trials}), Sharpe déflaté : "
                + ("—" if p.deflated_sharpe is None else f"{p.deflated_sharpe:.2f}"),
                "",
                "| Hors échantillon | Référence (actuel) | Procédure candidate |",
                "|---|---|---|",
            ]
            for k in ("trades", "expectancy_r", "profit_factor", "max_drawdown", "net_pnl_eur"):
                b, c = p.baseline_oos.get(k), p.candidate_oos.get(k)
                fmt = (lambda v: "—" if v is None else (f"{v:.2f}" if isinstance(v, float) else str(v)))
                md.append(f"| {k} | {fmt(b)} | {fmt(c)} |")
            md += ["", "Critères :", ""] + [f"- {'✅' if ok else '❌'} {k}" for k, ok in p.checks.items()]
            if p.holdout:
                md.append(f"\nRéserve finale non utilisée : {p.holdout[0]:%Y-%m-%d} → {p.holdout[1]:%Y-%m-%d}.")
        return "\n".join(md)


def run_cycle(
    journal: DecisionJournal,
    asof: datetime,
    settings,
    bars: Sequence | None = None,
    pit=None,
    current: dict | None = None,
    grid: dict | None = None,
    registry: ExperimentRegistry | None = None,
    out_dir: str | Path | None = None,
    forward_horizon: timedelta = timedelta(days=3),
    min_n: int = 10,
    criteria: Criteria | None = None,
    **wf_kw,
) -> CycleResult:
    j = attach_forward_moves(journal, [b for b in bars if b.time < asof], forward_horizon) if bars else journal
    report = analyze(j, asof=asof, min_n=min_n)
    proposal = None
    if grid and bars:
        prior = registry.total_trials() if registry else 0
        proposal = reevaluate(current or {}, grid, bars, asof, settings, pit, prior_trials=prior, criteria=criteria, **wf_kw)
        if registry:
            registry.record(proposal)
    res = CycleResult(asof, report, proposal)
    if out_dir:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        res.report_path = out / f"improvement_{asof:%Y%m%d}.md"
        res.report_path.write_text(res.to_markdown(), encoding="utf-8")
    return res
