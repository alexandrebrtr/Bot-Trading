"""Journal décisions → résultats (Phase 5, première boucle d'amélioration).

Chaque décision (y compris les ``NO_TRADE`` et leur étage de refus) est reliée
à ce qui s'est passé ensuite :

- une décision ``LONG`` exécutée est reliée au trade qu'elle a ouvert
  (R réalisé, P&L net, raison de sortie, durée) ;
- une décision ``NO_TRADE`` peut recevoir un **contrefactuel** : la variation
  du prix sur un horizon fixe après la décision (``attach_forward_moves``),
  pour savoir si un filtre bloque surtout des pertes ou surtout des gains.

Règle anti look-ahead : toute lecture se fait « à la date ``asof`` ». Un
résultat n'est visible que s'il est **connu** à ``asof`` (trade clôturé avant
``asof``, horizon du contrefactuel terminé avant ``asof``). Le journal sert à
apprendre du passé, jamais à corriger une décision avec une information
qu'elle ne pouvait pas avoir.

Deux sources :
- ``from_backtest(result)`` : un ``BacktestResult`` (backtest ou walk-forward) ;
- ``from_runtime(dir)`` : ``decisions.jsonl`` et ``trades.jsonl`` écrits par la
  boucle de paper trading (format de ``dashboard.runtime``).
"""

from __future__ import annotations

import bisect
import json
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path

from bot_trading.dashboard.runtime import dumps, parse_time


@dataclass(frozen=True)
class TradeOutcome:
    deal_id: str
    opened_at: datetime
    closed_at: datetime
    entry_price: float
    exit_price: float
    net_pnl_eur: float
    r_multiple: float | None
    exit_reason: str
    funding_eur: float = 0.0
    fees_eur: float = 0.0
    gross_pnl_eur: float | None = None
    initial_risk_eur: float | None = None

    @property
    def holding_hours(self) -> float:
        return (self.closed_at - self.opened_at).total_seconds() / 3600


@dataclass(frozen=True)
class JournalEntry:
    time: datetime  # instant de décision (clôture de bougie)
    action: str  # LONG / HOLD / CLOSE / NO_TRADE
    stage: str = ""  # étage du Decision Engine (fondamental, D1, H4, setup…)
    outcome: str = ""  # FILLED, VETO: …, NO_TRADE…
    reason: str = ""
    setup: str = ""
    regime: str = ""
    bias: str = ""
    quality: float | None = None
    confidence: float | None = None
    price: float | None = None
    stop_level: float | None = None
    take_profit_level: float | None = None
    room_r: float | None = None  # potentiel prévu jusqu'au premier obstacle
    trade: TradeOutcome | None = None
    # Contrefactuel : variation relative du prix sur l'horizon, et fin de l'horizon.
    forward_move: float | None = None
    forward_known_at: datetime | None = None

    @property
    def filled(self) -> bool:
        return self.action == "LONG" and self.trade is not None


@dataclass
class DecisionJournal:
    entries: list[JournalEntry] = field(default_factory=list)
    source: str = ""

    # ------------------------------------------------------------ lecture causale
    def as_of(self, asof: datetime | None) -> "DecisionJournal":
        """Ce qui est connu à ``asof`` : décisions passées, trades clos, horizons terminés."""
        if asof is None:
            return self
        out = []
        for e in self.entries:
            if e.time > asof:
                continue
            if e.trade is not None and e.trade.closed_at > asof:
                e = replace(e, trade=None, outcome=e.outcome or "FILLED")
            if e.forward_known_at is not None and e.forward_known_at > asof:
                e = replace(e, forward_move=None, forward_known_at=None)
            out.append(e)
        return DecisionJournal(out, self.source)

    @property
    def trades(self) -> list[JournalEntry]:
        return [e for e in self.entries if e.filled]

    @property
    def no_trades(self) -> list[JournalEntry]:
        return [e for e in self.entries if e.action == "NO_TRADE"]

    def to_records(self) -> list[dict]:
        return [asdict(e) for e in self.entries]

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            for r in self.to_records():
                f.write(dumps(r) + "\n")
        return path


# ------------------------------------------------------------------ sources


def from_backtest(result) -> DecisionJournal:
    """Journal depuis un ``BacktestResult`` : les trades sont reliés par l'heure de décision."""
    by_decision: dict[datetime, list] = {}
    for t in result.trades:
        if t.decided_at is not None:
            by_decision.setdefault(t.decided_at, []).append(t)
    entries = []
    for rec in result.decisions:
        d = rec.decision
        trade = None
        if d.action.value == "LONG" and rec.outcome == "FILLED":
            cands = by_decision.get(rec.time) or []
            if cands:
                tr = cands.pop(0)
                c = tr.trade
                trade = TradeOutcome(
                    c.deal_id, c.opened_at, c.closed_at, c.entry_price, c.exit_price, tr.net_pnl_eur, tr.r_multiple,
                    c.exit_reason, c.funding_eur, c.fees_eur, c.gross_pnl_eur, tr.initial_risk_eur,
                )
        m = d.meta or {}
        entries.append(JournalEntry(
            time=rec.time, action=d.action.value, stage=str(m.get("stage", "")), outcome=rec.outcome, reason=d.reason,
            setup=d.setup, regime=d.regime, bias=d.bias, quality=_f(m.get("quality")), confidence=d.confidence,
            stop_level=d.stop_level, take_profit_level=d.take_profit_level, room_r=_f(m.get("room_r")), trade=trade,
        ))
    return DecisionJournal(entries, f"backtest:{result.strategy}")


def _f(v) -> float | None:
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _trade_from_record(r: dict) -> TradeOutcome | None:
    opened, closed = parse_time(r.get("opened_at")), parse_time(r.get("closed_at"))
    if opened is None or closed is None:
        return None
    net = _f(r.get("net_pnl_eur"))
    if net is None:
        net = (_f(r.get("gross_pnl_eur")) or 0.0) - (_f(r.get("funding_eur")) or 0.0) - (_f(r.get("fees_eur")) or 0.0)
    risk = _f(r.get("initial_risk_eur"))
    rm = _f(r.get("r_multiple"))
    if rm is None and risk:
        rm = net / risk
    return TradeOutcome(
        str(r.get("deal_id", "")), opened, closed, _f(r.get("entry_price")) or 0.0, _f(r.get("exit_price")) or 0.0, net, rm,
        str(r.get("exit_reason") or ""), _f(r.get("funding_eur")) or 0.0, _f(r.get("fees_eur")) or 0.0, _f(r.get("gross_pnl_eur")), risk,
    )


def from_runtime(runtime_dir: str | Path) -> DecisionJournal:
    """Journal depuis les fichiers de la boucle paper (``decisions.jsonl``, ``trades.jsonl``).

    Un trade est relié à la décision ``LONG`` de même ``decided_at`` si ce champ
    existe, sinon à la dernière décision ``LONG`` exécutée avant son ouverture.
    """
    root = Path(runtime_dir)
    trades = [t for t in (_trade_from_record(r) for r in _read_jsonl(root / "trades.jsonl")) if t is not None]
    decided = {}
    for r in _read_jsonl(root / "trades.jsonl"):
        dt = parse_time(r.get("decided_at"))
        if dt is not None:
            decided[str(r.get("deal_id", ""))] = dt
    entries: list[JournalEntry] = []
    for r in _read_jsonl(root / "decisions.jsonl"):
        t = parse_time(r.get("time"))
        action = str(r.get("action", "")).upper()
        if t is None or action not in ("LONG", "HOLD", "CLOSE", "NO_TRADE"):
            continue
        meta = r.get("meta") if isinstance(r.get("meta"), dict) else {}
        entries.append(JournalEntry(
            time=t, action=action, stage=str(r.get("stage") or meta.get("stage") or ""), outcome=str(r.get("outcome") or ""),
            reason=str(r.get("reason") or ""), setup=str(r.get("setup") or ""), regime=str(r.get("regime") or ""), bias=str(r.get("bias") or ""),
            quality=_f(r.get("quality", meta.get("quality"))), confidence=_f(r.get("confidence")), price=_f(r.get("price")),
            stop_level=_f(r.get("stop_level")), take_profit_level=_f(r.get("take_profit_level")), room_r=_f(r.get("room_r", meta.get("room_r"))),
        ))
    entries.sort(key=lambda e: e.time)
    longs = [i for i, e in enumerate(entries) if e.action == "LONG" and (e.outcome in ("", "FILLED"))]
    taken: set[int] = set()
    for tr in sorted(trades, key=lambda t: t.opened_at):
        idx = None
        want = decided.get(tr.deal_id)
        if want is not None:
            idx = next((i for i in longs if entries[i].time == want and i not in taken), None)
        if idx is None:
            prior = [i for i in longs if entries[i].time <= tr.opened_at and i not in taken]
            idx = prior[-1] if prior else None
        if idx is None:
            # Trade sans décision journalisée : entrée minimale pour ne pas le perdre.
            entries.append(JournalEntry(time=tr.opened_at, action="LONG", outcome="FILLED", reason="décision absente du journal", trade=tr))
            continue
        taken.add(idx)
        entries[idx] = replace(entries[idx], trade=tr, outcome="FILLED")
    entries.sort(key=lambda e: e.time)
    return DecisionJournal(entries, f"runtime:{root}")


# ------------------------------------------------------------------ contrefactuels


def attach_forward_moves(journal: DecisionJournal, bars: Sequence, horizon: timedelta, actions: Iterable[str] = ("NO_TRADE",)) -> DecisionJournal:
    """Ajoute à chaque décision la variation du prix (bid close) sur ``horizon``.

    Prix de départ : clôture de la bougie qui se termine à l'instant de décision
    (ou la dernière connue avant). Prix d'arrivée : dernière clôture connue à
    ``décision + horizon``. ``forward_known_at`` permet de masquer la valeur tant
    qu'elle n'est pas connue (``DecisionJournal.as_of``).
    """
    if not bars:
        return journal
    step = bars[0].timeframe.minutes
    ends = [b.time + timedelta(minutes=step) for b in bars]
    closes = [b.bid.close for b in bars]
    acts = set(actions)
    out = []
    for e in journal.entries:
        if e.action in acts:
            i0 = bisect.bisect_right(ends, e.time) - 1
            target = e.time + horizon
            i1 = bisect.bisect_right(ends, target) - 1
            if 0 <= i0 < i1 and ends[-1] >= target and closes[i0] > 0:
                e = replace(e, price=e.price if e.price is not None else closes[i0], forward_move=closes[i1] / closes[i0] - 1, forward_known_at=ends[i1])
        out.append(e)
    return DecisionJournal(out, journal.source)
