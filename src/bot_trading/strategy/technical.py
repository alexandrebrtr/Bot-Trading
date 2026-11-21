"""Technical Engine top-down sur cinq timeframes (STRATEGY.md §4, mission §5 à §8).

Rôle : **secondaire**. Il ne décide jamais de la direction ; il dit si le
contexte technique permet une entrée longue, où, avec quelle invalidation, et
avec quelle qualité. Le Decision Engine reste maître (fondamental dominant).

| Étage | Timeframe | Ce qui est calculé |
|---|---|---|
| Tendance | D1 | EMA (T1, 🎛️ 50–200), pente, structure HH/HL, extension (close − EMA) / ATR (T3), régime de volatilité, résistances majeures |
| Structure | H4 | Dernier BOS (T2), dernier swing low (invalidation swing), OB et FVG majeurs |
| Setup | H1 (et H4) | FVG et OB haussiers actifs, qualifiés et notés ∈ [0 ; 1] (G3, O1–O4) |
| Confirmation | M15 | Retour dans la zone + réaction mesurable (mèche ou micro-BOS) (E2, G7) |
| Exécution | M5 | Prix d'entrée, distance du stop, spread (E3–E5) |

Causalité : chaque timeframe n'est alimenté qu'avec des bougies **closes**
(base : ``ctx.candle`` ; supérieurs : ``ctx.mtf.closed``). Les moteurs FVG/OB et
de structure sont incrémentaux. Un timeframe plus fin que la base n'existe
pas : la confirmation se fait alors sur le plus fin disponible (noté dans
``TechnicalSnapshot.confirmation_tf``).

Tous les seuils sont des paramètres 🎛️ à optimiser par walk-forward : aucune
valeur par défaut n'est présentée comme rentable.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from bot_trading.analysis.fvg import FairValueGap, FVGEngine, FVGParams
from bot_trading.analysis.indicators import IncrementalATR, IncrementalEMA
from bot_trading.analysis.order_blocks import OBParams, OrderBlock, OrderBlockEngine
from bot_trading.analysis.structure import StructureTracker, SwingKind
from bot_trading.core.models import Candle, Timeframe

ALL_TFS = (Timeframe.M5, Timeframe.M15, Timeframe.H1, Timeframe.H4, Timeframe.D1)
ENTRY_MODES = ("touch", "half", "full_reject", "bos")


@dataclass(frozen=True)
class TechnicalParams:
    atr_period: int = 14
    swing_left: int = 2
    swing_right: int = 2
    ema_periods: tuple[int, ...] = (20, 50, 100, 200)
    # T1 : EMA D1 de tendance (🎛️ 50–200) et pente sur n bougies D1.
    d1_trend_ema: int = 50
    d1_slope_lookback: int = 5
    require_d1_structure: bool = True  # refuse une structure LH/LL même au-dessus de l'EMA
    # T3 : extension maximale (close − EMA D1) / ATR D1 (🎛️ k ∈ [1 ; 4]).
    max_extension_atr: float = 3.0
    # Volatilité D1 : percentile de l'ATR sur n jours (information, filtre optionnel).
    vol_lookback: int = 100
    max_vol_percentile: float | None = None
    # Zones (G4, G5, O2, O3)
    fvg_min_size_atr: float = 0.3
    ob_min_displacement_atr: float = 1.0
    ob_max_retests: int = 2
    setup_timeframes: tuple[Timeframe, ...] = (Timeframe.H1, Timeframe.H4)
    max_age_bars: dict = field(default_factory=lambda: {Timeframe.H1: 120, Timeframe.H4: 50})
    use_order_blocks: bool = True
    use_fvgs: bool = True
    # Confirmation (E2, G7) — variante d'entrée testée dans EXP-03.
    entry_mode: str = "half"
    confirm_tf: Timeframe = Timeframe.M15
    confirm_min_wick_ratio: float = 0.2  # mèche basse / range de la bougie de confirmation
    bos_confirm_window: int = 6  # bougies de confirmation pendant lesquelles la zone doit avoir été touchée (mode bos)
    # Stop (E4) : bas de zone − buffer × ATR du timeframe du setup.
    stop_buffer_atr: float = 0.3
    # Mémoire : élagage des zones invalidées ou trop anciennes.
    prune_every: int = 50

    def __post_init__(self) -> None:
        if self.entry_mode not in ENTRY_MODES:
            raise ValueError(f"entry_mode inconnu : {self.entry_mode}")


class TimeframeState:
    """État incrémental d'un timeframe : ATR, EMA, structure, FVG, OB, résistances."""

    def __init__(self, tf: Timeframe, p: TechnicalParams) -> None:
        self.tf = tf
        self.p = p
        self.times: list[datetime] = []
        self.last: Candle | None = None
        self.atr = IncrementalATR(p.atr_period)
        periods = sorted(set(p.ema_periods) | ({p.d1_trend_ema} if tf == Timeframe.D1 else set()))
        self.emas = {n: IncrementalEMA(n) for n in periods}
        self.ema_hist: dict[int, deque] = {n: deque(maxlen=max(p.d1_slope_lookback, 1) + 1) for n in periods}
        self.structure = StructureTracker(p.swing_left, p.swing_right)
        self.fvg = FVGEngine(FVGParams(atr_period=p.atr_period, min_size_atr=p.fvg_min_size_atr, swing_left=p.swing_left, swing_right=p.swing_right), tf)
        self.ob = OrderBlockEngine(OBParams(atr_period=p.atr_period, swing_left=p.swing_left, swing_right=p.swing_right, min_displacement_atr=p.ob_min_displacement_atr), tf)
        self.atr_hist: deque[float] = deque(maxlen=p.vol_lookback)
        self.unbroken_highs: list[tuple[int, float]] = []  # swings hauts non dépassés en clôture
        self.new_bos_bull = False  # BOS haussier sur la dernière bougie
        self.updated_at: datetime | None = None  # clôture de la dernière bougie reçue

    @property
    def index(self) -> int:
        return len(self.times) - 1

    @property
    def atr_value(self) -> float | None:
        return self.atr.values[-1] if self.atr.values else None

    def update(self, c: Candle) -> None:
        self.times.append(c.time)
        self.last = c
        self.atr.update(c)
        a = self.atr_value
        if a is not None:
            self.atr_hist.append(a)
        for n, e in self.emas.items():
            v = e.update(c.close)
            if v is not None:
                self.ema_hist[n].append(v)
        n_swings = len(self.structure.swings)
        events = self.structure.update(c)
        self.new_bos_bull = any(e.bullish for e in events)
        for s in self.structure.swings[n_swings:]:
            if s.kind == SwingKind.HIGH:
                self.unbroken_highs.append((s.index, s.price))
        if self.unbroken_highs:
            self.unbroken_highs = [(i, px) for i, px in self.unbroken_highs if c.close <= px]
        self.fvg.update(c)
        self.ob.update(c)
        self.updated_at = c.time + timedelta(minutes=self.tf.minutes)
        if self.p.prune_every and self.index % self.p.prune_every == 0:
            self._prune()

    def _prune(self) -> None:
        keep = 4 * max(self.p.max_age_bars.values(), default=100)
        i = self.index
        self.fvg.fvgs = [f for f in self.fvg.fvgs if f.state.is_active and i - f.confirmed_index <= keep]
        self.ob.blocks = [o for o in self.ob.blocks if o.state.is_active and i - o.confirmed_index <= keep]

    # ------------------------------------------------------------ lectures
    def ema(self, n: int) -> float | None:
        return self.emas[n].value if n in self.emas else None

    def ema_slope_up(self, n: int) -> bool | None:
        h = self.ema_hist.get(n)
        if not h or len(h) < h.maxlen:
            return None
        return h[-1] > h[0]

    def last_swings(self, kind: SwingKind, k: int = 2) -> list[float]:
        return [s.price for s in self.structure.swings if s.kind == kind][-k:]

    def structure_trend(self) -> str:
        hs, ls = self.last_swings(SwingKind.HIGH), self.last_swings(SwingKind.LOW)
        if len(hs) < 2 or len(ls) < 2:
            return "unknown"
        if hs[1] > hs[0] and ls[1] > ls[0]:
            return "bullish"
        if hs[1] < hs[0] and ls[1] < ls[0]:
            return "bearish"
        return "neutral"

    def last_bos(self) -> tuple[bool, datetime] | None:
        if not self.structure.bos:
            return None
        e = self.structure.bos[-1]
        return e.bullish, self.times[e.index]

    def last_swing_low(self) -> float | None:
        lows = self.last_swings(SwingKind.LOW, 1)
        return lows[0] if lows else None

    def swing_lows_since(self, t: datetime) -> list[float]:
        return [s.price for s in self.structure.swings if s.kind == SwingKind.LOW and self.times[s.confirmed_index] >= t]

    def vol_percentile(self) -> float | None:
        a = self.atr_value
        if a is None or len(self.atr_hist) < 20:
            return None
        return sum(1 for x in self.atr_hist if x <= a) / len(self.atr_hist)


@dataclass(frozen=True)
class Setup:
    kind: str  # "FVG" | "OB"
    timeframe: Timeframe
    top: float
    bottom: float
    quality: float  # [0 ; 1]
    components: dict
    age_bars: int
    stop: float
    confirmed: bool
    confirmation: str
    confirmed_at: datetime | None

    @property
    def name(self) -> str:
        return f"{self.kind}_{self.timeframe.value}"

    @property
    def midpoint(self) -> float:
        return (self.top + self.bottom) / 2


@dataclass(frozen=True)
class TechnicalSnapshot:
    time: datetime
    available: tuple[Timeframe, ...]
    d1_trend: str  # bullish / bearish / neutral / unknown
    d1_close: float | None
    d1_ema: float | None
    d1_atr: float | None
    extension_atr: float | None
    vol_percentile: float | None
    h4_structure: str  # dernier BOS : bullish / bearish / unknown
    h4_invalidation: float | None  # dernier swing low H4
    confirmation_tf: Timeframe
    setups: tuple[Setup, ...]
    best: Setup | None
    notes: tuple[str, ...]
    ema_distance_atr: dict = field(default_factory=dict)  # (close − EMA n) / ATR, par TF

    @property
    def confirmed_setups(self) -> tuple[Setup, ...]:
        return tuple(s for s in self.setups if s.confirmed)


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


class TechnicalEngine:
    """Alimenté à chaque clôture de la bougie de base (``update``), interrogé par ``snapshot``."""

    def __init__(self, params: TechnicalParams | None = None, base: Timeframe = Timeframe.H1) -> None:
        self.p = params or TechnicalParams()
        self.base = base
        self.states: dict[Timeframe, TimeframeState] = {tf: TimeframeState(tf, self.p) for tf in ALL_TFS if tf.minutes >= base.minutes}
        self._seen: dict[Timeframe, int] = {tf: 0 for tf in self.states}
        self.now: datetime | None = None

    # ------------------------------------------------------------ alimentation
    def update(self, ctx) -> None:
        """``ctx`` : ``BarContext`` du backtester (ou équivalent paper trading)."""
        self.update_from(ctx.candle, ctx.now, ctx.mtf.closed)

    def update_from(self, base_candle: Candle, now: datetime, closed: dict[Timeframe, list[Candle]]) -> None:
        self.now = now
        for tf, st in self.states.items():
            if tf == self.base:
                st.update(base_candle)
                self._seen[tf] += 1
                continue
            lst = closed.get(tf, [])
            for c in lst[self._seen[tf]:]:
                st.update(c)
            self._seen[tf] = len(lst)

    def state(self, tf: Timeframe) -> TimeframeState | None:
        return self.states.get(tf)

    @property
    def confirmation_tf(self) -> Timeframe:
        return self.p.confirm_tf if self.p.confirm_tf in self.states else self.base

    # ------------------------------------------------------------ analyse
    def snapshot(self) -> TechnicalSnapshot:
        notes: list[str] = []
        d1 = self.states.get(Timeframe.D1)
        h4 = self.states.get(Timeframe.H4)
        d1_trend, d1_close, d1_ema, d1_atr, ext, volp = "unknown", None, None, None, None, None
        if d1 is not None and d1.last is not None:
            d1_close = d1.last.close
            d1_ema = d1.ema(self.p.d1_trend_ema)
            d1_atr = d1.atr_value
            slope = d1.ema_slope_up(self.p.d1_trend_ema)
            st = d1.structure_trend()
            volp = d1.vol_percentile()
            if d1_ema is None or slope is None:
                notes.append(f"D1 : EMA{self.p.d1_trend_ema} en préchauffage")
            else:
                above = d1_close > d1_ema
                struct_ok = st != "bearish" if self.p.require_d1_structure else True
                if above and slope and struct_ok:
                    d1_trend = "bullish"
                elif not above and not slope:
                    d1_trend = "bearish"
                else:
                    d1_trend = "neutral"
                notes.append(f"D1 : close {d1_close:.1f} {'>' if above else '<'} EMA{self.p.d1_trend_ema} {d1_ema:.1f}, pente {'+' if slope else '−'}, structure {st}")
                if d1_atr:
                    ext = (d1_close - d1_ema) / d1_atr
        h4_struct, h4_inv = "unknown", None
        if h4 is not None:
            lb = h4.last_bos()
            if lb is not None:
                h4_struct = "bullish" if lb[0] else "bearish"
            h4_inv = h4.last_swing_low()
        ema_dist = {}
        for tf, st in self.states.items():
            a = st.atr_value
            if st.last is None or not a:
                continue
            ema_dist[tf.value] = {n: round((st.last.close - v) / a, 3) for n, e in st.emas.items() if (v := e.value) is not None}

        setups = tuple(self._setups(h4_struct))
        confirmed = [s for s in setups if s.confirmed]
        best = max(confirmed, key=lambda s: (s.quality, s.stop)) if confirmed else None
        return TechnicalSnapshot(
            time=self.now,  # type: ignore[arg-type]
            available=tuple(self.states),
            d1_trend=d1_trend,
            d1_close=d1_close,
            d1_ema=d1_ema,
            d1_atr=d1_atr,
            extension_atr=ext,
            vol_percentile=volp,
            h4_structure=h4_struct,
            h4_invalidation=h4_inv,
            confirmation_tf=self.confirmation_tf,
            setups=setups,
            best=best,
            notes=tuple(notes),
            ema_distance_atr=ema_dist,
        )

    def _setups(self, h4_struct: str) -> list[Setup]:
        conf = self.states[self.confirmation_tf]
        c = conf.last
        if c is None or conf.updated_at != self.now:
            return []  # pas de nouvelle bougie de confirmation à cette clôture
        out: list[Setup] = []
        for tf in self.p.setup_timeframes:
            st = self.states.get(tf)
            if st is None or st.atr_value is None:
                continue
            max_age = self.p.max_age_bars.get(tf, 100)
            i = st.index
            zones: list[tuple[str, object]] = []
            if self.p.use_fvgs:
                zones += [("FVG", f) for f in st.fvg.active(True)]
            if self.p.use_order_blocks:
                zones += [("OB", o) for o in st.ob.active(True) if o.state.touches <= self.p.ob_max_retests]
            bull_zones = [z for _, z in zones]
            for kind, z in zones:
                zs = z.state  # type: ignore[attr-defined]
                age = zs.age(i)
                if age < 1 or age > max_age:
                    continue
                ok, how = self._confirm(zs, c, conf)
                if not ok and how == "":
                    continue  # zone non touchée : pas un setup en jeu
                q, comp = self._quality(kind, z, tf, age, max_age, h4_struct, bull_zones, st.atr_value)
                stop = zs.bottom - self.p.stop_buffer_atr * st.atr_value
                out.append(Setup(kind, tf, zs.top, zs.bottom, q, comp, age, stop, ok, how, self.now if ok else None))
        return out

    def _confirm(self, zs, c: Candle, conf: TimeframeState) -> tuple[bool, str]:
        """(confirmé, description). Description vide = zone pas touchée par la bougie de confirmation."""
        size = zs.top - zs.bottom
        if size <= 0:
            return False, ""
        pen = _clip01((zs.top - c.low) / size)
        mode = self.p.entry_mode
        if mode == "bos":
            # micro-BOS haussier sur le TF de confirmation, zone touchée récemment, clôture au-dessus du bas.
            if not conf.new_bos_bull:
                return (False, "zone touchée, pas de micro-BOS") if pen > 0 else (False, "")
            recent = list(conf.fvg.candles[-self.p.bos_confirm_window:])
            touched = any(x.low <= zs.top for x in recent)
            if touched and c.close > zs.bottom:
                return True, f"micro-BOS {conf.tf.value} après retour dans la zone"
            return False, ""
        if pen <= 0:
            return False, ""
        mitig = max(zs.max_mitigation, pen)
        if c.close < zs.bottom:
            return False, "clôture sous la zone"
        rng = c.range
        wick = (min(c.open, c.close) - c.low) / rng if rng > 0 else 0.0
        reaction = c.is_bullish and c.close >= (zs.top + zs.bottom) / 2 and wick >= self.p.confirm_min_wick_ratio
        reaction = reaction or (conf.new_bos_bull and c.close > zs.top)
        if mode == "half" and mitig < 0.5:
            return False, f"mitigation {mitig:.0%} < 50 %"
        if mode == "full_reject":
            if mitig < 1.0:
                return False, f"mitigation {mitig:.0%} < 100 %"
            if c.close <= zs.top:
                return False, "pas de clôture au-dessus de la zone après mitigation complète"
        if not reaction:
            return False, f"zone touchée ({mitig:.0%}), pas de réaction mesurable en {conf.tf.value}"
        return True, f"réaction {conf.tf.value} (mèche {wick:.0%}, mitigation {mitig:.0%})"

    def _quality(self, kind: str, z, tf: Timeframe, age: int, max_age: int, h4_struct: str, bull_zones: list, atr: float) -> tuple[float, dict]:
        """Score G3/O1–O4 ∈ [0 ; 1] : somme pondérée de composantes explicables (pondérations 🎛️)."""
        zs = z.state
        if kind == "FVG":
            size_c = _clip01((z.size_atr or 0.0) / 1.0)
            impulse_c = _clip01((z.impulse_atr or 0.0) / 2.0)
            confl = 1.0 if z.with_bos else 0.0
        else:
            size_c = _clip01(((z.top - z.bottom) / atr if atr else 0.0) / 1.0)
            impulse_c = _clip01((z.displacement_atr or 0.0) / 3.0)
            confl = 1.0 if z.has_fvg else 0.0
        overlap = any(o is not z and o.state.bottom < zs.top and o.state.top > zs.bottom for o in bull_zones)
        comp = {
            "taille_atr": (0.15, size_c),
            "impulsion": (0.20, impulse_c),
            "bos_ou_fvg": (0.15, confl),
            "timeframe": (0.10, {Timeframe.H4: 1.0, Timeframe.H1: 0.7}.get(tf, 0.5)),
            "fraicheur": (0.10, _clip01(1 - age / max_age)),
            "retests": (0.10, {0: 1.0, 1: 1.0, 2: 0.5}.get(zs.touches, 0.0)),
            "confluence_zone": (0.10, 1.0 if overlap else 0.0),
            "structure_h4": (0.10, 1.0 if h4_struct == "bullish" else 0.0),
        }
        q = sum(w * v for w, v in comp.values())
        return round(q, 4), {k: round(v, 3) for k, (_, v) in comp.items()}


def realized_vol(closes: list[float], periods_per_year: float) -> float | None:
    if len(closes) < 3:
        return None
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var * periods_per_year)


__all__ = [
    "ALL_TFS",
    "ENTRY_MODES",
    "FairValueGap",
    "OrderBlock",
    "Setup",
    "TechnicalEngine",
    "TechnicalParams",
    "TechnicalSnapshot",
    "TimeframeState",
    "realized_vol",
]
