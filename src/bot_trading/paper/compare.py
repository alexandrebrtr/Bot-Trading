"""Comparaison paper trading / trades manuels d'Alexandre (ROADMAP, phase 4).

Sources des trades manuels (données personnelles, jamais committées) :

- le journal ``TJ_*.xlsx`` (onglet « Trades », mêmes colonnes que
  ``research/journal_analysis.py``) : seuls les trades **Live / SP500**, c'est-à-dire
  le CFD US500 de Capital.com, sont retenus ; lecture par ``openpyxl`` s'il est installé ;
- ou un CSV simple ``date,direction,entry_price,exit_price,pnl`` (UTF-8, en-tête obligatoire).

Le rapport compare, sur la période commune : nombre de trades, taux de réussite,
P&L (le bot en €, le journal dans sa devise : pas de conversion implicite), et
la concordance des dates d'entrée (un trade manuel et un trade du bot à moins
de ``window`` l'un de l'autre). Il ne modifie rien.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True)
class ManualTrade:
    opened: datetime
    direction: str  # LONG / SHORT
    entry_price: float | None
    exit_price: float | None
    pnl: float | None  # devise du journal


def _to_dt(v) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, date):
        return datetime.combine(v, time(0), tzinfo=timezone.utc)
    try:
        t = datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            t = datetime.strptime(str(v).strip()[:10], "%d/%m/%Y")
        except ValueError:
            return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _num(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", ".").replace(" ", "")) if not isinstance(v, (int, float)) else float(v)
    except ValueError:
        return None


def load_manual_csv(path: str | Path) -> list[ManualTrade]:
    out = []
    with open(path, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            t = _to_dt(r.get("date"))
            if t is None:
                continue
            out.append(ManualTrade(t, str(r.get("direction") or "LONG").upper(), _num(r.get("entry_price")), _num(r.get("exit_price")), _num(r.get("pnl"))))
    return sorted(out, key=lambda m: m.opened)


def load_manual_xlsx(path: str | Path, account: str = "Live", symbol: str = "SP500") -> list[ManualTrade]:
    try:
        import openpyxl
    except ImportError as e:  # dépendance facultative
        raise RuntimeError("Lecture du journal .xlsx : pip install openpyxl (ou exporter en CSV)") from e
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb["Trades"]
    rows = ws.iter_rows(values_only=True)
    header = [str(h or "").strip() for h in next(rows)]
    idx = {h: i for i, h in enumerate(header)}
    date_col = "Date" if "Date" in idx else "-"
    need = (date_col, "Type Compte", "Symbole", "Direction", "Prix Entrée")
    missing = [c for c in need if c not in idx]
    if missing:
        raise RuntimeError(f"Colonnes absentes de l'onglet Trades : {', '.join(missing)}")

    def get(row, col):
        i = idx.get(col)
        return row[i] if i is not None and i < len(row) else None

    out = []
    for row in rows:
        if get(row, "Type Compte") != account or get(row, "Symbole") != symbol:
            continue
        if get(row, "Taille Position") in (None, "") or get(row, "Prix Entrée") in (None, ""):
            continue  # dividendes, dépôts, retraits
        t = _to_dt(get(row, date_col))
        if t is None:
            continue
        out.append(ManualTrade(t, str(get(row, "Direction") or "").upper(), _num(get(row, "Prix Entrée")), _num(get(row, "Prix Sortie")), _num(get(row, "P&L Net ($)"))))
    return sorted(out, key=lambda m: m.opened)


def load_manual(path: str | Path) -> list[ManualTrade]:
    p = Path(path)
    return load_manual_xlsx(p) if p.suffix.lower() in (".xlsx", ".xlsm") else load_manual_csv(p)


def _stats(pnls: Sequence[float]) -> dict:
    n = len(pnls)
    wins = [x for x in pnls if x > 0]
    losses = [-x for x in pnls if x < 0]
    return {
        "trades": n,
        "win_rate": round(len(wins) / n, 4) if n else None,
        "pnl": round(sum(pnls), 4),
        "profit_factor": round(sum(wins) / sum(losses), 4) if losses else None,
    }


def compare(paper_trades: Sequence[dict], manual: Sequence[ManualTrade], start: datetime | None = None, end: datetime | None = None, window: timedelta = timedelta(days=1)) -> dict:
    """Comparaison sur [start ; end] (par défaut : période couverte par le paper trading)."""
    opened = [(_to_dt(t.get("opened_at")), t) for t in paper_trades]
    opened = [(o, t) for o, t in opened if o is not None]
    if start is None:
        start = min((o for o, _ in opened), default=None)
    if end is None:
        end = max((_to_dt(t.get("closed_at")) or o for o, t in opened), default=None)
    if start is None or end is None:
        return {"period": None, "paper": _stats([]), "manual": _stats([]), "matched": 0, "note": "aucun trade paper : rien à comparer"}
    bot = [(o, t) for o, t in opened if start <= o <= end]
    man = [m for m in manual if start - window <= m.opened <= end + window]
    used: set[int] = set()
    matched = []
    for o, t in bot:
        j = next((i for i, m in enumerate(man) if i not in used and abs(m.opened - o) <= window), None)
        if j is not None:
            used.add(j)
            matched.append({"paper_opened": o.isoformat(), "manual_opened": man[j].opened.isoformat(), "manual_direction": man[j].direction,
                            "paper_pnl_eur": t.get("net_pnl_eur"), "manual_pnl": man[j].pnl})
    return {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "paper": {**_stats([float(t.get("net_pnl_eur") or 0.0) for _, t in bot]), "currency": "EUR"},
        "manual": {**_stats([m.pnl for m in man if m.pnl is not None]), "currency": "journal ($)", "short_trades": sum(1 for m in man if m.direction == "SHORT")},
        "matched": len(matched),
        "matched_trades": matched,
        "paper_only": len(bot) - len(matched),
        "manual_only": len(man) - len(used),
        "window_hours": window.total_seconds() / 3600,
    }


def to_markdown(rep: dict) -> str:
    if rep.get("period") is None:
        return rep.get("note", "Rien à comparer.")
    p, m = rep["paper"], rep["manual"]

    def pct(x):
        return "—" if x is None else f"{x:.0%}"

    lines = [
        f"Période : {rep['period']['start'][:10]} → {rep['period']['end'][:10]}",
        "",
        "| | Bot (paper) | Manuel (journal) |",
        "|---|---|---|",
        f"| Trades | {p['trades']} | {m['trades']} |",
        f"| Taux de réussite | {pct(p['win_rate'])} | {pct(m['win_rate'])} |",
        f"| P&L | {p['pnl']:+.2f} € | {m['pnl']:+.2f} ({m['currency']}) |",
        f"| Profit factor | {p['profit_factor'] if p['profit_factor'] is not None else '—'} | {m['profit_factor'] if m['profit_factor'] is not None else '—'} |",
        "",
        f"Entrées concordantes (± {rep['window_hours']:g} h) : {rep['matched']} ; bot seul : {rep['paper_only']} ; manuel seul : {rep['manual_only']}"
        + (f" (dont {m['short_trades']} ventes, que le bot ne fait jamais)" if m.get("short_trades") else ""),
    ]
    return "\n".join(lines)


def write_report(runtime_dir: str | Path, rep: dict) -> Path:
    from bot_trading.dashboard.runtime import write_json

    path = Path(runtime_dir) / "manual_comparison.json"
    write_json(path, {**rep, "generated_at": datetime.now(timezone.utc)})
    return path


def paper_trades(runtime_dir: str | Path) -> list[dict]:
    p = Path(runtime_dir) / "trades.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out
