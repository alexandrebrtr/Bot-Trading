"""Commande du Data Engine : ``python -m bot_trading.marketdata <commande>``.

- ``health`` : fraîcheur, trous et doublons de chaque dataset stocké ;
- ``refresh <source> [...]`` : collecte réelle (réseau) puis stockage Parquet.
  Sources : ``capital``, ``dukascopy``, ``fred``, ``fed-rss``, ``vix``,
  ``treasury``, ``edgar``. Les identifiants sont lus dans ``.env``.

Aucune commande n'envoie d'ordre : Capital.com reste en lecture seule.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta

from bot_trading.core.models import Timeframe
from bot_trading.marketdata.common import load_dotenv, utc_now
from bot_trading.marketdata.engine import DataEngine
from bot_trading.marketdata.storage import ParquetStore

# Grosses pondérations suivies par défaut (tickers ; CIK résolus via company_tickers.json).
TOP_TICKERS = ("NVDA", "MSFT", "AAPL", "AMZN", "GOOGL", "META", "AVGO", "BRK-B", "TSLA", "JPM")


def _report(label: str, res) -> None:
    print(f"{label:40} {res.rows_in:>7} lignes reçues, {res.rows_new:>7} nouvelles, {res.rows_total:>8} au total")


def refresh(engine: DataEngine, source: str, args: argparse.Namespace) -> None:
    now = utc_now()
    if source == "capital":
        from bot_trading.broker.capital_readonly import CapitalCredentials, CapitalReadOnlyClient
        from bot_trading.marketdata.sources.capital import CapitalCandleSource

        client = CapitalReadOnlyClient(CapitalCredentials.from_env())
        src = CapitalCandleSource(client, args.epic)
        for tf in [Timeframe(t) for t in args.timeframes]:
            rows, prov = src.fetch(tf, now - timedelta(days=args.days))
            _report(prov.dataset, engine.ingest_candles(rows, prov, tf))
        client.logout()
    elif source == "dukascopy":
        from bot_trading.marketdata.sources.dukascopy import DukascopySource

        src = DukascopySource(epic=args.epic)
        for tf in [Timeframe(t) for t in args.timeframes]:
            rows, prov = src.fetch(now.date() - timedelta(days=args.days), now.date(), tf)
            _report(prov.dataset, engine.ingest_candles(rows, prov, tf))
    elif source == "fred":
        from bot_trading.marketdata.sources import fred

        src = fred.FredSource()
        for sid in args.series or list(fred.SERIES):
            rows, prov = src.observations(sid)
            _report(prov.dataset, engine.ingest_fred(rows, prov, sid))
        rows, prov = src.release_calendar(now.date() - timedelta(days=30), now.date() + timedelta(days=90))
        _report(prov.dataset, engine.ingest(rows, prov, fred.CALENDAR_KEYS))
    elif source == "fed-rss":
        from bot_trading.marketdata.sources.fed_rss import FEEDS, FedRssSource

        src = FedRssSource()
        for feed in FEEDS:
            rows, prov = src.fetch(feed)
            _report(f"{prov.dataset} ({feed})", engine.ingest_fed_rss(rows, prov))
    elif source == "vix":
        from bot_trading.marketdata.sources.cboe import CboeSource

        rows, prov = CboeSource().fetch("VIX")
        _report(prov.dataset, engine.ingest_vix(rows, prov))
    elif source == "treasury":
        from bot_trading.marketdata.sources.treasury import TreasurySource

        src = TreasurySource()
        for year in range(now.year - args.years + 1, now.year + 1):
            rows, prov = src.fetch_year(year)
            _report(f"{prov.dataset} {year}", engine.ingest_treasury(rows, prov))
    elif source == "edgar":
        from bot_trading.marketdata.sources.edgar import EdgarSource

        src = EdgarSource()
        ciks = src.tickers()
        for ticker in args.tickers or TOP_TICKERS:
            cik = ciks.get(ticker.upper())
            if cik is None:
                print(f"{ticker}: CIK introuvable")
                continue
            filings, prov = src.filings(cik)
            _report(f"{prov.dataset} {ticker}", engine.ingest_edgar_filings(filings, prov))
            acc = {f["accession_number"]: f["available_at"] for f in filings}
            facts, prov = src.facts(cik, acc)
            _report(f"{prov.dataset} {ticker}", engine.ingest_edgar_facts(facts, prov))
    else:
        raise SystemExit(f"Source inconnue : {source}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bot_trading.marketdata")
    parser.add_argument("--store", default="data/store")
    parser.add_argument("--env", default=".env")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("health")
    rf = sub.add_parser("refresh")
    rf.add_argument("sources", nargs="+")
    rf.add_argument("--epic", default="US500")
    rf.add_argument("--timeframes", nargs="+", default=["M5", "M15", "H1", "H4", "D1"])
    rf.add_argument("--days", type=int, default=30)
    rf.add_argument("--years", type=int, default=2)
    rf.add_argument("--series", nargs="*")
    rf.add_argument("--tickers", nargs="*")
    args = parser.parse_args(argv)
    load_dotenv(args.env)
    engine = DataEngine(ParquetStore(args.store))
    if args.cmd == "health":
        reports = engine.health()
        if not reports:
            print("Aucun dataset stocké.")
        for r in reports:
            print(("OK   " if r.ok else "ALERTE ") + r.summary())
        return 0 if all(r.ok for r in reports) else 1
    for s in args.sources:
        refresh(engine, s, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
