"""Analyse reproductible du journal de trading (TJ_2026.xlsx).

Usage :
    python research/journal_analysis.py chemin/vers/TJ_2026.xlsx

Le journal est une donnée personnelle : il ne doit jamais être commité.
Le script ne lit que l'onglet « Trades » et imprime les statistiques
reprises dans docs/JOURNAL_ANALYSIS.md.

Segments :
    A_Capital_US500   compte Live, symbole SP500 (CFD US500 de Capital.com)
    B_Live_autres     compte Live, autres actifs (UK100, US100, crypto)
    C_PropFirm_US500  prop firms (réel et évaluation) sur ES/MES
    C_PropFirm_autres prop firms sur UK100, US100, MNQ
    D_Paper_Demo      comptes Paper et Demo
"""

from __future__ import annotations

import sys

import numpy as np
import pandas as pd

UNDERLYING = {
    "SP500": "US500",
    "ESU6": "US500",
    "MESZ6": "US500",
    "MESU6": "US500",
    "MES": "US500",
    "UK100": "UK100",
    "USNDAQ100": "NDX",
    "MNQZ6": "NDX",
    "SOLANA": "CRYPTO",
}


def segment(row: pd.Series) -> str:
    account, symbol = row["Type Compte"], row["Symbole"]
    if account == "Live" and symbol == "SP500":
        return "A_Capital_US500"
    if account == "Live":
        return "B_Live_autres"
    if account in ("Prop Firm", "Prop Firm Demo"):
        return "C_PropFirm_" + ("US500" if row["under"] == "US500" else "autres")
    return "D_Paper_Demo"


def load(path: str) -> pd.DataFrame:
    df = pd.read_excel(path, sheet_name="Trades").rename(columns={"-": "Date"})
    # Les lignes sans taille ni prix sont des dividendes, dépôts ou retraits.
    df = df[df["Taille Position"].notna() & df["Prix Entrée"].notna()].copy()
    df["under"] = df["Symbole"].map(UNDERLYING)
    df["segment"] = df.apply(segment, axis=1)
    df["pnl"] = pd.to_numeric(df["P&L Net ($)"], errors="coerce")
    long = df["Direction"] == "LONG"
    df["pts"] = np.where(long, df["Prix Sortie"] - df["Prix Entrée"], df["Prix Entrée"] - df["Prix Sortie"])
    df["sl_pts"] = (df["Prix Entrée"] - df["Stop Loss"]).abs()
    df["tp_pts"] = (df["Take Profit"] - df["Prix Entrée"]).abs()
    df["sl_pct"] = df["sl_pts"] / df["Prix Entrée"] * 100
    return df


def summary(g: pd.DataFrame) -> pd.Series:
    wins, losses = g[g.pnl > 0], g[g.pnl < 0]
    return pd.Series(
        {
            "trades": len(g),
            "long_%": (g.Direction == "LONG").mean() * 100,
            "win_%": (g.pnl > 0).mean() * 100,
            "pnl_net": g.pnl.sum(),
            "gain_moyen": wins.pnl.mean(),
            "perte_moyenne": losses.pnl.mean(),
            "pire_perte": g.pnl.min(),
            "profit_factor": wins.pnl.sum() / -losses.pnl.sum() if len(losses) else np.inf,
            "pts_median": g.pts.median(),
            "sl_renseigne_%": g["Stop Loss"].notna().mean() * 100,
            "sl_median_%_prix": g.sl_pct.median(),
            "tp_median_pts": g.tp_pts.median(),
            "M5_%": (g.Timeframe == "M5").mean() * 100,
        }
    )


def main(path: str) -> None:
    pd.set_option("display.width", 200)
    df = load(path)
    print(df.groupby("segment").apply(summary).T.round(2).to_string())
    print()
    print(df.groupby(["segment", "Setup"]).size().unstack(fill_value=0).to_string())
    print()
    us = df[(df.under == "US500") & df.pts.notna()]
    wins, losses = us[us.pts > 0], us[us.pts <= 0]
    print(
        f"US500 tous comptes : {len(us)} trades, win {100 * (us.pts > 0).mean():.1f} %, "
        f"gain moyen {wins.pts.mean():.2f} pts (médiane {wins.pts.median():.2f}), "
        f"perte moyenne {losses.pts.mean():.2f} pts, espérance {us.pts.mean():.2f} pts"
    )
    prop = df[df.segment == "C_PropFirm_US500"]
    print(
        f"Prop firm US500 : 5 pires pertes {prop.pnl.nsmallest(5).sum():.2f}, "
        f"P&L total {prop.pnl.sum():.2f}"
    )
    print(prop.groupby("Type Compte").pnl.agg(["size", "sum"]).to_string())


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
