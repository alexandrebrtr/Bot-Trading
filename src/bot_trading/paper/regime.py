"""Vue fondamentale du paper trading, lue dans ``regime.json``.

Le Market Regime Engine publie une ``RegimeAssessment`` dans le dossier
d'exécution. Pour chaque décision, ``RuntimeRegimeProvider`` renvoie :

- ``FundamentalView.unavailable`` si le fichier manque, est illisible, est
  une donnée de démonstration, a été publié **après** l'instant de décision
  (rattrapage de bougies anciennes : pas de look-ahead) ou est trop ancien ;
- sinon ``FundamentalView.from_assessment`` : le régime défavorable bloque les
  entrées et réduit la taille (F10, ``size_multiplier`` ≤ 1) ; sur une position
  ouverte, ``RISK_OFF`` resserre le stop sans clôture forcée (Q2, validé).

Sans fondamental, le Decision Engine ne peut ouvrir aucune position (S2).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from bot_trading.strategy.fundamental import FundamentalView


class RuntimeRegimeProvider:
    def __init__(self, path: str | Path, max_age: timedelta = timedelta(hours=48)) -> None:
        self.path = Path(path)
        self.max_age = max_age
        self._cache: tuple[float, object] | None = None
        self.last_problem = ""

    def _load(self):
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            self.last_problem = f"{self.path.name} absent"
            return None
        if self._cache is not None and self._cache[0] == mtime:
            return self._cache[1]
        from bot_trading.intelligence.schemas import RegimeAssessment

        value: object
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("objet JSON attendu")
            if data.get("demo"):
                value = "évaluation de démonstration refusée"
            else:
                value = RegimeAssessment.model_validate(data)
        except (OSError, ValueError) as e:  # pydantic.ValidationError hérite de ValueError
            value = f"{self.path.name} illisible ({type(e).__name__})"
        self._cache = (mtime, value)
        return value

    def view(self, now: datetime) -> FundamentalView:
        a = self._load()
        if a is None:
            return FundamentalView.unavailable(self.last_problem)
        if isinstance(a, str):
            self.last_problem = a
            return FundamentalView.unavailable(a)
        asof = a.asof  # type: ignore[attr-defined]
        if asof > now:
            self.last_problem = f"régime publié à {asof.isoformat()}, après la décision ({now.isoformat()})"
            return FundamentalView.unavailable(self.last_problem)
        if now - asof > self.max_age:
            self.last_problem = f"régime périmé (publié à {asof.isoformat()})"
            return FundamentalView.unavailable(self.last_problem)
        self.last_problem = ""
        return FundamentalView.from_assessment(a)

    def __call__(self, ctx) -> FundamentalView:
        """Signature de ``DecisionStrategy.fundamental_provider``."""
        return self.view(ctx.now)
