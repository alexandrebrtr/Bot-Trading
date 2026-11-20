"""État d'exécution lu par le dashboard (lecture seule).

Le dashboard ne parle à aucun moteur en direct : il lit des fichiers que la
boucle de paper trading, le moteur de décision et les boucles d'amélioration
écrivent dans un dossier d'exécution (``data/runtime`` par défaut, ignoré par git).

| Fichier | Contenu | Producteur |
|---|---|---|
| ``account.json`` | equity, solde, plus haut, marge, exposition, ``updated_at`` | boucle paper |
| ``equity.jsonl`` | une ligne ``{"time", "equity_eur"}`` par point | boucle paper |
| ``positions.json`` | liste des positions ouvertes | boucle paper |
| ``trades.jsonl`` | une ligne par trade clos | boucle paper |
| ``decisions.jsonl`` | une ligne par décision (voir ``decision_feed``) | moteur de décision |
| ``risk_events.jsonl`` | vetos et alertes du Risk Engine | boucle paper |
| ``regime.json`` | ``RegimeAssessment`` (JSON Pydantic) | Market Regime Engine |
| ``experiments.jsonl`` | essais et propositions des boucles d'amélioration | agent d'amélioration |
| ``heartbeat.json`` | ``{"time", "component", "status", "message"}`` par composant | tous |
| ``kill_switch.json`` | état persistant du kill switch | ``risk.kill_switch`` |

Toute lecture tolère l'absence du fichier (état vide) et ignore les lignes
corrompues en les comptant dans ``warnings`` : un fichier abîmé ne doit jamais
faire tomber le dashboard. Les helpers ``write_json`` et ``append_jsonl``
servent aux producteurs (écriture atomique, horodatage UTC ISO 8601).
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

DEFAULT_RUNTIME_DIR = Path("data/runtime")


# ------------------------------------------------------------------ sérialisation


def _default(o: Any):
    if isinstance(o, datetime):
        if o.tzinfo is None:
            o = o.replace(tzinfo=timezone.utc)
        return o.isoformat()
    if isinstance(o, date):
        return o.isoformat()
    if isinstance(o, Enum):
        return o.value
    if hasattr(o, "model_dump"):
        return o.model_dump(mode="json")
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    raise TypeError(f"Type non sérialisable : {type(o).__name__}")


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=_default, ensure_ascii=False)


def write_json(path: str | Path, obj: Any) -> None:
    """Écriture atomique (fichier temporaire + ``os.replace``)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(dumps(obj))
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def append_jsonl(path: str | Path, obj: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(dumps(obj) + "\n")


def parse_time(value: Any) -> datetime | None:
    """ISO 8601 → datetime UTC. ``None`` si absent ou invalide."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


# ------------------------------------------------------------------ lecture


@dataclass(frozen=True)
class AccountSnapshot:
    equity_eur: float
    balance_eur: float
    peak_equity_eur: float
    used_margin_eur: float
    open_exposure_lots: float
    updated_at: datetime | None
    mode: str = "paper"


@dataclass
class RuntimeReader:
    root: Path = DEFAULT_RUNTIME_DIR
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.root = Path(self.root)

    def path(self, name: str) -> Path:
        return self.root / name

    def exists(self) -> bool:
        return self.root.is_dir()

    # -------------------------------------------------------------- bas niveau
    def read_json(self, name: str) -> Any | None:
        p = self.path(name)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            self.warnings.append(f"{name} illisible ({type(e).__name__})")
            return None

    def read_jsonl(self, name: str, limit: int | None = None) -> list[dict]:
        p = self.path(name)
        if not p.exists():
            return []
        rows: list[dict] = []
        bad = 0
        try:
            with p.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        bad += 1
                        continue
                    if isinstance(obj, dict):
                        rows.append(obj)
                    else:
                        bad += 1
        except OSError as e:
            self.warnings.append(f"{name} illisible ({type(e).__name__})")
            return []
        if bad:
            self.warnings.append(f"{name} : {bad} ligne(s) corrompue(s) ignorée(s)")
        return rows[-limit:] if limit else rows

    # -------------------------------------------------------------- vues
    def account(self) -> AccountSnapshot | None:
        d = self.read_json("account.json")
        if not isinstance(d, dict):
            return None
        try:
            equity = float(d["equity_eur"])
            return AccountSnapshot(
                equity_eur=equity,
                balance_eur=float(d.get("balance_eur", equity)),
                peak_equity_eur=float(d.get("peak_equity_eur", equity)),
                used_margin_eur=float(d.get("used_margin_eur", 0.0)),
                open_exposure_lots=float(d.get("open_exposure_lots", 0.0)),
                updated_at=parse_time(d.get("updated_at")),
                mode=str(d.get("mode", "paper")),
            )
        except (KeyError, TypeError, ValueError):
            self.warnings.append("account.json : champs manquants ou invalides")
            return None

    def equity_curve(self) -> list[tuple[datetime, float]]:
        out: list[tuple[datetime, float]] = []
        for r in self.read_jsonl("equity.jsonl"):
            t = parse_time(r.get("time"))
            try:
                v = float(r["equity_eur"])
            except (KeyError, TypeError, ValueError):
                continue
            if t is not None:
                out.append((t, v))
        out.sort(key=lambda x: x[0])
        return out

    def positions(self) -> list[dict]:
        d = self.read_json("positions.json")
        return [p for p in d if isinstance(p, dict)] if isinstance(d, list) else []

    def trades(self, limit: int | None = None) -> list[dict]:
        return self.read_jsonl("trades.jsonl", limit)

    def risk_events(self, limit: int | None = 200) -> list[dict]:
        return self.read_jsonl("risk_events.jsonl", limit)

    def regime(self) -> dict | None:
        d = self.read_json("regime.json")
        return d if isinstance(d, dict) else None

    def experiments(self, limit: int | None = None) -> list[dict]:
        return self.read_jsonl("experiments.jsonl", limit)

    def heartbeats(self) -> list[dict]:
        d = self.read_json("heartbeat.json")
        if isinstance(d, dict):
            # {"composant": {...}} ou un seul battement {"component": ...}
            if "component" in d:
                return [d]
            return [{"component": k, **v} for k, v in d.items() if isinstance(v, dict)]
        if isinstance(d, list):
            return [x for x in d if isinstance(x, dict)]
        return []
