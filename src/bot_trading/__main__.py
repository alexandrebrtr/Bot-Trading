"""Point d'entrée : ``python -m bot_trading <commande>``.

Commandes :
- ``status`` : affiche le mode et les garde-fous actifs ;
- ``replay <fichier.csv>`` : rejoue des bougies M5 (bid) dans les moteurs FVG / OB
  et le broker simulé, sans aucun ordre (diagnostic des détections).

Il n'existe aucune commande d'exécution réelle.
"""

from __future__ import annotations

import argparse
import sys

from bot_trading.analysis.fvg import FVGEngine
from bot_trading.analysis.order_blocks import OrderBlockEngine
from bot_trading.broker.capital_readonly import ALLOWED_ENDPOINTS
from bot_trading.config import Settings
from bot_trading.marketdata.candles import load_csv


def cmd_status(settings: Settings) -> int:
    r = settings.risk
    print(f"Mode               : {settings.mode.value} (aucun mode réel n'existe)")
    print(f"Instrument         : {settings.instrument.epic}, taille min {settings.instrument.min_size} lot")
    print(f"Capital initial    : {r.initial_capital_eur} EUR, levier max {r.max_leverage:g}:1")
    print(f"Exposition max     : {r.max_total_exposure_lots} lot")
    print(f"Perte max/position : {r.max_loss_fraction_of_margin:.0%} de la marge engagée")
    print(f"Risque max/trade   : {r.max_risk_fraction_of_equity:.0%} de l'equity")
    print(f"Drawdown max       : {r.max_account_drawdown:.0%}")
    print(f"Shorts             : {'autorisés' if r.allow_shorts else 'désactivés'}")
    print(f"Stop garanti       : {'obligatoire' if r.require_guaranteed_stop else 'facultatif'}")
    print("Capital.com        : lecture seule, endpoints autorisés :")
    for m, rx in ALLOWED_ENDPOINTS:
        print(f"  {m:6} {rx.pattern}")
    return 0


def cmd_replay(path: str) -> int:
    candles = load_csv(path)
    fvg = FVGEngine()
    ob = OrderBlockEngine()
    for c in candles:
        fvg.update(c)
        ob.update(c)
    print(f"{len(candles)} bougies, {len(fvg.fvgs)} FVG, {len(ob.blocks)} order blocks")
    print(f"FVG haussiers actifs : {len(fvg.active(True))}, OB haussiers actifs : {len(ob.active(True))}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bot_trading")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    rp = sub.add_parser("replay")
    rp.add_argument("csv")
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    if args.cmd == "status":
        return cmd_status(settings)
    return cmd_replay(args.csv)


if __name__ == "__main__":
    sys.exit(main())
