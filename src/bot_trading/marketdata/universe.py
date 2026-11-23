"""Univers des grosses pondérations suivies par la composante « entreprises ».

Identifiants SEC (CIK) stables et publics ; la liste ``company_tickers.json``
de la SEC sert à les vérifier lors de ``refresh edgar``.

⚠️ Biais du survivant : c'est le top 10 **actuel** du S&P 500. Appliqué à
l'historique, il favorise les sociétés qui ont réussi (Nvidia n'était pas dans
le top 10 en 2018). Les pondérations, elles, ne sont jamais projetées dans le
passé (poids égaux). À garder en tête en lisant un backtest.
"""

from __future__ import annotations

TOP_TICKERS: tuple[str, ...] = ("NVDA", "MSFT", "AAPL", "AMZN", "GOOGL", "META", "AVGO", "BRK-B", "TSLA", "JPM")

TOP_CIKS: dict[str, str] = {
    "NVDA": "1045810",
    "MSFT": "789019",
    "AAPL": "320193",
    "AMZN": "1018724",
    "GOOGL": "1652044",
    "META": "1326801",
    "AVGO": "1730168",
    "BRK-B": "1067983",
    "TSLA": "1318605",
    "JPM": "19617",
}
