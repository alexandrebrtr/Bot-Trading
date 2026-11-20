"""Flux RSS de la Réserve fédérale : décisions FOMC, discours, témoignages.

Doc : https://www.federalreserve.gov/feeds/feeds.htm. Sans clé. Les flux ne
gardent que ~15 éléments : il faut **interroger régulièrement et archiver**
(le stockage Parquet dédoublonne sur ``guid``).

``available_at`` = ``pubDate`` du flux. Les titres et résumés sont des textes
externes : nettoyés, tronqués et marqués ``trust = "untrusted_external"``.
Ce sont des données à analyser, jamais des instructions.
"""

from __future__ import annotations

from datetime import datetime
from email.utils import parsedate_to_datetime

from bot_trading.marketdata.common import UNTRUSTED, Provenance, clean_external_text, ensure_utc, sha256_bytes, utc_now
from bot_trading.marketdata.http import PublicSourceClient
from bot_trading.marketdata.sources.xmlsafe import child_text, local, parse_xml

SOURCE = "fed_rss"
HOSTS = ("www.federalreserve.gov",)
BASE = "https://www.federalreserve.gov/feeds"
FEEDS = {
    "press_monetary": f"{BASE}/press_monetary.xml",  # communiqués FOMC
    "press_all": f"{BASE}/press_all.xml",
    "speeches": f"{BASE}/speeches.xml",
    "testimony": f"{BASE}/testimony.xml",
}
DATASET = "news.fed_rss"
KEYS = ("guid",)
LICENSE = "federalreserve.gov : information publique du gouvernement US."


def parse_rss(raw: bytes, feed: str) -> list[dict]:
    root = parse_xml(raw)
    out = []
    for item in root.iter():
        if local(item.tag) != "item":
            continue
        pub = child_text(item, "pubDate")
        if not pub:
            continue
        try:
            published = ensure_utc(parsedate_to_datetime(pub.strip()))
        except (TypeError, ValueError):
            continue
        link = clean_external_text(child_text(item, "link"), 500)
        if link and not link.startswith("https://www.federalreserve.gov/"):
            link = ""  # lien inattendu : on ne le garde pas
        guid = clean_external_text(child_text(item, "guid"), 500) or link or f"{feed}:{published.isoformat()}"
        out.append(
            {
                "guid": guid,
                "feed": feed,
                "title": clean_external_text(child_text(item, "title"), 500),
                "summary": clean_external_text(child_text(item, "description"), 2000),
                "category": clean_external_text(child_text(item, "category"), 200),
                "link": link,
                "event_time": published,
                "available_at": published,
                "trust": UNTRUSTED,
            }
        )
    return out


class FedRssSource:
    def __init__(self, client: PublicSourceClient | None = None, clock=utc_now) -> None:
        self.client = client or PublicSourceClient(SOURCE, HOSTS, rate_per_second=1)
        self.clock = clock

    def fetch(self, feed: str) -> tuple[list[dict], Provenance]:
        url = FEEDS[feed]
        now: datetime = self.clock()
        raw = self.client.get(url).content
        return parse_rss(raw, feed), Provenance(SOURCE, DATASET, url, now, {"feed": feed}, sha256_bytes(raw), LICENSE)
