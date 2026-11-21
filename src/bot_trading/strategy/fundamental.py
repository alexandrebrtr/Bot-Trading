"""Vue fondamentale consommée par le Decision Engine.

Le Decision Engine ne lit jamais directement les agents : il reçoit une
``FundamentalView`` figée à l'instant de décision, construite :

- depuis une ``RegimeAssessment`` du Market Regime Engine (``from_assessment``) ;
- ou depuis les séries point-in-time du backtester (``from_pit``) :
  ``regime/assessment`` (valeur = ``RegimeAssessment`` ou ``FundamentalView``)
  sinon ``regime`` + ``bias/swing`` (format des données synthétiques et des
  agents simples).

Sans donnée fondamentale, la vue est ``available=False`` et le Decision Engine
répond ``NO_TRADE`` : la technique seule ne peut jamais ouvrir une position (S2).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from bot_trading.marketdata.source import Observation

# Séries publiées dans la PointInTimeSource du backtester.
SERIES_ASSESSMENT = "regime/assessment"
SERIES_REGIME = "regime"
SERIES_BIAS = "bias/swing"

RISK_ON = "RISK_ON"
NEUTRE = "NEUTRE"
RISK_OFF = "RISK_OFF"

_REGIME_ALIASES = {
    "RISK_ON": RISK_ON,
    "NEUTRE": NEUTRE,
    "NEUTRAL": NEUTRE,
    "RISK_OFF": RISK_OFF,
    # étiquettes des données synthétiques (marketdata/synthetic.py)
    "BULL": RISK_ON,
    "CHOP": NEUTRE,
    "BEAR": RISK_OFF,
}
_BIAS_ALIASES = {"BULLISH": "bullish", "NEUTRAL": "neutral", "NEUTRE": "neutral", "BEARISH": "bearish"}

# Politique par défaut quand seule l'étiquette de régime est connue (alignée sur RegimeConfig).
DEFAULT_SIZE_MULTIPLIER = {RISK_ON: 1.0, NEUTRE: 0.5, RISK_OFF: 0.0}
DEFAULT_MIN_QUALITY = {RISK_ON: 0.5, NEUTRE: 0.8, RISK_OFF: 1.0}


def normalize_regime(value) -> str | None:
    if value is None:
        return None
    v = getattr(value, "value", value)
    return _REGIME_ALIASES.get(str(v).upper())


def normalize_bias(value) -> str:
    if value is None:
        return "unknown"
    v = str(getattr(value, "value", value))
    return _BIAS_ALIASES.get(v.upper(), v.lower() if v.lower() in ("bullish", "neutral", "bearish") else "unknown")


@dataclass(frozen=True)
class FundamentalView:
    available: bool
    asof: datetime | None = None
    regime: str | None = None  # RISK_ON / NEUTRE / RISK_OFF
    bias: str = "unknown"  # bullish / neutral / bearish / unknown
    score: float | None = None  # [-1 ; 1] si connu
    uncertainty: float | None = None  # [0 ; 1] si connue
    allow_new_entries: bool = False
    size_multiplier: float = 0.0  # ≤ 1, jamais d'augmentation du risque
    min_setup_quality: float = 1.0
    event_blackout: bool = False
    invalidations: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    overrides: tuple[str, ...] = ()

    @classmethod
    def unavailable(cls, why: str = "aucune donnée fondamentale") -> "FundamentalView":
        return cls(available=False, overrides=(why,))

    # ------------------------------------------------------------ fabriques
    @classmethod
    def from_labels(
        cls,
        regime,
        bias=None,
        asof: datetime | None = None,
        sources: Iterable[str] = (),
        event_blackout: bool = False,
    ) -> "FundamentalView":
        reg = normalize_regime(regime)
        b = normalize_bias(bias)
        if reg is None and b == "unknown":
            return cls.unavailable()
        if reg is None:  # biais seul : on en déduit un régime prudent
            reg = {"bullish": RISK_ON, "neutral": NEUTRE, "bearish": RISK_OFF}[b]
        if b == "unknown":
            b = {RISK_ON: "bullish", NEUTRE: "neutral", RISK_OFF: "bearish"}[reg]
        allow = reg != RISK_OFF and not event_blackout
        return cls(
            available=True,
            asof=asof,
            regime=reg,
            bias=b,
            allow_new_entries=allow,
            size_multiplier=DEFAULT_SIZE_MULTIPLIER[reg] if allow else 0.0,
            min_setup_quality=DEFAULT_MIN_QUALITY[reg],
            event_blackout=event_blackout,
            sources=tuple(sources),
        )

    @classmethod
    def from_assessment(cls, a) -> "FundamentalView":
        """Depuis une ``intelligence.schemas.RegimeAssessment`` (import évité : couplage faible)."""
        srcs = tuple(dict.fromkeys(f"{s.source}:{s.identifier}@{s.available_at.isoformat()}" for s in a.sources))
        inv = tuple(i.description for i in a.invalidations)
        return cls(
            available=True,
            asof=a.asof,
            regime=normalize_regime(a.regime),
            bias=normalize_bias(a.bias),
            score=a.score,
            uncertainty=a.uncertainty,
            allow_new_entries=a.allow_new_entries,
            size_multiplier=a.size_multiplier,
            min_setup_quality=a.min_setup_quality,
            event_blackout=a.event_blackout,
            invalidations=inv,
            sources=srcs,
            overrides=tuple(a.overrides),
        )

    @classmethod
    def from_pit(cls, pit) -> "FundamentalView":
        """Depuis ``BarContext.pit`` (``PointInTimeView``) : ne voit que le publié à l'instant de décision."""
        obs = pit.get(SERIES_ASSESSMENT)
        if obs is not None:
            v = obs.value
            if isinstance(v, FundamentalView):
                return v
            if hasattr(v, "regime") and hasattr(v, "allow_new_entries"):
                return cls.from_assessment(v)
        reg = pit.get(SERIES_REGIME)
        bias = pit.get(SERIES_BIAS)
        if reg is None and bias is None:
            return cls.unavailable()
        used = [o for o in (reg, bias) if o is not None]
        asof = max(o.available_at for o in used)
        srcs = [f"{o.source or 'pit'}:{o.series}@{o.available_at.isoformat()}" for o in used]
        return cls.from_labels(reg.value if reg else None, bias.value if bias else None, asof=asof, sources=srcs)

    # ------------------------------------------------------------ lecture
    @property
    def confidence(self) -> float:
        """Confiance fondamentale ∈ [0 ; 1] (score et incertitude si connus, sinon par régime)."""
        if not self.available:
            return 0.0
        if self.score is not None:
            base = 0.5 + 0.5 * max(min(self.score, 1.0), -1.0)
        else:
            base = {RISK_ON: 0.75, NEUTRE: 0.5, RISK_OFF: 0.2}.get(self.regime or "", 0.0)
        if self.uncertainty is not None:
            base *= 1.0 - 0.5 * self.uncertainty
        return round(max(0.0, min(1.0, base)), 4)

    def describe(self) -> str:
        if not self.available:
            return "Fondamental indisponible"
        txt = f"Régime {self.regime}, biais {self.bias}"
        if self.score is not None:
            txt += f", score {self.score:+.2f}"
        if self.uncertainty is not None:
            txt += f", incertitude {self.uncertainty:.2f}"
        if self.event_blackout:
            txt += ", fenêtre d'annonce"
        return txt


def assessment_observation(assessment, source: str = "MarketRegimeEngine") -> Observation:
    """Publie une évaluation du Market Regime Engine dans une ``PointInTimeSource``.

    ``available_at`` = ``asof`` de l'évaluation : elle n'existe pas avant.
    """
    return Observation(SERIES_ASSESSMENT, assessment.asof, assessment, assessment.asof, source, meta={"forecast": True})


@dataclass
class FundamentalTracker:
    """Mémorise le régime précédent pour détecter les changements (sortie sur changement de régime)."""

    previous: FundamentalView | None = None
    history: list[tuple[datetime | None, str | None]] = field(default_factory=list)

    def update(self, view: FundamentalView) -> tuple[FundamentalView | None, bool]:
        prev = self.previous
        changed = prev is not None and prev.available and view.available and prev.regime != view.regime
        if prev is None or prev.regime != view.regime:
            self.history.append((view.asof, view.regime))
        self.previous = view
        return prev, changed
