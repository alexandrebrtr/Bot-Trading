"""Interface entre le moteur de décision et le dashboard.

Le dashboard ne dépend pas du moteur de décision : il consomme un
``DecisionFeed`` (protocole d'une méthode). L'implémentation par défaut lit
``decisions.jsonl`` dans le dossier d'exécution.

Côté producteur, une seule fonction suffit ::

    from bot_trading.dashboard.decision_feed import publish_decision
    publish_decision("data/runtime/decisions.jsonl", decision, time=now,
                     outcome="VETO", violations=("SPREAD_TOO_WIDE",), price=6812.4)

``decision`` est un ``bot_trading.core.decision.Decision``. ``outcome`` décrit
ce qui s'est passé après la décision (``FILLED``, ``VETO``, ``NO_TRADE``,
``SIZE_BELOW_MIN``…) et ``violations`` les codes du Risk Engine.

Format d'une ligne (tous les champs sauf ``time`` et ``action`` sont facultatifs) ::

    {"time": "2026-11-20T15:00:00+00:00", "action": "NO_TRADE", "reason": "...",
     "confidence": 0.4, "setup": "FVG_H1_50", "regime": "RISK_ON", "bias": "bullish",
     "stop_level": null, "take_profit_level": null, "risk_fraction": null, "size": null,
     "sources": ["ALFRED:CPIAUCSL"], "outcome": "NO_TRADE", "violations": [],
     "price": 6812.4, "timeframe": "H1"}
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from bot_trading.core.decision import Action, Decision
from bot_trading.dashboard.runtime import RuntimeReader, append_jsonl, parse_time

ACTIONS = tuple(a.value for a in Action)


@dataclass(frozen=True)
class DecisionView:
    time: datetime
    action: str
    reason: str = ""
    confidence: float | None = None
    setup: str = ""
    regime: str = ""
    bias: str = ""
    stop_level: float | None = None
    take_profit_level: float | None = None
    risk_fraction: float | None = None
    size: float | None = None
    sources: tuple[str, ...] = ()
    outcome: str = ""
    violations: tuple[str, ...] = ()
    price: float | None = None
    timeframe: str = ""

    @property
    def vetoed(self) -> bool:
        return bool(self.violations) or self.outcome.upper().startswith("VETO")


@runtime_checkable
class DecisionFeed(Protocol):
    def latest(self, limit: int = 200) -> list[DecisionView]: ...


def _opt_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _str_tuple(v: Any) -> tuple[str, ...]:
    if isinstance(v, str):
        return (v,) if v else ()
    if isinstance(v, (list, tuple)):
        return tuple(str(x) for x in v)
    return ()


def parse_record(rec: dict) -> DecisionView | None:
    """Ligne JSON → ``DecisionView``. ``None`` si l'heure ou l'action est invalide."""
    t = parse_time(rec.get("time"))
    action = str(rec.get("action", "")).upper()
    if t is None or action not in ACTIONS:
        return None
    return DecisionView(
        time=t,
        action=action,
        reason=str(rec.get("reason") or ""),
        confidence=_opt_float(rec.get("confidence")),
        setup=str(rec.get("setup") or ""),
        regime=str(rec.get("regime") or ""),
        bias=str(rec.get("bias") or ""),
        stop_level=_opt_float(rec.get("stop_level")),
        take_profit_level=_opt_float(rec.get("take_profit_level")),
        risk_fraction=_opt_float(rec.get("risk_fraction")),
        size=_opt_float(rec.get("size")),
        sources=_str_tuple(rec.get("sources")),
        outcome=str(rec.get("outcome") or ""),
        violations=_str_tuple(rec.get("violations")),
        price=_opt_float(rec.get("price")),
        timeframe=str(rec.get("timeframe") or ""),
    )


def decision_to_record(
    decision: Decision,
    time: datetime,
    outcome: str = "",
    violations: Sequence[str] = (),
    price: float | None = None,
    timeframe: str = "",
) -> dict:
    return {
        "time": time,
        "action": decision.action.value,
        "reason": decision.reason,
        "confidence": decision.confidence,
        "setup": decision.setup,
        "regime": decision.regime,
        "bias": decision.bias,
        "stop_level": decision.stop_level,
        "take_profit_level": decision.take_profit_level,
        "risk_fraction": decision.risk_fraction,
        "size": decision.size,
        "sources": list(decision.sources),
        "outcome": outcome,
        "violations": [getattr(v, "value", v) for v in violations],
        "price": price,
        "timeframe": timeframe,
    }


def publish_decision(path: str | Path, decision: Decision, time: datetime, **kw) -> None:
    append_jsonl(path, decision_to_record(decision, time, **kw))


class JsonlDecisionFeed:
    """Lit ``decisions.jsonl`` ; les lignes invalides sont ignorées et comptées."""

    def __init__(self, reader: RuntimeReader, name: str = "decisions.jsonl") -> None:
        self.reader = reader
        self.name = name

    def latest(self, limit: int = 200) -> list[DecisionView]:
        out: list[DecisionView] = []
        invalid = 0
        for rec in self.reader.read_jsonl(self.name):
            v = parse_record(rec)
            if v is None:
                invalid += 1
            else:
                out.append(v)
        if invalid:
            self.reader.warnings.append(f"{self.name} : {invalid} décision(s) invalide(s) ignorée(s)")
        out.sort(key=lambda d: d.time)
        return out[-limit:] if limit else out


def summarize(decisions: Sequence[DecisionView]) -> dict[str, Any]:
    """Répartition des actions, part de NO_TRADE et vetos par code."""
    counts = {a: 0 for a in ACTIONS}
    vetoes: dict[str, int] = {}
    for d in decisions:
        counts[d.action] = counts.get(d.action, 0) + 1
        for v in d.violations:
            vetoes[v] = vetoes.get(v, 0) + 1
    n = len(decisions)
    return {
        "total": n,
        "counts": counts,
        "no_trade_share": counts["NO_TRADE"] / n if n else None,
        "vetoed": sum(1 for d in decisions if d.vetoed),
        "vetoes_by_code": dict(sorted(vetoes.items(), key=lambda kv: -kv[1])),
        "last": decisions[-1] if decisions else None,
    }
