# Bot-Trading

Système de trading swing **Long Only** et **trend following** sur le CFD **US500**
(S&P 500) de Capital.com. L'analyse fondamentale décide de la direction,
l'analyse technique (FVG, order blocks, structure multi-timeframe) optimise
l'entrée. L'absence d'opportunité conduit à `NO_TRADE`.

> **Aucun ordre réel ne peut partir de ce dépôt.** Le seul compte API disponible
> est un compte réel (pas de démo). L'exécution passe exclusivement par un broker
> simulé local, et l'accès à Capital.com est bloqué en lecture seule au niveau du
> transport HTTP. Voir [SECURITY.md](SECURITY.md).

## État actuel (socle)

| Composant | Module | État |
|---|---|---|
| Configuration (20 €, levier 20:1, 0,01 lot, 1 lot max) | `bot_trading.config` | ✅ |
| Calculs de contrat (marge, notionnel, P&L EUR/USD) | `bot_trading.core.contract` | ✅ |
| Risk Engine déterministe avec veto | `bot_trading.risk.engine` | ✅ |
| Broker simulé type Capital.com | `bot_trading.broker.simulated` | ✅ |
| Adaptateur Capital.com en lecture seule | `bot_trading.broker.capital_readonly` | ✅ (mocks, jamais testé sur l'API réelle) |
| Structure de marché (swings, BOS) | `bot_trading.analysis.structure` | ✅ |
| Fair Value Gap Engine | `bot_trading.analysis.fvg` | ✅ |
| Order Block Engine | `bot_trading.analysis.order_blocks` | ✅ |
| Agrégation multi-timeframe sans look-ahead | `bot_trading.marketdata.candles` | ✅ |
| Data Engine : stockage Parquet point-in-time, traçabilité, qualité | `bot_trading.marketdata.storage`, `quality`, `engine` | ✅ |
| Connecteurs : Capital.com (REST + WebSocket), Dukascopy, FRED/ALFRED, RSS Fed, SEC EDGAR, Cboe VIX, Trésor US | `bot_trading.marketdata.sources` | ✅ (mocks, jamais testés sur les vraies API) |
| Interfaces de sources de données (prix, séries point-in-time) | `bot_trading.marketdata.source` | ✅ |
| Backtester événementiel, benchmarks, métriques, walk-forward, stress tests | `bot_trading.backtest` | ✅ (validé sur données synthétiques, pas encore sur données réelles) |
| Moteur de décision, boucles d'amélioration | — | à venir |

## Démarrage rapide

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest            # aucun réseau, aucun identifiant
python -m bot_trading status
python -m bot_trading replay chemin/vers/bougies_m5.csv
python -m bot_trading backtest --years 4 --walk-forward   # données SYNTHÉTIQUES
python -m bot_trading backtest --csv bougies_h1_bid.csv --timeframe H1

# Data Engine (réseau + .env) : collecte puis contrôle de fraîcheur
python -m bot_trading.marketdata refresh fred fed-rss vix treasury
python -m bot_trading.marketdata refresh capital --timeframes M5 H1 D1 --days 30
python -m bot_trading.marketdata refresh dukascopy --timeframes M5 --days 365
python -m bot_trading.marketdata refresh edgar
python -m bot_trading.marketdata health
```

Les données sont écrites dans `data/store/` (ignoré par git), une table Parquet
par jeu de données et par année, plus `data/store/_lineage.jsonl` qui trace
chaque collecte (source, URL sans clé, heure, empreinte SHA-256, licence).

Avec Docker :

```bash
docker compose run --rm tests     # tests, réseau coupé
docker compose run --rm bot status
```

Configuration : copier `.env.example` en `.env` (ignoré par git). Les identifiants
Capital.com ne servent qu'à lire les prix et les caractéristiques du marché.

## Ordres de grandeur

Avec l'US500 vers 6 800 points et un levier de 20 : 1 lot ≈ 340 € de marge,
0,01 lot ≈ 3,4 €, 1 point = 0,01 $ en 0,01 lot. La règle « perte max 50 % de la
marge engagée » limite la distance du stop garanti à environ 2,5 % du prix
(≈ 170 points), quelle que soit la taille.

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) : modules, flux de données, garanties anti look-ahead, backtester.
- [AGENTS.md](AGENTS.md) : règles pour les agents (humains ou IA) qui modifient le code.
- [SECURITY.md](SECURITY.md) : garde-fous contre les ordres réels et gestion des secrets.
- `DATA_SOURCES.md`, `STRATEGY.md`, `RISK_POLICY.md`, `ROADMAP.md`, `EXPERIMENTS.md` : rédigés dans une branche dédiée.
