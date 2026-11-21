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
        self._hist_cache: tuple[float, list] | None = None
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

    def _history(self) -> list:
        """Évaluations archivées (``regime_history.jsonl``), triées par ``asof``. Lignes invalides ignorées."""
        from bot_trading.intelligence.schemas import RegimeAssessment

        p = self.path.with_name("regime_history.jsonl")
        try:
            mtime = p.stat().st_mtime
        except OSError:
            return []
        if self._hist_cache is not None and self._hist_cache[0] == mtime:
            return self._hist_cache[1]
        out = []
        with p.open(encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                    if isinstance(d, dict) and not d.get("demo"):
                        out.append(RegimeAssessment.model_validate(d))
                except ValueError:
                    continue
        out.sort(key=lambda a: a.asof)
        self._hist_cache = (mtime, out)
        return out

    def _from_history(self, now: datetime):
        """Dernière évaluation archivée publiée au plus tard à ``now`` (rattrapage point-in-time)."""
        past = [a for a in self._history() if a.asof <= now]
        return past[-1] if past else None

    def view(self, now: datetime) -> FundamentalView:
        a = self._load()
        if a is None:
            return FundamentalView.unavailable(self.last_problem)
        if isinstance(a, str):
            self.last_problem = a
            return FundamentalView.unavailable(a)
        asof = a.asof  # type: ignore[attr-defined]
        if asof > now:
            past = self._from_history(now)
            if past is None:
                self.last_problem = f"régime publié à {asof.isoformat()}, après la décision ({now.isoformat()})"
                return FundamentalView.unavailable(self.last_problem)
            a, asof = past, past.asof
        if now - asof > self.max_age:
            self.last_problem = f"régime périmé (publié à {asof.isoformat()})"
            return FundamentalView.unavailable(self.last_problem)
        self.last_problem = ""
        return FundamentalView.from_assessment(a)

    def __call__(self, ctx) -> FundamentalView:
        """Signature de ``DecisionStrategy.fundamental_provider``."""
        return self.view(ctx.now)


# ------------------------------------------------------------------ production automatique


def data_coverage(a) -> tuple[int, int]:
    """(composantes macro F1–F5 disponibles, composantes intermarchés disponibles)."""
    macro = sum(1 for s in a.macro.subscores if s.available and s.component.startswith("F"))
    inter = sum(1 for s in a.subscores if s.available and s.component.startswith("F6"))
    return macro, inter


def guard_coverage(a, min_macro: int = 2, min_intermarket: int = 1):
    """Sans assez de données réelles, l'évaluation est publiée mais n'autorise aucune entrée."""
    macro, inter = data_coverage(a)
    if macro >= min_macro and inter >= min_intermarket:
        return a
    why = f"Données insuffisantes ({macro} composante(s) macro, {inter} intermarché(s)) : aucune nouvelle entrée"
    return a.model_copy(update={"allow_new_entries": False, "size_multiplier": 0.0, "overrides": list(a.overrides) + [why]})


class RegimePublisher:
    """Calcule ``regime.json`` depuis le store (Market Regime Engine) et l'archive dans ``regime_history.jsonl``.

    ``asof`` = dernière clôture de bougie du timeframe : l'évaluation ne voit que
    les données publiées à cet instant, et la décision prise à cette clôture peut
    l'utiliser (``asof`` ≤ instant de décision).
    """

    def __init__(self, store, runtime_dir: str | Path, minutes: int = 60, every: timedelta = timedelta(hours=1), assess=None, **pit_kw) -> None:
        from bot_trading.marketdata.pit import assess_regime

        self.store = store
        self.root = Path(runtime_dir)
        self.minutes = minutes
        self.every = every
        self.assess = assess or assess_regime
        self.pit_kw = pit_kw
        self.last_asof: datetime | None = None

    def maybe_publish(self, now: datetime):
        from bot_trading.dashboard.runtime import append_jsonl, write_json
        from bot_trading.marketdata.pit import floor_time

        asof = floor_time(now, self.minutes)
        if self.last_asof is not None and asof - self.last_asof < self.every:
            return None
        previous = None
        current = RuntimeRegimeProvider(self.root / "regime.json", max_age=timedelta(days=3650))._load()
        if current is not None and not isinstance(current, str):
            if current.asof >= asof and self.last_asof is None:
                self.last_asof = current.asof
                return None
            previous = current.regime
        a = guard_coverage(self.assess(self.store, asof, previous, **self.pit_kw))
        data = a.model_dump(mode="json")
        write_json(self.root / "regime.json", data)
        append_jsonl(self.root / "regime_history.jsonl", data)
        self.last_asof = asof
        return a
