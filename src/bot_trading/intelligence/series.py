"""Identifiants des séries et petites fonctions de calcul causales.

Les identifiants sont ceux de FRED/ALFRED quand la série y existe. Les séries
sans source gratuite (ISM, consensus) n'ont pas d'identifiant : elles ne sont
jamais simulées (voir DATA_SOURCES.md §8).
"""

from __future__ import annotations

import math
from statistics import fmean, pstdev

from bot_trading.intelligence.data import Observation
from bot_trading.intelligence.schemas import SourceRef

# --- Macro (ALFRED, mensuel sauf mention) ---------------------------------
CPI = "CPIAUCSL"
CORE_CPI = "CPILFESL"
PCE_PRICE = "PCEPI"
CORE_PCE = "PCEPILFE"
UNRATE = "UNRATE"
PAYROLLS = "PAYEMS"  # milliers d'emplois
CLAIMS = "ICSA"  # hebdomadaire
REAL_GDP = "GDPC1"  # trimestriel
INDPRO = "INDPRO"
RETAIL_SALES = "RSAFS"
CONSUMPTION = "PCE"
# Nowcast Cleveland Fed du glissement mensuel (%), archivé localement.
# Valeur pour la période du mois publié, disponible AVANT la publication.
NOWCAST_CORE_CPI_MOM = "CLEV_NOWCAST_CORE_CPI_MOM"
NOWCAST_CPI_MOM = "CLEV_NOWCAST_CPI_MOM"

# --- Taux et anticipations (quotidien) ------------------------------------
UST2Y = "DGS2"
UST10Y = "DGS10"
# Atlanta Fed MPT : baisse de taux attendue sur 12 mois, en points de base (> 0 = baisses).
MPT_CUTS_12M_BP = "ATL_MPT_CUTS_12M_BP"

# --- Intermarchés (quotidien) ---------------------------------------------
VIX = "VIXCLS"
VIX3M = "VXVCLS"
DOLLAR_BROAD = "DTWEXBGS"  # proxy DXY
HY_OAS = "BAMLH0A0HYM2"  # %, usage interne, 3 ans d'historique sur FRED

# --- Actions (optionnel, sources à brancher) --------------------------------
BREADTH_PCT_ABOVE_200D = "BREADTH_PCT_ABOVE_200D"  # 0–100
CYCLICAL_VS_DEFENSIVE = "SECTOR_CYCLICAL_VS_DEFENSIVE"  # ratio d'ETF sectoriels
SP500_EARNINGS_YIELD = "SP500_EARNINGS_YIELD"  # %, trailing

SOURCE_NAMES = {
    VIX: "Cboe / FRED",
    VIX3M: "Cboe / FRED",
    UST2Y: "Trésor US / FRED",
    UST10Y: "Trésor US / FRED",
    DOLLAR_BROAD: "FRED (Fed H.10)",
    HY_OAS: "FRED (ICE BofA, usage interne)",
    MPT_CUTS_12M_BP: "Atlanta Fed MPT",
    NOWCAST_CORE_CPI_MOM: "Cleveland Fed nowcast",
    NOWCAST_CPI_MOM: "Cleveland Fed nowcast",
}


def source_of(series_id: str) -> str:
    return SOURCE_NAMES.get(series_id, "ALFRED")


def ref(o: Observation, note: str = "") -> SourceRef:
    return SourceRef(
        source=o.source or source_of(o.series_id),
        identifier=o.series_id,
        available_at=o.available_at,
        period=o.period.isoformat(),
        value=o.value,
        note=note,
    )


def squash(x: float, scale: float = 1.0) -> float:
    """Projection bornée dans ]-1 ; 1[ (tanh)."""
    return math.tanh(x / scale) if scale > 0 else 0.0


def clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def values(obs: list[Observation]) -> list[float]:
    return [o.value for o in obs]


def pct_change(obs: list[Observation], lag: int) -> float | None:
    if len(obs) <= lag or obs[-1 - lag].value == 0:
        return None
    return (obs[-1].value / obs[-1 - lag].value - 1.0) * 100.0


def annualized(obs: list[Observation], months: int) -> float | None:
    """Variation annualisée (en %) sur ``months`` mois d'une série mensuelle de niveau."""
    if len(obs) <= months or obs[-1 - months].value <= 0:
        return None
    return ((obs[-1].value / obs[-1 - months].value) ** (12.0 / months) - 1.0) * 100.0


def diff(obs: list[Observation], lag: int) -> float | None:
    if len(obs) <= lag:
        return None
    return obs[-1].value - obs[-1 - lag].value


def mean_last(obs: list[Observation], n: int) -> float | None:
    if len(obs) < n or n <= 0:
        return None
    return fmean(o.value for o in obs[-n:])


def statistical_surprise(obs: list[Observation], window: int = 12) -> tuple[float, float] | None:
    """Surprise de la dernière variation mensuelle vs un modèle naïf (moyenne des
    ``window`` variations précédentes), normalisée par leur écart type.

    C'est le substitut du consensus (F12) : reproductible, sans look-ahead,
    mais ce n'est pas l'attente du marché. Renvoie (surprise brute en %, z-score).
    """
    if len(obs) < window + 2:
        return None
    changes = [(obs[i].value / obs[i - 1].value - 1.0) * 100.0 for i in range(len(obs) - window - 1, len(obs))]
    hist, last = changes[:-1], changes[-1]
    sd = pstdev(hist)
    raw = last - fmean(hist)
    return raw, (raw / sd if sd > 1e-9 else 0.0)
