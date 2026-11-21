"""Analyse post-trade : ce qui marche, ce qui coûte, ce que les filtres évitent.

Entrée : un ``DecisionJournal`` lu à une date ``asof`` (rien de postérieur).
Sortie : un ``PostTradeReport`` avec des statistiques par groupe et une liste
de **constats** classés. Un constat n'est jamais une action : il peut motiver
une hypothèse à déclarer dans ``EXPERIMENTS.md`` puis à tester en walk-forward
(``improvement.reevaluation``), rien de plus.

Garde-fous statistiques : un groupe n'est commenté qu'à partir de ``min_n``
trades, et une espérance n'est dite « négative » ou « positive » que si son
intervalle de confiance à 95 % exclut zéro. Avec quelques dizaines de trades,
la plupart des écarts sont du bruit : le rapport le dit.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from bot_trading.improvement.journal import DecisionJournal, JournalEntry


@dataclass(frozen=True)
class GroupSummary:
    key: str
    n: int
    expectancy_r: float | None
    ci95_r: tuple[float, float] | None
    win_rate: float | None
    net_pnl_eur: float
    avg_holding_hours: float | None

    @property
    def significant_sign(self) -> int:
        """+1 / −1 si l'IC 95 % exclut zéro, sinon 0."""
        if self.ci95_r is None:
            return 0
        lo, hi = self.ci95_r
        return 1 if lo > 0 else -1 if hi < 0 else 0


@dataclass(frozen=True)
class Finding:
    severity: str  # "alerte" | "à tester" | "info"
    topic: str
    text: str


@dataclass
class PostTradeReport:
    asof: datetime | None
    source: str
    overall: GroupSummary
    groups: dict[str, list[GroupSummary]]
    no_trade_by_stage: dict[str, dict]
    plan_vs_outcome: dict
    costs: dict
    findings: list[Finding] = field(default_factory=list)

    def to_dict(self) -> dict:
        from dataclasses import asdict

        return asdict(self)

    def to_markdown(self) -> str:
        def r(v, nd=2):
            return "—" if v is None else f"{v:+.{nd}f}"

        def pct(v):
            return "—" if v is None else f"{v * 100:.0f} %"

        o = self.overall
        lines = [
            "# Analyse post-trade",
            "",
            f"Source : {self.source}. Connu au : {self.asof.isoformat() if self.asof else 'fin des données'}.",
            "",
            f"**{o.n} trades**, espérance {r(o.expectancy_r)} R"
            + (f" (IC 95 % {o.ci95_r[0]:+.2f} à {o.ci95_r[1]:+.2f})" if o.ci95_r else "")
            + f", taux de gain {pct(o.win_rate)}, P&L net {o.net_pnl_eur:+.2f} €.",
            "",
            "## Constats",
            "",
        ]
        if self.findings:
            lines += [f"- **{f.severity}** ({f.topic}) : {f.text}" for f in self.findings]
        else:
            lines.append("- Aucun constat significatif.")
        for name, rows in self.groups.items():
            if not rows:
                continue
            lines += ["", f"## Par {name}", "", "| Groupe | Trades | Espérance R | IC 95 % | Gain | P&L net € | Durée moy. h |", "|---|---|---|---|---|---|---|"]
            for g in rows:
                ci = f"{g.ci95_r[0]:+.2f} / {g.ci95_r[1]:+.2f}" if g.ci95_r else "—"
                hold = "—" if g.avg_holding_hours is None else f"{g.avg_holding_hours:.0f}"
                lines.append(f"| {g.key} | {g.n} | {r(g.expectancy_r)} | {ci} | {pct(g.win_rate)} | {g.net_pnl_eur:+.2f} | {hold} |")
        if self.no_trade_by_stage:
            lines += ["", "## NO_TRADE par étage (contrefactuel)", "", "| Étage | Décisions | Avec horizon connu | Variation moyenne | Part en hausse |", "|---|---|---|---|---|"]
            for k, v in self.no_trade_by_stage.items():
                mv = "—" if v["mean_forward_move"] is None else f"{v['mean_forward_move'] * 100:+.2f} %"
                lines.append(f"| {k} | {v['count']} | {v['with_forward']} | {mv} | {pct(v['share_up'])} |")
        pv = self.plan_vs_outcome
        lines += [
            "",
            "## Prévu vs réalisé",
            "",
            f"- Potentiel prévu moyen (premier obstacle) : {r(pv.get('mean_room_r'))} R ; réalisé moyen : {r(pv.get('mean_realized_r'))} R.",
            f"- Part des trades sortis au take profit : {pct(pv.get('tp_share'))} ; au stop initial ou en perte : {pct(pv.get('stop_share'))}.",
            f"- Qualité du setup : espérance {r(pv.get('high_quality_r'))} R (moitié haute) contre {r(pv.get('low_quality_r'))} R (moitié basse).",
            "",
            "## Coûts",
            "",
            f"- Financement {self.costs['funding_eur']:.2f} €, frais et primes {self.costs['fees_eur']:.2f} €, soit {pct(self.costs.get('cost_share_of_gross'))} du P&L brut absolu.",
            "",
            "_Constats = hypothèses à déclarer dans EXPERIMENTS.md puis à tester en walk-forward. Aucun paramètre n'est modifié par cette analyse._",
        ]
        return "\n".join(lines)


# ------------------------------------------------------------------ statistiques


def _ci95(xs: list[float]) -> tuple[float, float] | None:
    n = len(xs)
    if n < 3:
        return None
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    # Student approché (n petit) : t ≈ 1,96 × (1 + 2,5/n)
    h = 1.96 * (1 + 2.5 / n) * sd / math.sqrt(n)
    return (m - h, m + h)


def summarize(key: str, entries: list[JournalEntry]) -> GroupSummary:
    trades = [e.trade for e in entries if e.trade is not None]
    rs = [t.r_multiple for t in trades if t.r_multiple is not None]
    n = len(trades)
    return GroupSummary(
        key=key,
        n=n,
        expectancy_r=sum(rs) / len(rs) if rs else None,
        ci95_r=_ci95(rs),
        win_rate=sum(t.net_pnl_eur > 0 for t in trades) / n if n else None,
        net_pnl_eur=sum(t.net_pnl_eur for t in trades),
        avg_holding_hours=sum(t.holding_hours for t in trades) / n if n else None,
    )


def _bucket_quality(e: JournalEntry) -> str:
    q = e.quality
    if q is None:
        return "inconnue"
    return "≥ 0,8" if q >= 0.8 else "0,65–0,8" if q >= 0.65 else "< 0,65"


def _bucket_holding(e: JournalEntry) -> str:
    h = e.trade.holding_hours if e.trade else 0
    return "< 1 j" if h < 24 else "1–3 j" if h < 72 else "3–7 j" if h < 168 else "≥ 7 j"


def _exit_family(reason: str, r_multiple: float | None = None) -> str:
    r = reason.upper()
    if "TAKE" in r or r.startswith("TP"):
        return "take profit"
    if "STOP" in r:
        # Un stop touché en gain est un stop remonté (break-even, suiveur, RISK_OFF).
        return "stop remonté (en gain)" if r_multiple is not None and r_multiple > 0 else "stop"
    if "END_OF" in r:
        return "fin des données"
    if "temporelle" in reason:
        return "sortie temporelle"
    if "Financement" in reason:
        return "financement"
    if "structure" in reason:
        return "rupture de structure"
    if "fondamental" in reason.lower():
        return "invalidation fondamentale"
    return reason[:40] or "?"


GROUPERS: dict[str, Callable[[JournalEntry], str]] = {
    "setup": lambda e: e.setup or "?",
    "régime": lambda e: e.regime or "?",
    "qualité": _bucket_quality,
    "sortie": lambda e: _exit_family(e.trade.exit_reason, e.trade.r_multiple) if e.trade else "?",
    "durée": _bucket_holding,
}


def analyze(journal: DecisionJournal, asof: datetime | None = None, min_n: int = 10) -> PostTradeReport:
    j = journal.as_of(asof)
    trades = j.trades
    overall = summarize("tous", trades)
    groups: dict[str, list[GroupSummary]] = {}
    for name, fn in GROUPERS.items():
        buckets: dict[str, list[JournalEntry]] = defaultdict(list)
        for e in trades:
            buckets[fn(e)].append(e)
        groups[name] = sorted((summarize(k, v) for k, v in buckets.items()), key=lambda g: -g.n)

    # NO_TRADE par étage, avec contrefactuel si disponible.
    nt: dict[str, list[JournalEntry]] = defaultdict(list)
    for e in j.no_trades:
        nt[e.stage or "?"].append(e)
    no_trade_by_stage = {}
    for k, v in sorted(nt.items(), key=lambda kv: -len(kv[1])):
        mv = [e.forward_move for e in v if e.forward_move is not None]
        no_trade_by_stage[k] = {
            "count": len(v),
            "with_forward": len(mv),
            "mean_forward_move": sum(mv) / len(mv) if mv else None,
            "share_up": sum(m > 0 for m in mv) / len(mv) if mv else None,
        }

    # Prévu vs réalisé.
    rooms = [e.room_r for e in trades if e.room_r is not None]
    rs = [e.trade.r_multiple for e in trades if e.trade and e.trade.r_multiple is not None]
    fams = [_exit_family(e.trade.exit_reason, e.trade.r_multiple) for e in trades if e.trade]
    qual = sorted((e for e in trades if e.quality is not None and e.trade and e.trade.r_multiple is not None), key=lambda e: e.quality)
    half = len(qual) // 2
    lo_r = [e.trade.r_multiple for e in qual[:half]]
    hi_r = [e.trade.r_multiple for e in qual[half:]] if half else []
    plan = {
        "mean_room_r": sum(rooms) / len(rooms) if rooms else None,
        "mean_realized_r": sum(rs) / len(rs) if rs else None,
        "tp_share": fams.count("take profit") / len(fams) if fams else None,
        "stop_share": fams.count("stop") / len(fams) if fams else None,
        "low_quality_r": sum(lo_r) / len(lo_r) if lo_r else None,
        "high_quality_r": sum(hi_r) / len(hi_r) if hi_r else None,
        "quality_pairs": len(qual),
    }

    funding = sum(e.trade.funding_eur for e in trades if e.trade)
    fees = sum(e.trade.fees_eur for e in trades if e.trade)
    gross = sum((e.trade.gross_pnl_eur if e.trade.gross_pnl_eur is not None else e.trade.net_pnl_eur + e.trade.funding_eur + e.trade.fees_eur) for e in trades if e.trade)
    costs = {"funding_eur": funding, "fees_eur": fees, "gross_pnl_eur": gross, "cost_share_of_gross": (funding + fees) / abs(gross) if gross else None}

    report = PostTradeReport(asof, j.source, overall, groups, no_trade_by_stage, plan, costs)
    report.findings = _findings(report, min_n)
    return report


def _findings(rep: PostTradeReport, min_n: int) -> list[Finding]:
    out: list[Finding] = []
    o = rep.overall
    if o.n < min_n:
        out.append(Finding("info", "échantillon", f"{o.n} trades seulement (< {min_n}) : aucune conclusion par groupe n'est fiable."))
    elif o.significant_sign < 0:
        out.append(Finding("alerte", "espérance", f"Espérance globale négative et significative ({o.expectancy_r:+.2f} R sur {o.n} trades)."))
    for name, rows in rep.groups.items():
        for g in rows:
            if g.n < min_n or g.key in ("?", "inconnue"):
                continue
            if g.significant_sign < 0:
                out.append(Finding("à tester", name, f"{name} « {g.key} » : espérance {g.expectancy_r:+.2f} R sur {g.n} trades, IC 95 % sous zéro. Hypothèse : le désactiver ou le restreindre."))
            elif g.significant_sign > 0 and o.significant_sign <= 0:
                out.append(Finding("à tester", name, f"{name} « {g.key} » : espérance {g.expectancy_r:+.2f} R sur {g.n} trades, IC 95 % au-dessus de zéro alors que l'ensemble ne l'est pas."))
    pv = rep.plan_vs_outcome
    if pv["quality_pairs"] >= 2 * min_n and pv["low_quality_r"] is not None and pv["high_quality_r"] is not None:
        if pv["high_quality_r"] < pv["low_quality_r"]:
            out.append(Finding("à tester", "qualité", f"Les setups les mieux notés font moins bien ({pv['high_quality_r']:+.2f} R) que les moins bien notés ({pv['low_quality_r']:+.2f} R) : la note de qualité est à recalibrer."))
    if pv["mean_room_r"] is not None and pv["mean_realized_r"] is not None and o.n >= min_n and pv["mean_realized_r"] < 0.25 * pv["mean_room_r"]:
        out.append(Finding("info", "prévu vs réalisé", f"Réalisé moyen {pv['mean_realized_r']:+.2f} R pour un potentiel prévu de {pv['mean_room_r']:.2f} R."))
    cs = rep.costs.get("cost_share_of_gross")
    if cs is not None and cs > 0.25 and o.n >= 3:
        out.append(Finding("alerte", "coûts", f"Financement et frais = {cs * 100:.0f} % du P&L brut (R22) : durée de détention ou taille de position à revoir."))
    for stage, v in rep.no_trade_by_stage.items():
        if v["with_forward"] >= 5 * min_n and v["share_up"] is not None and v["share_up"] >= 0.6 and (v["mean_forward_move"] or 0) > 0:
            out.append(Finding("info", "filtres", f"Étage « {stage} » : après {v['with_forward']} refus, le prix a monté dans {v['share_up'] * 100:.0f} % des cas. Ne prouve pas qu'un trade aurait gagné (stop, coûts), mais mérite un test A/B."))
    order = {"alerte": 0, "à tester": 1, "info": 2}
    return sorted(out, key=lambda f: order.get(f.severity, 3))
