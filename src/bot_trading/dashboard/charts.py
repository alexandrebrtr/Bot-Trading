"""Graphiques multi-timeframes : bougies, FVG, order blocks et BOS.

Les zones viennent des moteurs existants (``FVGEngine``, ``OrderBlockEngine``),
rejoués bougie par bougie : le graphique montre exactement ce que les moteurs
connaissaient à chaque clôture. Une zone commence à sa bougie d'origine, n'est
**confirmée** qu'à ``confirmed_index`` et s'arrête à son invalidation (ou à la
dernière bougie si elle est encore active).

``build_chart_data`` est pur (testable sans Plotly). ``candlestick_figure``
importe Plotly à la demande.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from bot_trading.analysis.fvg import FVGEngine, FVGParams
from bot_trading.analysis.order_blocks import OBParams, OrderBlockEngine
from bot_trading.core.models import Candle, Timeframe
from bot_trading.marketdata.candles import resample

# Ordre top-down de la stratégie (D1 → M5).
TOP_DOWN: tuple[Timeframe, ...] = (Timeframe.D1, Timeframe.H4, Timeframe.H1, Timeframe.M15, Timeframe.M5)
ROLES = {
    Timeframe.D1: "Tendance principale",
    Timeframe.H4: "Structure swing",
    Timeframe.H1: "Setup",
    Timeframe.M15: "Confirmation",
    Timeframe.M5: "Exécution",
}


@dataclass(frozen=True)
class ChartZone:
    kind: str  # "FVG" | "OB"
    bullish: bool
    start: datetime  # ouverture de la bougie d'origine
    confirmed_at: datetime  # clôture de la bougie de confirmation
    end: datetime  # invalidation ou fin du graphique
    top: float
    bottom: float
    active: bool
    mitigation: float
    touches: int
    quality: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        side = "haussier" if self.bullish else "baissier"
        return f"{self.kind} {side}"


@dataclass(frozen=True)
class BosMarker:
    time: datetime
    level: float
    bullish: bool


@dataclass(frozen=True)
class ChartData:
    timeframe: Timeframe
    candles: list[Candle]
    zones: list[ChartZone]
    bos: list[BosMarker]

    @property
    def empty(self) -> bool:
        return not self.candles


def _close_time(c: Candle, tf: Timeframe) -> datetime:
    return c.time + timedelta(minutes=tf.minutes)


def build_chart_data(
    candles: Sequence[Candle],
    tf: Timeframe,
    visible_bars: int = 200,
    include_inactive: bool = False,
    fvg_params: FVGParams | None = None,
    ob_params: OBParams | None = None,
    max_zones: int | None = 15,
    price_margin: float = 0.01,
) -> ChartData:
    """Rejoue toutes les bougies (préchauffage ATR/structure) et garde la fenêtre visible.

    Pour rester lisible, on ne garde que les zones qui recoupent la plage de prix
    visible (± ``price_margin``) et, au plus, les ``max_zones`` plus récemment confirmées.
    """
    candles = list(candles)
    if not candles:
        return ChartData(tf, [], [], [])
    fvg = FVGEngine(fvg_params, tf)
    ob = OrderBlockEngine(ob_params, tf)
    for c in candles:
        fvg.update(c)
        ob.update(c)

    first_visible = max(0, len(candles) - visible_bars)
    visible = candles[first_visible:]
    last_end = _close_time(candles[-1], tf)

    def end_of(state) -> datetime:
        if state.invalidated_index is not None:
            return _close_time(candles[state.invalidated_index], tf)
        return last_end

    zones: list[ChartZone] = []
    for f in fvg.fvgs:
        st = f.state
        if not include_inactive and not st.is_active:
            continue
        end = end_of(st)
        if end < visible[0].time:
            continue
        zones.append(
            ChartZone(
                "FVG", f.bullish, candles[f.first_index].time, _close_time(candles[f.confirmed_index], tf), end,
                f.top, f.bottom, st.is_active, st.max_mitigation, st.touches,
                {"taille/ATR": f.size_atr, "impulsion/ATR": f.impulse_atr, "BOS": f.with_bos},
            )
        )
    for o in ob.blocks:
        st = o.state
        if not include_inactive and not st.is_active:
            continue
        end = end_of(st)
        if end < visible[0].time:
            continue
        zones.append(
            ChartZone(
                "OB", o.bullish, candles[o.index].time, _close_time(candles[o.confirmed_index], tf), end,
                o.top, o.bottom, st.is_active, st.max_mitigation, st.touches,
                {"déplacement/ATR": o.displacement_atr, "avec FVG": o.has_fvg, "niveau BOS": o.bos_level},
            )
        )
    lo = min(c.low for c in visible) * (1 - price_margin)
    hi = max(c.high for c in visible) * (1 + price_margin)
    zones = [z for z in zones if z.top >= lo and z.bottom <= hi]
    if max_zones is not None and len(zones) > max_zones:
        zones = sorted(zones, key=lambda z: z.confirmed_at)[-max_zones:]
    bos = [
        BosMarker(_close_time(candles[e.index], tf), e.level, e.bullish)
        for e in fvg.structure.bos
        if e.index >= first_visible
    ]
    zones.sort(key=lambda z: z.start)
    return ChartData(tf, visible, zones, bos)


def multi_timeframe(base: Sequence[Candle], base_tf: Timeframe) -> dict[Timeframe, list[Candle]]:
    """Agrège un timeframe de base vers les timeframes supérieurs (bougies closes seulement)."""
    out: dict[Timeframe, list[Candle]] = {base_tf: list(base)}
    for tf in TOP_DOWN:
        if tf.minutes > base_tf.minutes:
            out[tf] = resample(base, tf)
    return out


def market_closures(candles: Sequence[Candle], tf: Timeframe, max_points: int = 5000) -> list[datetime]:
    """Heures d'ouverture des bougies absentes (week-ends, pauses), à masquer sur l'axe du temps.

    Limité à ``max_points`` valeurs pour garder le graphique léger.
    """
    step = timedelta(minutes=tf.minutes)
    out: list[datetime] = []
    for a, b in zip(candles, candles[1:]):
        t = a.time + step
        while t < b.time and len(out) < max_points:
            out.append(t)
            t += step
    return out


def zone_rows(data: ChartData) -> list[dict]:
    """Table des zones pour l'affichage, les plus récentes d'abord."""
    rows = []
    for z in sorted(data.zones, key=lambda z: z.confirmed_at, reverse=True):
        row = {
            "type": z.label,
            "confirmée": z.confirmed_at,
            "bas": round(z.bottom, 2),
            "haut": round(z.top, 2),
            "mitigation": f"{z.mitigation:.0%}",
            "retests": z.touches,
            "active": z.active,
        }
        for k, v in z.quality.items():
            row[k] = round(v, 2) if isinstance(v, float) else v
        rows.append(row)
    return rows


# ------------------------------------------------------------------ Plotly

COLORS = {
    ("FVG", True): "rgba(38, 166, 154, {a})",
    ("FVG", False): "rgba(239, 83, 80, {a})",
    ("OB", True): "rgba(66, 133, 244, {a})",
    ("OB", False): "rgba(255, 152, 0, {a})",
}


def _tz(t: datetime, tz):
    return t.astimezone(tz) if tz is not None and t.tzinfo is not None else t


def candlestick_figure(data: ChartData, title: str = "", height: int = 560, tz=None):
    """``tz`` : fuseau d'affichage (``zoneinfo.ZoneInfo``), UTC si ``None``."""
    import plotly.graph_objects as go

    fig = go.Figure()
    fig.add_trace(
        go.Candlestick(
            x=[_tz(c.time, tz) for c in data.candles],
            open=[c.open for c in data.candles],
            high=[c.high for c in data.candles],
            low=[c.low for c in data.candles],
            close=[c.close for c in data.candles],
            name=data.timeframe.value,
            increasing_line_color="#26a69a",
            decreasing_line_color="#ef5350",
        )
    )
    x_min = data.candles[0].time if data.candles else None
    for z in data.zones:
        alpha = 0.22 if z.active else 0.08
        color = COLORS[(z.kind, z.bullish)]
        fig.add_shape(
            type="rect",
            x0=_tz(max(z.start, x_min) if x_min else z.start, tz),
            x1=_tz(z.end, tz),
            y0=z.bottom,
            y1=z.top,
            fillcolor=color.format(a=alpha),
            line=dict(color=color.format(a=0.7), width=1, dash="solid" if z.kind == "FVG" else "dot"),
            layer="below",
        )
    # Légende des zones (traces invisibles).
    for (kind, bullish), color in COLORS.items():
        if any(z.kind == kind and z.bullish == bullish for z in data.zones):
            fig.add_trace(
                go.Scatter(
                    x=[None], y=[None], mode="markers",
                    marker=dict(size=12, color=color.format(a=0.6), symbol="square"),
                    name=f"{kind} {'haussier' if bullish else 'baissier'}",
                )
            )
    if data.bos:
        fig.add_trace(
            go.Scatter(
                x=[_tz(b.time, tz) for b in data.bos],
                y=[b.level for b in data.bos],
                mode="markers",
                marker=dict(symbol=["triangle-up" if b.bullish else "triangle-down" for b in data.bos], size=9,
                            color=["#26a69a" if b.bullish else "#ef5350" for b in data.bos]),
                name="BOS (clôture)",
            )
        )
    if data.candles:
        lo = min(c.low for c in data.candles)
        hi = max(c.high for c in data.candles)
        pad = (hi - lo) * 0.05 or 1.0
        fig.update_yaxes(range=[lo - pad, hi + pad])
        breaks = market_closures(data.candles, data.timeframe)
        if breaks:
            fig.update_xaxes(rangebreaks=[dict(values=[_tz(t, tz) for t in breaks], dvalue=data.timeframe.minutes * 60_000)])
    fig.update_layout(
        title=title,
        height=height,
        xaxis_rangeslider_visible=False,
        margin=dict(l=10, r=10, t=40, b=10),
        legend=dict(orientation="h", y=1.02, x=0),
    )
    return fig


def line_figure(points: Sequence[tuple[datetime, float]], name: str, height: int = 320, percent: bool = False, fill: bool = False, tz=None):
    import plotly.graph_objects as go

    fig = go.Figure(
        go.Scatter(
            x=[_tz(t, tz) for t, _ in points], y=[v for _, v in points], mode="lines", name=name,
            fill="tozeroy" if fill else None,
        )
    )
    fig.update_layout(height=height, margin=dict(l=10, r=10, t=30, b=10), showlegend=False)
    if percent:
        fig.update_yaxes(tickformat=".0%")
    return fig
