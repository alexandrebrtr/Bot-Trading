"""Registre des essais et propositions (``experiments.jsonl``), en ajout seul.

- Chaque ré-évaluation y écrit une ligne (format lu par l'onglet Amélioration
  du dashboard) ; le total des ``n_trials`` sert au Sharpe déflaté suivant.
- Une revue humaine s'enregistre par une **nouvelle** ligne (``review``) : rien
  n'est réécrit, l'historique reste vérifiable.
- Le registre n'applique jamais de paramètres. Une proposition approuvée reste
  à mettre en œuvre à la main (commit revu), après paper trading.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from bot_trading.dashboard.runtime import append_jsonl

REVIEW_STATUSES = ("approuvé (à appliquer manuellement après paper trading)", "refusé par revue humaine")


class ExperimentRegistry:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def records(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(r, dict):
                out.append(r)
        return out

    def total_trials(self) -> int:
        """Essais cumulés (hors démo) : à reporter dans EXPERIMENTS.md."""
        return sum(int(r.get("n_trials") or 0) for r in self.records() if not r.get("demo"))

    def record(self, proposal) -> dict:
        rec = proposal.to_experiment()
        append_jsonl(self.path, rec)
        return rec

    def review(self, proposal_id: str, reviewer: str, approve: bool, note: str = "") -> dict:
        """Décision humaine sur une proposition. ``reviewer`` est obligatoire."""
        if not reviewer.strip():
            raise ValueError("Une revue exige le nom du relecteur humain")
        if not any(r.get("id") == proposal_id for r in self.records()):
            raise KeyError(f"Proposition inconnue : {proposal_id}")
        rec = {
            "time": datetime.now(timezone.utc),
            "id": f"{proposal_id}-revue",
            "review_of": proposal_id,
            "loop": "revue humaine",
            "reviewer": reviewer,
            "status": REVIEW_STATUSES[0] if approve else REVIEW_STATUSES[1],
            "note": note,
            "applied": False,
            "n_trials": 0,
        }
        append_jsonl(self.path, rec)
        return rec
