"""Faisabilité d'un trade US500 à 0,01 lot sur un compte Capital.com de 20 EUR.

Usage :
    python research/feasibility.py [--price 7520] [--eurusd 1.16] [--size 0.01]

Toutes les hypothèses non vérifiées sont des arguments, pour pouvoir les remplacer
par les valeurs lues dans GET /api/v1/markets/US500 (dealingRules, overnightFee).
"""

from __future__ import annotations

import argparse


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--price", type=float, default=7520.0, help="cours US500 (USD)")
    p.add_argument("--eurusd", type=float, default=1.16, help="1 EUR = x USD")
    p.add_argument("--size", type=float, default=0.01, help="taille (1 = 1 USD par point)")
    p.add_argument("--capital", type=float, default=20.0, help="capital du compte (EUR)")
    p.add_argument("--margin", type=float, default=0.05, help="marge (5 % = levier 20:1)")
    p.add_argument("--max_loss_eur", type=float, default=1.70, help="perte max visée (EUR)")
    p.add_argument("--spread_pts", type=float, default=0.6, help="spread (points), NON VÉRIFIÉ")
    p.add_argument("--sofr", type=float, default=0.0375, help="SOFR annuel, NON VÉRIFIÉ")
    p.add_argument("--fee", type=float, default=0.04, help="frais Capital.com annuels (4 %)")
    p.add_argument("--gsl_premium", type=float, default=0.0003, help="prime GSL, NON VÉRIFIÉE")
    p.add_argument("--fx_fee", type=float, default=0.007, help="frais de conversion 0,7 %")
    a = p.parse_args()

    eur_per_pt = a.size / a.eurusd
    notional = a.price * a.size / a.eurusd
    margin = notional * a.margin
    loss_cap_50 = 0.5 * margin
    dist_50 = loss_cap_50 / eur_per_pt
    dist_target = a.max_loss_eur / eur_per_pt
    gsl_fee = a.gsl_premium * a.price * a.size / a.eurusd
    spread_cost = a.spread_pts * eur_per_pt
    funding_night = notional * (a.fee + a.sofr) / 360
    fx_on_loss = loss_cap_50 * a.fx_fee
    full_loss = loss_cap_50 + gsl_fee + spread_cost + fx_on_loss

    rows = [
        ("Valeur du point", f"{eur_per_pt:.5f} EUR"),
        ("Nominal", f"{notional:.2f} EUR"),
        ("Marge engagée", f"{margin:.2f} EUR ({100 * margin / a.capital:.1f} % du capital)"),
        ("Perte max = 50 % de la marge", f"{loss_cap_50:.2f} EUR"),
        ("  -> distance de stop max", f"{dist_50:.0f} pts ({100 * dist_50 / a.price:.2f} % du prix)"),
        (f"Perte visée {a.max_loss_eur:.2f} EUR", f"distance {dist_target:.0f} pts ({100 * dist_target / a.price:.2f} %)"),
        ("Prime GSL si déclenché", f"{gsl_fee:.3f} EUR"),
        ("Spread aller-retour", f"{spread_cost:.4f} EUR"),
        ("Financement long par nuit", f"{funding_night:.4f} EUR"),
        ("Financement 30 nuits", f"{30 * funding_night:.3f} EUR"),
        ("Conversion 0,7 % sur la perte max", f"{fx_on_loss:.3f} EUR"),
        ("Perte totale si stop touché", f"{full_loss:.2f} EUR ({100 * full_loss / a.capital:.1f} % du capital)"),
        ("Pertes max avant drawdown de 50 %", f"{(0.5 * a.capital) / full_loss:.1f}"),
        ("Taille max permise par la marge", f"{a.capital / (a.price / a.eurusd * a.margin):.2f} lot"),
    ]
    for k, v in rows:
        print(f"{k:<38} {v}")
    print()
    print("Risque par trade selon la distance du stop (taille 0,01) :")
    for pts in (25, 50, 75, 100, 150, dist_50):
        loss = pts * eur_per_pt + gsl_fee + spread_cost
        print(f"  {pts:>5.0f} pts ({100 * pts / a.price:.2f} %) -> {loss:.2f} EUR, {100 * loss / a.capital:.1f} % du capital")


if __name__ == "__main__":
    main()
