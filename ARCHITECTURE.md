# Architecture

## Vue d'ensemble

```
   données gratuites (FRED/ALFRED, Fed RSS, Cboe, Trésor, EDGAR, Dukascopy)
                                     │  marketdata/sources/ (GET seul, hôtes en liste blanche)
                                     ▼
Capital.com (lecture seule, REST + WebSocket) ──► marketdata/ ──► data/store/*.parquet (available_at, lineage)
                                     │  lectures as_of (anti look-ahead)
                                     ▼
   intelligence/ (macro, entreprises, régime)        analysis/ (structure, FVG, OB)
                                     │ RegimeAssessment                  │
                                     ▼                                   ▼
                            moteur de décision (fondamental dominant, technique secondaire)   [à venir]
                                     │  OrderRequest
                                     ▼
                              risk/engine.py  ── VETO ──► rejet journalisé
                                     │ approuvé
                                     ▼
                           broker/simulated.py (paper trading local)
```

Il n'existe **aucune flèche** vers un broker réel.

## Modules

| Module | Rôle | Dépendances |
|---|---|---|
| `config.py` | Paramètres de compte, d'instrument, de coûts et de risque. Modes `backtest` / `paper` uniquement. | aucune |
| `core/models.py` | `Candle`, `Quote`, `OrderRequest`, `Position`, `ClosedTrade`, `Timeframe`. | aucune |
| `core/contract.py` | Notionnel, marge, valeur du point, P&L, validation des tailles (0,01 lot). | config |
| `risk/engine.py` | Veto déterministe. Ne connaît ni la stratégie ni les agents. | config, core |
| `broker/simulated.py` | Exécution simulée : ask/bid, spread, stop garanti, prime, financement, stop-out. Passe toujours par le Risk Engine. | config, core, risk |
| `broker/capital_readonly.py` | Client HTTP Capital.com limité à une liste blanche de lecture, vérifiée dans l'adaptateur de transport. | requests, core |
| `analysis/indicators.py` | ATR (Wilder), SMA : causaux, version batch et incrémentale. | core |
| `analysis/structure.py` | Swings confirmés et BOS (sur clôture). | core |
| `analysis/zones.py` | Suivi de mitigation, retests et invalidation d'une zone. | core |
| `analysis/fvg.py` | Détection et qualification des FVG. | indicators, structure, zones |
| `analysis/order_blocks.py` | Détection et qualification des order blocks. | indicators, structure, zones |
| `marketdata/candles.py` | CSV, agrégation M5 → M15/H1/H4/D1 sans bougie incomplète. | core |
| `marketdata/common.py` | UTC / New York, `Provenance` (traçabilité, URL sans clé), nettoyage des textes externes, `.env`. | aucune |
| `marketdata/storage.py` | `ParquetStore` : partition par année, écriture atomique, dédoublonnage, lecture `as_of`, `_lineage.jsonl`. | pyarrow |
| `marketdata/quality.py` | Fraîcheur (FRESH/STALE/MISSING), trous en séance vs fermeture, doublons, OHLC incohérents. | core |
| `marketdata/http.py` | Client GET-only des sources publiques : hôtes en liste blanche, limiteur, reprises 429/5xx, erreurs sans secret. | requests |
| `marketdata/sources/capital.py` | Historique Capital.com paginé (via le client lecture seule), bougies closes uniquement. | broker/capital_readonly |
| `marketdata/sources/capital_stream.py` | WebSocket Capital.com : bougies et cotations, destinations en liste blanche, émission à la clôture. | websocket-client |
| `marketdata/sources/dukascopy.py` | Minutes bid/ask (bi5) agrégées en M5→D1 pour le backtest long. | candles |
| `marketdata/sources/fred.py` | FRED/ALFRED : toutes les vintages, heure de publication, `PointInTimeSeries`, calendrier des publications. | http |
| `marketdata/sources/fed_rss.py` | RSS Fed (FOMC, discours, témoignages), archivage, textes marqués non fiables. | http |
| `marketdata/sources/edgar.py` | SEC EDGAR : dépôts (10-K/10-Q/8-K) et faits XBRL datés à l'acceptation. | http |
| `marketdata/sources/cboe.py`, `treasury.py` | VIX quotidien, courbe des taux du Trésor (2 ans, 10 ans, pente). | http |
| `marketdata/engine.py` | `DataEngine` : ingestion, lectures `as_of`, rapport de santé. | tout marketdata |

| `intelligence/data.py` | Interface `PointInTimeData` (séries par vintage, Fed, calendrier, EDGAR, pondérations) et store en mémoire. | aucune |
| `intelligence/macro.py` | Macro Intelligence Agent (F1 à F5) : biais, incertitude, scénarios, invalidations, sources. | data, fed, llm |
| `intelligence/equity.py` | Equity Intelligence Agent (F7) : résultats des grosses pondérations, breadth, secteurs. | data |
| `intelligence/regime.py` | Market Regime Engine : `RISK_ON` / `NEUTRE` / `RISK_OFF`, autorisation d'entrée, multiplicateur de taille ≤ 1, fenêtres d'annonce. | macro, equity |
| `intelligence/llm.py`, `guard.py` | LLM optionnel (OpenAI Responses), désactivé par défaut et en backtest ; protection contre l'injection et les fuites de secrets. | pydantic, requests |

Détail : `docs/INTELLIGENCE.md`. Le paquet `intelligence/` n'importe ni `risk`, ni `broker`, ni `core`.

## Garanties anti look-ahead

1. **Moteurs incrémentaux.** FVG et OB sont calculés par `update(candle)` à la
   clôture de chaque bougie ; les fonctions batch rejouent simplement les bougies une à une.
2. **Confirmation explicite.** Chaque détection porte un `confirmed_index` :
   - FVG : indice de la 3ᵉ bougie ;
   - swing : indice du pivot + `right` ;
   - BOS : bougie dont la **clôture** casse le dernier swing déjà confirmé ;
   - order block : bougie du BOS.
3. **Mises à jour postérieures seulement.** `ZoneState.update` ignore toute bougie
   d'indice ≤ `confirmed_index`.
4. **ATR au moment de la confirmation.** Les ratios (taille/ATR, déplacement/ATR)
   utilisent l'ATR connu à `confirmed_index`.
5. **Multi-timeframe.** `resample` n'émet pas la bougie en cours tant qu'elle n'est
   pas close.
6. **Données externes datées.** Chaque ligne stockée porte `available_at`
   (premier instant où elle était connaissable) ; `ParquetStore.read(as_of=t)`
   ne renvoie que `available_at <= t`. Heures inconnues = choix prudent
   (fin de journée à New York). Les vintages ALFRED donnent la valeur telle
   que publiée puis révisée (`PointInTimeSeries.as_of`).
7. **Tests de préfixe.** `tests/test_fvg.py` et `tests/test_structure_and_ob.py`
   vérifient que les détections confirmées à l'indice *i* sont identiques quand la
   série s'arrête à *i* et quand elle continue, et qu'un futur différent ne change
   pas le passé.

## Risk Engine

Règles (toutes évaluées, toutes listées en cas de veto) :

| Règle | Violation |
|---|---|
| Kill switch manuel | `KILL_SWITCH` |
| Drawdown ≥ 50 % depuis le plus haut (verrou, reprise sur confirmation humaine) | `DRAWDOWN_HALT` |
| Perte au stop des positions ouvertes + nouvelle position ferait passer sous le plancher | `DRAWDOWN_WOULD_BREACH` |
| Short demandé alors que désactivé | `SHORT_DISABLED` |
| Mauvais epic | `WRONG_EPIC` |
| Stop absent / non garanti / du mauvais côté / trop proche | `NO_STOP`, `STOP_NOT_GUARANTEED`, `STOP_WRONG_SIDE`, `STOP_TOO_CLOSE` |
| Perte au stop (+ prime) > 50 % de la marge engagée (plafond absolu) | `MAX_LOSS_EXCEEDED` |
| Perte au stop (+ prime) > 20 % de l'equity (risque max par trade) | `MAX_RISK_PER_TRADE_EXCEEDED` |
| Take profit du mauvais côté | `TAKE_PROFIT_WRONG_SIDE` |
| Taille < 0,01 ou hors pas | `INVALID_SIZE` |
| Exposition cumulée > 1 lot | `EXPOSURE_EXCEEDED` |
| Marge requise (levier 20) > marge disponible | `INSUFFICIENT_MARGIN` |
| Spread trop large, données périmées, marché fermé, fenêtre d'annonce macro | `SPREAD_TOO_WIDE`, `STALE_DATA`, `MARKET_CLOSED`, `EVENT_BLACKOUT` |

## Broker simulé : hypothèses à calibrer

Les valeurs suivantes sont dans `BrokerCosts` et doivent être recalées sur
`GET /api/v1/markets/US500` (champs `instrument.overnightFee`, `dealingRules`,
`snapshot`) et sur l'historique des transactions `SWAP` du compte :

- spread par défaut (0,6 point) quand la donnée ne fournit pas bid/ask ;
- prime du stop garanti (0 par défaut, facturée seulement au déclenchement) ;
- taux de financement long journalier, heure de prélèvement, multiplicateur du vendredi ;
- seuil de stop-out (50 % de la marge utilisée) ;
- EUR/USD par défaut (1,16).

## API Capital.com (doc officielle lue le 20/11/2026)

- REST live : `https://api-capital.backend-capital.com`, démo :
  `https://demo-api-capital.backend-capital.com` (inutilisable, pas de compte démo).
- Authentification : `POST /api/v1/session` avec `X-CAP-API-KEY` et le corps
  `identifier` / `password` ; jetons `CST` et `X-SECURITY-TOKEN` en headers ;
  session de 10 minutes, `GET /api/v1/ping` pour la garder.
- Prix : `GET /api/v1/prices/{epic}` avec `resolution` (`MINUTE_5`, `MINUTE_15`,
  `HOUR`, `HOUR_4`, `DAY`…), `max` (1000 max), `from`, `to`.
- Limite : 10 requêtes par seconde, `POST /session` 1 par seconde.

Les noms de champs de la réponse des prix (`snapshotTimeUTC`, `openPrice.bid`…)
n'ont été lus que dans un résumé de la page : `parse_prices` devra être revalidé
sur une réponse réelle.
