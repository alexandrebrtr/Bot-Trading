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
      strategy/fundamental.py (FundamentalView)   strategy/technical.py (top-down D1→M5)
                                     │                                   │  + obstacles.py
                                     ▼                                   ▼
                     strategy/decision_engine.py : LONG / HOLD / CLOSE / NO_TRADE
                     (fondamental dominant, sizing.py, pré-contrôle RiskEngine, positions.py)
                                     │  Decision → OrderRequest (backtester / paper)
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
| `marketdata/source.py` | Interfaces `PriceSource` (bougies bid + spread) et `PointInTimeSource` (séries datées par leur disponibilité), implémentations mémoire et CSV. | core |
| `marketdata/synthetic.py` | Bougies synthétiques déterministes (régimes, séances, gaps) pour tests et démo. | core |
| `core/decision.py` | `Decision` : LONG / HOLD / CLOSE / NO_TRADE, invalidation, risque, setup, biais. | aucune |
| `strategy/fundamental.py` | `FundamentalView` : régime, biais, autorisation d'entrée, multiplicateur ≤ 1, depuis une `RegimeAssessment` ou les séries point-in-time. | marketdata.source |
| `strategy/technical.py` | Technical Engine top-down : EMA 20/50/100/200, ATR, structure, FVG et OB par timeframe ; setups H1/H4 notés ∈ [0 ; 1] et confirmés en M15 (ou base). | analysis |
| `strategy/obstacles.py` | Premier obstacle au-dessus de l'entrée (FVG baissier, OB vendeur, swing haut non cassé), distance en R, ATR et spreads. | technical |
| `strategy/sizing.py` | Taille = risque / (distance + prime) × valeur du point, plafonds R5, R8, R19 ; paliers de drawdown R17. Partagé avec le backtester. | config, core |
| `strategy/positions.py` | Sorties swing (fondamental, régime, structure, temps, financement, risque) et remontée du stop garanti, jamais vers le bas. | technical, fundamental |
| `strategy/decision_engine.py` | `DecisionEngine` (pipeline à 8 étages, champs de la mission) et `DecisionStrategy` (branchement backtester). | risk, strategy |
| `backtest/engine.py` | Backtester événementiel chronologique. Réutilise `SimulatedBroker` et `RiskEngine`. | broker, risk, marketdata |
| `backtest/strategies.py` | Benchmarks (buy & hold, trend following, technique FVG, fondamentale seule, complète, aléatoire). | analysis, backtest |
| `backtest/metrics.py` | Métriques de la mission, Sharpe probabiliste et déflaté, risque de ruine. | backtest |
| `backtest/validation.py` | Walk-forward, réserve finale à ouverture unique, stress tests, tableau comparatif. | backtest |
| `backtest/lookahead.py` | Tests de préfixe et de futur alternatif applicables à toute stratégie. | backtest |

| `intelligence/data.py` | Interface `PointInTimeData` (séries par vintage, Fed, calendrier, EDGAR, pondérations) et store en mémoire. | aucune |
| `intelligence/macro.py` | Macro Intelligence Agent (F1 à F5) : biais, incertitude, scénarios, invalidations, sources. | data, fed, llm |
| `intelligence/equity.py` | Equity Intelligence Agent (F7) : résultats des grosses pondérations, breadth, secteurs. | data |
| `intelligence/regime.py` | Market Regime Engine : `RISK_ON` / `NEUTRE` / `RISK_OFF`, autorisation d'entrée, multiplicateur de taille ≤ 1, fenêtres d'annonce. | macro, equity |
| `intelligence/llm.py`, `guard.py` | LLM optionnel (OpenAI Responses), désactivé par défaut et en backtest ; protection contre l'injection et les fuites de secrets. | pydantic, requests |

| `paper/trader.py` | `PaperTrader` : boucle de paper trading sur `EventLoop` (même code que le backtest), état persistant (`paper_state.json`), reprise et rattrapage, publication pour le dashboard, kill switch rechargé avant chaque bougie. | backtest, broker, risk, strategy, dashboard.runtime |
| `paper/feeds.py` | `StoreBarFeed` (bougies closes du store, spread à la clôture), `CapitalRefresher` (collecte Capital.com en lecture seule), `InMemoryBarFeed` (tests). | marketdata |
| `paper/regime.py` | `RuntimeRegimeProvider` : `FundamentalView` depuis `regime.json`, refusée si absente, périmée, de démo ou publiée après la décision. | strategy.fundamental, intelligence.schemas |
| `risk/kill_switch.py` | Kill switch persistant (`data/runtime/kill_switch.json`) : engagé via `RiskEngine.engage_kill_switch`, rechargé par `apply(engine)`, reprise sur confirmation humaine ; fichier illisible = engagé. | risk |
| `dashboard/runtime.py` | Lecture tolérante de l'état d'exécution (`data/runtime/*.json[l]`) et helpers d'écriture atomique pour les producteurs. | aucune |
| `dashboard/decision_feed.py` | Interface `DecisionFeed` consommée par le dashboard ; `publish_decision` pour le moteur de décision. | core |
| `dashboard/views.py`, `charts.py`, `market.py` | Calculs d'affichage, zones FVG/OB/BOS pour les graphiques, lecture du store Parquet `as_of`. | analysis, marketdata, backtest.metrics |
| `dashboard/app.py` | Application Streamlit en lecture seule (6 onglets + kill switch). | streamlit, plotly (extra `dashboard`) |
| `dashboard/demo.py` | Jeu de démonstration synthétique (`--demo`). | backtest, marketdata |

Détail : `docs/INTELLIGENCE.md`. Le paquet `intelligence/` n'importe ni `risk`, ni `broker`, ni `core`.

## Dashboard (`bot_trading.dashboard`)

```
boucle paper / moteur de décision / régime / amélioration
        │  write_json, append_jsonl, publish_decision
        ▼
data/runtime/*.json[l] ──► RuntimeReader ──┐
data/store/*.parquet   ──► market (as_of) ─┼──► app.py (Streamlit, 127.0.0.1)
                                           │        │ bouton kill switch
                                           │        ▼
                                           └── KillSwitchStore.engage(RiskEngine)
```

- **Lecture seule.** Le paquet n'importe ni le broker simulé, ni le client
  Capital.com, ni aucune bibliothèque réseau (test AST `test_dashboard_cannot_send_orders_or_use_network`).
- **Kill switch.** Le Risk Engine reste sans I/O ; `risk/kill_switch.py`
  persiste son état. Toute boucle qui appelle `check_order` doit d'abord
  appeler `KillSwitchStore.apply(engine)` (la boucle de paper trading le fait
  avant chaque bougie).
- **Graphiques causaux.** Les zones viennent de `FVGEngine` / `OrderBlockEngine`
  rejoués bougie par bougie ; une zone est datée de sa confirmation ; les
  timeframes agrégés n'émettent que des bougies closes à l'instant d'affichage.
- **États vides.** Fichier absent = message explicatif ; fichier ou ligne
  corrompu = ignoré et signalé dans System Health, jamais d'exception.

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

## Backtester (`bot_trading.backtest`)

### Boucle d'événements

Pour chaque bougie bid `i` du timeframe de base :

1. **Ouverture** : cotation `open / open + spread` envoyée au broker simulé ; un
   gap sous un stop garanti sort **au niveau du stop** (prime facturée).
2. **Ordres en attente** (décidés à la clôture de `i-1`) exécutés au prix
   d'ouverture de `i`, jamais au prix de clôture qui a servi à décider.
   Taille = plancher(risque autorisé / (distance + prime) × valeur du point) au pas
   de 0,01 (R15) ; stop trop proche → distance minimale (R10) ; taille < 0,01 →
   `SIZE_BELOW_MIN`, jamais de stop rapproché (R11). Puis Risk Engine (veto).
3. **Vie de la bougie** : `SimulatedBroker.on_bar` (stop/TP intra-bougie, stop
   d'abord si les deux sont touchés, financement à 22:00 UTC, triple le vendredi,
   stop-out de marge, verrou de drawdown).
4. **Clôture** : equity marquée ; la stratégie reçoit un `BarContext` (bougies
   `0..i`, timeframes supérieurs **clos**, séries point-in-time à l'heure de
   clôture, positions, equity). Une remontée de stop (`HOLD` + `stop_level`) est
   posée immédiatement ; un stop ne peut jamais être éloigné.

Bougie `tradable=False`, `market_open` ou `event_blackout` (fonctions de la
configuration) → l'ordre est refusé par le Risk Engine (`MARKET_CLOSED`,
`EVENT_BLACKOUT`). Les trous dans la série sont listés comme interruptions.

### Interface pour les connecteurs

Un connecteur de prix implémente `PriceSource.get_bars(epic, timeframe, start, end) -> list[Bar]`
(bougies **bid** + spread de la bougie). Un connecteur macro ou un agent publie
des `Observation(series, period, value, available_at, source)` dans une
`PointInTimeSource` ; le backtester n'expose que `as_of(series, heure_de_décision)`.
Les stratégies fondamentales lisent par exemple `bias/swing` (`bullish`,
`neutral`, `bearish`) et `regime`.

### Garde-fous look-ahead du backtest

- `CausalHistory` lève `LookaheadError` sur tout accès à une bougie future.
- `PointInTimeView` lève `LookaheadError` si une source renvoie une valeur
  publiée après l'instant de décision ; `InMemoryPointInTimeSource` refuse une
  observation sans `available_at` (UTC) ou publiée avant sa période.
- Exécution à l'ouverture suivante ; préchauffage (`trade_start`) sans trading.
- `check_prefix_invariance` et `check_future_independence` : à exécuter sur
  toute nouvelle stratégie (les benchmarks y passent dans les tests ; une
  stratégie qui précalcule la clôture suivante est détectée).

### Validation

- `walk_forward_folds` : 24 mois d'optimisation, 6 mois de test, pas de 6 mois,
  12 derniers mois en réserve ; chaque fenêtre de test repart de 20 € et se
  préchauffe sur des bougies antérieures.
- `walk_forward` : grille de paramètres, objectif Calmar avec un minimum de
  trades (jamais le rendement seul) ; `n_trials` à reporter dans EXPERIMENTS.md
  pour le Sharpe déflaté.
- `HoldoutGuard` : la réserve finale ne s'ouvre qu'une fois (verrou sur fichier).
- `run_stress` : spread ×2 et ×3, financement ×2, prime GSL +1 point, EUR/USD
  1,00 et 1,30, cumul défavorable.

### Benchmarks

Tous passent par les mêmes coûts, la même taille minimale et le même Risk Engine :
buy & hold (stop garanti à 95 % de la distance maximale, réentrée après stop),
trend following SMA, technique FVG seule, fondamentale seule, complète
(`FundamentalFilter`), entrées aléatoires avec les mêmes stops.

### Limites connues

- Données réelles non encore branchées : les résultats actuels viennent de
  données **synthétiques** et ne disent rien de la stratégie.
- Ajustements de dividendes non simulés (pas encore dans le broker simulé).
- Take profit sur gap favorable à l'ouverture : exécuté au niveau du TP (prudent).
- Bougie D1 = jour calendaire UTC (convention de `resample`), à recaler sur la
  séance Capital.com.
- Coûts (spread par défaut, financement, prime GSL) : hypothèses de `BrokerCosts`
  à calibrer sur `GET /api/v1/markets/US500`.
- Consensus macro historique indisponible gratuitement (F12) : aucune surprise
  vs consensus ne doit être utilisée en backtest.

## Decision Engine (`bot_trading.strategy`)

Pipeline, chaque étage pouvant répondre `NO_TRADE` (champ `meta["stage"]` du journal) :

| # | Étage | Règles | Refus typique |
|---|---|---|---|
| 1 | fondamental | S2, F10, F11 : fondamental disponible, pas `RISK_OFF`, biais non baissier, hors fenêtre d'annonce, multiplicateur > 0 | « Fondamental indisponible : la technique seule ne peut pas ouvrir de position » |
| 2 | D1 | T1 (close > EMA, pente +, structure non baissière), T3 (extension ≤ k ATR) | « Tendance D1 neutral » |
| 3 | H4 | T2 : dernier BOS haussier | « Structure H4 bearish » |
| 4 | setup / confirmation | Zone FVG ou OB haussière H1/H4 active, âge ≤ max, qualité ≥ seuil du régime (0,5 en `RISK_ON`, 0,8 en `NEUTRE`, 0,9 si DD ≥ 35 %), réaction mesurable sur le TF de confirmation selon la variante (`touch`, `half`, `full_reject`, `bos`) | « Zone FVG_H1 en jeu, pas de confirmation » |
| 5 | exécution | E4/R8/R11 : stop sous la zone − buffer ATR, ≤ 2,5 % du prix ; R10 : distance minimale ; E5 : spread ≤ s × médiane | « Invalidation trop lointaine » |
| 6 | obstacles | T4/E6 : place jusqu'au premier obstacle ≥ min R | « Potentiel insuffisant : … = 0,8 R » |
| 7 | sizing | R9 : base × multiplicateur du régime × palier R17 ; R15, R5, R19 | `SIZE_BELOW_MIN` |
| 8 | risk_veto | `RiskEngine.check_order` sur l'ordre estimé | « Veto du Risk Engine : KILL_SWITCH » |

Le fondamental est **dominant** : les étages 2 à 8 ne peuvent que refuser. Une
décision `LONG` porte la justification, le biais, la configuration, la
confiance (0,6 × fondamental + 0,4 × qualité du setup), les conditions d'entrée
et d'invalidation, le risque estimé en EUR, la durée envisagée et les sources
(`Decision.missing_fields()` vide ; vérifié par les tests sur toutes les
décisions d'un backtest).

Le pré-contrôle n'est pas le veto définitif : à l'exécution (ouverture de la
bougie suivante), le backtester redimensionne au prix réel avec `size_for_risk`
et l'ordre passe par le Risk Engine du broker simulé.

Timeframes : la base du backtest fixe le plus petit timeframe disponible. En
base H1, la confirmation M15 et l'exécution M5 n'existent pas : la confirmation
se fait en H1 (`TechnicalSnapshot.confirmation_tf`). Les tests utilisent H1 et M15.

Positions ouvertes (`PositionManager`) : invalidation fondamentale (CLOSE),
`RISK_OFF` (stop au break-even ou sous le dernier plus bas H1 par défaut, Q2),
BOS baissier H4 après l'entrée (CLOSE), sortie temporelle X3, financement R22,
puis break-even après +a R et stop suiveur sous les swing lows H1. Un nouveau
stop est toujours strictement plus haut que l'actuel et sous le bid moins la
distance minimale du stop garanti ; il n'existe aucune opération de retrait du
stop, et une analyse indisponible laisse le stop garanti en place.

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

## Paper trading (`bot_trading.paper`)

```
Capital.com (GET /prices, lecture seule) ──► store Parquet (available_at)
                                                │ StoreBarFeed : bougies closes à « maintenant »
                                                ▼
regime.json ──► RuntimeRegimeProvider ──► DecisionStrategy ──► EventLoop.step (backtest/engine.py)
                                                                  │ SimulatedBroker + RiskEngine
                                                                  ▼            ▲ KillSwitchStore.apply
                         paper_state.json + account/positions/trades/decisions/equity/risk_events
                                                                  │
                                                                  ▼
                                                     dashboard (lecture seule)
```

- `EventLoop` est le corps de la boucle du backtester, extrait pour être
  appelé une bougie à la fois ; `Backtester.run` l'utilise aussi. Le paper
  trading n'a donc aucune logique d'exécution propre.
- Une bougie n'est traitée qu'une fois close (`available_at <= now`) ; la
  décision prise à sa clôture est exécutée à l'ouverture de la suivante.
- Au démarrage, la stratégie est préchauffée sur un broker jetable ; le compte
  rechargé (solde, plus haut, positions, ordre en attente, stop d'ouverture)
  n'est jamais modifié par le préchauffage.
- Un état illisible ou d'un autre timeframe bloque le démarrage : `paper reset`
  archive le compte (rien n'est supprimé).
- Limite : sans `regime.json` publié par le Market Regime Engine, toutes les
  décisions sont `NO_TRADE` (S2). La production automatique de `regime.json`
  depuis le store n'est pas encore branchée.
