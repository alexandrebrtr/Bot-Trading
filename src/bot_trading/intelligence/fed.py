"""Lecture déterministe du ton de la Fed (F4).

Méthode : lexique de locutions hawkish / dovish, négations simples, score
normalisé dans [-1 ; 1] (+1 = très hawkish). Le ton d'une intervention est
comparé à la précédente intervention **du même orateur** : c'est le
changement de discours qui porte l'information, plus que le niveau.

Le texte est une donnée externe : il est seulement compté, jamais exécuté
ni transmis tel quel à un autre composant.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from bot_trading.intelligence.data import FedCommunication

HAWKISH = {
    "inflation remains elevated": 2.0,
    "inflation is elevated": 2.0,
    "further tightening": 2.5,
    "additional policy firming": 2.5,
    "raise rates": 2.0,
    "rate increase": 1.5,
    "higher for longer": 2.0,
    "restrictive for some time": 2.0,
    "maintain a restrictive": 1.5,
    "upside risks to inflation": 2.0,
    "inflation risks": 1.0,
    "not yet confident": 1.5,
    "premature to cut": 2.0,
    "no rush": 1.5,
    "patient": 0.5,
    "strong labor market": 0.5,
    "tight labor market": 1.0,
    "wage pressures": 1.0,
    "balance sheet reduction": 1.0,
    "vigilant": 1.0,
    "price stability": 0.3,
}

DOVISH = {
    "rate cut": 2.0,
    "lower rates": 1.5,
    "reduce the target range": 2.5,
    "lowered the target range": 2.5,
    "easing": 1.5,
    "accommodative": 1.5,
    "downside risks to employment": 2.0,
    "labor market has softened": 2.0,
    "labor market is cooling": 1.5,
    "softening": 1.0,
    "disinflation": 1.5,
    "inflation has eased": 2.0,
    "inflation has moved closer": 1.5,
    "greater confidence": 1.0,
    "recalibrat": 1.5,
    "less restrictive": 2.0,
    "productivity": 0.5,
    "end balance sheet runoff": 1.5,
    "support the economy": 1.0,
}

NEGATIONS = ("not ", "no ", "n't ", "never ", "without ")

# Poids des orateurs (configurable). Les communiqués FOMC et le président pèsent
# plus que les autres membres. Kevin Warsh est suivi explicitement (mission V3).
DEFAULT_SPEAKER_WEIGHTS = {
    "FOMC": 3.0,
    "Warsh": 2.0,
    "Powell": 2.0,
    "Jefferson": 1.2,
    "Williams": 1.2,
    "Bowman": 1.0,
    "Waller": 1.0,
    "Barr": 1.0,
    "Cook": 1.0,
    "Kugler": 1.0,
    "Miran": 1.0,
}
KIND_WEIGHTS = {"statement": 1.5, "press_conference": 1.3, "minutes": 1.0, "testimony": 1.2, "speech": 1.0}


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text).lower()
    t = re.sub(r"[​-‏ -‮﻿]", "", t)  # caractères invisibles
    return re.sub(r"\s+", " ", t)


def _count(text: str, lexicon: dict[str, float]) -> tuple[float, float]:
    pos = neg = 0.0
    for phrase, w in lexicon.items():
        for m in re.finditer(re.escape(phrase), text):
            window = text[max(0, m.start() - 12) : m.start()]
            if any(n in window for n in NEGATIONS):
                neg += w
            else:
                pos += w
    return pos, neg


def tone(text: str) -> tuple[float, int]:
    """Ton dans [-1 ; 1] (+1 hawkish) et nombre de signaux trouvés."""
    t = normalize(text)
    h_pos, h_neg = _count(t, HAWKISH)
    d_pos, d_neg = _count(t, DOVISH)
    # une locution hawkish niée compte comme dovish et inversement
    hawk = h_pos + d_neg
    dove = d_pos + h_neg
    total = hawk + dove
    if total == 0:
        return 0.0, 0
    n = sum(1 for p in list(HAWKISH) + list(DOVISH) if p in t)
    return (hawk - dove) / total * min(1.0, total / 4.0), n


@dataclass(frozen=True)
class SpeakerReading:
    comm: FedCommunication
    tone: float
    previous_tone: float | None
    signals: int
    weight: float

    @property
    def delta(self) -> float | None:
        return None if self.previous_tone is None else self.tone - self.previous_tone


def read_communications(
    comms: list[FedCommunication],
    speaker_weights: dict[str, float] | None = None,
) -> list[SpeakerReading]:
    """Ton de la dernière intervention de chaque orateur, comparé à la précédente.

    ``comms`` doit déjà être filtré point-in-time et trié par date.
    """
    sw = speaker_weights or DEFAULT_SPEAKER_WEIGHTS
    last: dict[str, SpeakerReading] = {}
    for c in comms:
        t, n = tone(f"{c.title}. {c.text}")
        prev = last.get(c.speaker)
        last[c.speaker] = SpeakerReading(
            comm=c,
            tone=t,
            previous_tone=prev.tone if prev else None,
            signals=n,
            weight=sw.get(c.speaker, 0.7) * KIND_WEIGHTS.get(c.kind, 1.0),
        )
    return sorted(last.values(), key=lambda r: r.comm.published_at)
