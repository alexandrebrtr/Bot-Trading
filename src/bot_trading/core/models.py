"""Types de base partagés par tous les moteurs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Direction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class Timeframe(str, Enum):
    """Timeframes autorisés par la stratégie (M5, M15, H1, H4, D1)."""

    M5 = "M5"
    M15 = "M15"
    H1 = "H1"
    H4 = "H4"
    D1 = "D1"

    @property
    def minutes(self) -> int:
        return {"M5": 5, "M15": 15, "H1": 60, "H4": 240, "D1": 1440}[self.value]

    @property
    def capital_resolution(self) -> str:
        """Nom de la résolution dans l'API Capital.com (GET /api/v1/prices/{epic})."""
        return {
            "M5": "MINUTE_5",
            "M15": "MINUTE_15",
            "H1": "HOUR",
            "H4": "HOUR_4",
            "D1": "DAY",
        }[self.value]


@dataclass(frozen=True, slots=True)
class Candle:
    """Bougie OHLC en prix *mid* ou *bid* selon la source.

    ``time`` est l'heure d'ouverture de la bougie (UTC). La bougie n'est
    exploitable qu'après sa clôture, soit ``time + timeframe``.
    """

    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def __post_init__(self) -> None:
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError(f"Bougie incohérente à {self.time}: {self}")
        if self.high < self.low:
            raise ValueError(f"high < low à {self.time}")

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def range(self) -> float:
        return self.high - self.low


@dataclass(frozen=True, slots=True)
class Quote:
    """Cotation bid/ask à un instant donné."""

    time: datetime
    bid: float
    ask: float

    def __post_init__(self) -> None:
        if self.ask < self.bid:
            raise ValueError("ask < bid")

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """Intention d'ordre produite par la stratégie, avant passage au Risk Engine."""

    epic: str
    direction: Direction
    size: float
    stop_level: float | None
    guaranteed_stop: bool
    take_profit_level: float | None = None
    reason: str = ""


@dataclass(slots=True)
class Position:
    deal_id: str
    epic: str
    direction: Direction
    size: float
    entry_price: float
    opened_at: datetime
    stop_level: float
    guaranteed_stop: bool
    margin_eur: float
    take_profit_level: float | None = None
    funding_paid_eur: float = 0.0
    last_funding_at: datetime | None = None
    meta: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    deal_id: str
    epic: str
    direction: Direction
    size: float
    entry_price: float
    exit_price: float
    opened_at: datetime
    closed_at: datetime
    exit_reason: str
    gross_pnl_eur: float
    funding_eur: float
    fees_eur: float

    @property
    def net_pnl_eur(self) -> float:
        return self.gross_pnl_eur - self.funding_eur - self.fees_eur
