"""Kill switch persistant, appliqué par le Risk Engine.

Le Risk Engine reste sans I/O : son kill switch est un état en mémoire
(``RiskEngine.engage_kill_switch``). Ce module le rend durable entre processus
(dashboard, boucle de paper trading, CLI) par un petit fichier JSON :

- ``engage(engine, …)`` passe **par le Risk Engine** (``engine.engage_kill_switch()``)
  puis écrit l'état et une ligne d'audit ;
- ``apply(engine)`` recharge l'état dans un Risk Engine neuf. Toute boucle qui
  appelle ``RiskEngine.check_order`` doit appeler ``apply`` avant, pour qu'un
  kill switch engagé ailleurs produise un veto ``KILL_SWITCH`` ;
- ``reset(engine, confirmation)`` exige la même confirmation humaine que
  ``RiskEngine.reset_halt`` (le dashboard ne l'expose pas).

Le fichier est illisible ou corrompu ? Le kill switch est considéré comme
**engagé** : en cas de doute, on bloque.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from bot_trading.risk.engine import RiskEngine

DEFAULT_PATH = Path("data/runtime/kill_switch.json")
CONFIRMATION = "JE_CONFIRME_LA_REPRISE"


@dataclass(frozen=True)
class KillSwitchState:
    engaged: bool
    changed_at: str | None = None
    reason: str = ""
    actor: str = ""
    corrupted: bool = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _write_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".ks_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


class KillSwitchStore:
    def __init__(self, path: str | Path = DEFAULT_PATH) -> None:
        self.path = Path(path)
        self.audit_path = self.path.with_name("kill_switch_audit.jsonl")

    def load(self) -> KillSwitchState:
        if not self.path.exists():
            return KillSwitchState(engaged=False)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return KillSwitchState(
                engaged=bool(data["engaged"]),
                changed_at=data.get("changed_at"),
                reason=str(data.get("reason", "")),
                actor=str(data.get("actor", "")),
            )
        except (OSError, ValueError, KeyError, TypeError):
            return KillSwitchState(engaged=True, reason="fichier du kill switch illisible", corrupted=True)

    def apply(self, engine: RiskEngine) -> KillSwitchState:
        """Recharge l'état persistant dans ``engine`` (n'enlève jamais un kill switch déjà engagé)."""
        state = self.load()
        if state.engaged:
            engine.engage_kill_switch()
        return state

    def engage(self, engine: RiskEngine, reason: str, actor: str = "dashboard", now: datetime | None = None) -> KillSwitchState:
        engine.engage_kill_switch()
        if not engine.kill_switch:  # garde-fou : le Risk Engine doit confirmer
            raise RuntimeError("Le Risk Engine n'a pas engagé le kill switch")
        state = KillSwitchState(True, (now or _now()).isoformat(), reason.strip() or "sans motif", actor)
        self._persist(state, "ENGAGE")
        return state

    def reset(self, engine: RiskEngine, confirmation: str, actor: str = "cli", now: datetime | None = None) -> KillSwitchState:
        engine.reset_halt(confirmation)  # lève PermissionError sans la confirmation exacte
        state = KillSwitchState(False, (now or _now()).isoformat(), "reprise confirmée", actor)
        self._persist(state, "RESET")
        return state

    def _persist(self, state: KillSwitchState, action: str) -> None:
        payload = {k: v for k, v in asdict(state).items() if k != "corrupted"}
        _write_atomic(self.path, payload)
        with self.audit_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"action": action, **payload}, ensure_ascii=False) + "\n")
