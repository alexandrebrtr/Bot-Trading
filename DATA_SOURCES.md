# DATA_SOURCES.md — Sources de données gratuites

> Bot swing **Long Only** sur le CFD **US500** de Capital.com, capital 20 €, 0,01 lot.
> Contrainte : **sources gratuites uniquement** (ni Trading Economics, ni FMP).
> Pas de compte démo : le paper trading est **local**, alimenté par l'API Capital.com en lecture seule.
>
> Recherche faite le 20/11/2026. Chaque fait renvoie à la doc lue.
> Légende : ✅ vérifié dans la doc · ⚠️ à vérifier (info partielle, résumé ou doc ambiguë) · ❌ pas de source gratuite fiable.

---

## 0. Résumé : quelle source pour quel besoin

| Besoin | Source principale | Secours / complément | Statut |
|---|---|---|---|
| Bougies US500 M5 à D1, temps réel | API Capital.com (REST + WebSocket) | — | ✅ |
| Bougies US500, historique long (backtest) | Dukascopy (CFD US 500) | API Capital.com (historique limité, à mesurer) | ⚠️ |
| Calendrier macro : dates | FRED `release/dates` (dates futures incluses) | Calendriers BLS/BEA | ✅ |
| Calendrier macro : valeur publiée et précédente | ALFRED (FRED point-in-time) | BLS API, BEA API, DOL (claims) | ✅ |
| Calendrier macro : **consensus** | ❌ aucune API gratuite fiable | Proxys : Cleveland Fed nowcast (inflation), SPF (trimestriel) | ❌ |
| Consensus **historique** | ❌ aucune source gratuite | — | ❌ trou majeur |
| Macro point-in-time avec révisions | ALFRED (`realtime_start/end`, `vintagedates`) | — | ✅ |
| Discours et décisions Fed | RSS federalreserve.gov | — | ✅ |
| Anticipations de taux | Atlanta Fed Market Probability Tracker | — | ⚠️ (format de téléchargement à confirmer) |
| Fondamentaux S&P 500 (publiés) | SEC EDGAR (data.sec.gov) | — | ✅ |
| Estimations des analystes (EPS consensus) | ❌ pas de source gratuite fiable | yfinance (usage perso), Alpha Vantage `EARNINGS_CALENDAR` | ❌/⚠️ |
| Pondérations S&P 500 | Fichier holdings quotidien du SPY (SSGA) | — | ⚠️ (usage interne seulement) |
| VIX | Cboe (CSV quotidien depuis 1990) | — | ✅ |
| Dollar (DXY) | ❌ DXY ICE non gratuit → proxy FRED `DTWEXBGS` | — | ⚠️ proxy |
| Taux US 2 et 10 ans | Trésor US (XML quotidien) | FRED | ✅ |
| Spreads de crédit | FRED ICE BofA (3 ans d'historique, copyright) | — | ⚠️ |
| Secteurs | ❌ pas de source officielle gratuite intraday | ETF sectoriels via yfinance (usage perso) | ⚠️ |
| Actualités | RSS Fed + EDGAR (8-K) + GDELT DOC 2.0 | — | ⚠️ |

---

## 1. Prix US500

### 1.1 API Capital.com (source de vérité pour le live et le paper trading)

Doc : https://open-api.capital.com/

- **Clé** : oui. Header `X-CAP-API-KEY`, puis `POST /api/v1/session` (identifiant + mot de passe de la clé). Les requêtes suivantes passent les headers `CST` et `X-SECURITY-TOKEN`. ✅
- **Session** : « Session is active for 10 minutes », tokens valables 10 minutes après le dernier usage. Garder en vie avec `GET /api/v1/ping`. ✅
- **Historique REST** : `GET /api/v1/prices/{epic}` avec `resolution` (MINUTE, MINUTE_5, MINUTE_15, MINUTE_30, HOUR, HOUR_4, DAY, WEEK), `max`, `from`, `to` (format `YYYY-MM-DDTHH:MM:SS`). ✅ Le plafond de `max` = 1000 bougies par requête vient d'un résumé de la page : ⚠️ à confirmer par un test.
- **Profondeur d'historique** : non documentée. ⚠️ À mesurer pour chaque résolution (paginer avec `from`/`to` jusqu'à ce que la réponse soit vide).
- **Temps réel** : WebSocket `wss://api-streaming-capital.backend-capital.com/connect`. Abonnement cotations `marketData.subscribe` et bougies `OHLCMarketData.subscribe` (mêmes résolutions, type `classic` ou `heikin-ashi`), **40 instruments max**, ping au moins toutes les 10 minutes. ✅
- **Quotas** : « 10 per second per user » ; `POST /session` limité à 1 requête/s par clé. ✅
- **Prix bid/ask** : les bougies contiennent bid et ask (open/close/high par côté). ✅ Utile pour simuler le spread réel en paper trading.
- **Licence** : la page renvoie aux Terms and Conditions de Capital.com, sans clause spécifique sur les données. ⚠️ Usage personnel, ne pas redistribuer.
- **Fiabilité** : c'est le prix exact du CFD tradé, donc la référence pour les signaux et le paper trading.
- **Sécurité** : la session API est sur le **compte live**. Le module paper trading ne doit appeler **que** les endpoints de lecture (`/prices`, `/markets`, WebSocket). Prévoir un garde-fou dans le code qui bloque tout appel à `/positions` et `/workingorders`.
- Note : la doc indique qu'un compte démo est utilisable via `demo-api-capital.backend-capital.com`. Alexandre ne peut ouvrir aucun compte démo : paper trading local.
- Frais, stop garanti et faisabilité à 0,01 lot : voir `docs/CAPITAL_COM.md`.

### 1.2 Dukascopy (historique long pour le backtest)

Doc : https://www.dukascopy.com/swiss/en/marketwatch/historical/ · client open source : https://github.com/theorycraft-trading/dukascopy

- **Clé** : non (export gratuit). ✅
- **Timeframes** : tick, m1, m5, m15, m30, h1, h4, D, W, M (README du client). ✅
- **Instrument** : un CFD indice US 500 existe dans le catalogue, mais l'identifiant exact et la date de début ne sont pas sur les pages lues. ⚠️ Le client propose `dukascopy search <query>` pour le trouver.
- **Licence** : le client est sous Apache-2.0, ce qui ne couvre **pas** les données. Les conditions de Dukascopy pour les données n'ont pas été trouvées. ⚠️ Usage perso / recherche uniquement.
- **Fiabilité** : bonne réputation en forex. Attention, c'est le prix CFD de Dukascopy et **pas celui de Capital.com** : spread, horaires et éventuels ajustements de dividendes diffèrent. Il faut calibrer l'écart sur la période où les deux sources se recoupent.

### 1.3 À éviter pour l'US500

- **Alpha Vantage** : l'intraday est premium (« This is a premium endpoint ») et le gratuit est limité à 25 requêtes/jour. https://alphavantage.co/documentation · https://alphavantage.co/support/
- **yfinance** : « the Yahoo! finance API is intended for personal use only », non affilié à Yahoo. Utile pour un contrôle ponctuel de l'indice cash (^GSPC), pas comme source de production. https://raw.githubusercontent.com/ranaroussi/yfinance/main/README.md

---

## 2. Calendrier économique

### 2.1 Dates de publication : FRED

Doc : https://fred.stlouisfed.org/docs/api/fred/release_dates.html · https://fred.stlouisfed.org/docs/api/fred/releases_dates.html

- `GET https://api.stlouisfed.org/fred/release/dates?release_id=...` et `.../fred/releases/dates`. ✅
- `include_release_dates_with_no_data=true` renvoie les **dates futures** du calendrier (le défaut `false` les exclut). ✅
- La doc précise que ces dates viennent des agences et ne garantissent pas l'heure de disponibilité sur FRED. ⚠️ Les **heures** (8h30 ET pour CPI/NFP, 10h00 pour ISM…) sont à coder en dur et à vérifier sur les calendriers des agences.
- **Clé** : `api_key` obligatoire (gratuite). Quota : 120 requêtes/minute selon un profil tiers non vérifié (https://apis.io/rate-limits/fred/fred-rate-limits/). ⚠️

### 2.2 Valeur publiée et valeur précédente : ALFRED et agences

- **ALFRED** (voir §3) : la première vintage d'une observation = **valeur publiée** ; la vintage précédente = **valeur précédente telle que connue ce jour-là**. C'est exactement ce qu'il faut pour un backtest sans biais d'anticipation.
- **BLS** (CPI, NFP, chômage) : https://www.bls.gov/developers/api_faqs.htm. v2 avec clé gratuite : 500 requêtes/jour, 50 séries et 20 ans par requête, 50 requêtes par 10 s. Sans clé (v1) : 25 requêtes/jour. ✅ La doc signale un décalage d'un jour entre la publication et la disponibilité dans l'API. ⚠️ Pour réagir à 8h30, l'API BLS est donc trop lente : utiliser FRED/ALFRED ou le communiqué.
- **BEA** (PIB, PCE) : inscription gratuite (nom + email) sur https://apps.bea.gov/API/signup/. ⚠️ Le guide de l'API (quotas) était introuvable à l'URL testée. Doc à lire : https://apps.bea.gov/API/docs/index.htm
- **DOL** (jobless claims) : historique hebdomadaire depuis 1967, https://oui.doleta.gov/unemploy/claims_arch.asp (via https://catalog.data.gov/dataset/historical-unemployment-insurance-seasonally-adjusted-and-unadjusted-weekly-claims-data). ⚠️ Pas de licence explicite (donnée publique US).
- **ISM** : non couvert ici. Les données ISM sont propriétaires. ❌ Pas de source gratuite officielle identifiée pour l'historique ISM.
- **Retail sales, production industrielle** : disponibles sur FRED/ALFRED (Census et Fed). ✅

### 2.3 Consensus : le trou principal

**Aucune API gratuite et fiable ne fournit le consensus (prévision des économistes), ni en temps réel, ni surtout en historique.**

- Trading Economics, Benzinga, FMP : payants (exclus).
- Investing.com, ForexFactory, TradingView : consensus visible sur le site, mais pas d'API officielle. Le scraping est fragile et souvent contraire aux CGU. Un flux JSON hebdomadaire de ForexFactory circule (`nfs.faireconomy.media`), mais je n'ai trouvé **aucune doc officielle** : ⚠️ non documenté, peut disparaître, et ne couvre que la semaine en cours (pas d'historique).
- Alpha Vantage liste des fonctions macro (`CPI`, `NONFARM_PAYROLL`, `UNEMPLOYMENT`, `REAL_GDP`, `RETAIL_SALES`…), mais ce sont des **valeurs publiées**, pas des consensus. https://alphavantage.co/documentation

**Proxys gratuits possibles :**
- **Cleveland Fed Inflation Nowcasting** : nowcast quotidien (jours ouvrés, vers 10h ET) du CPI, core CPI, PCE et core PCE. https://www.clevelandfed.org/indicators-and-data/inflation-nowcasting ✅ Bon proxy de « surprise inflation » = publié − nowcast. ⚠️ Le téléchargement de l'historique des nowcasts n'est pas confirmé.
- **Survey of Professional Forecasters (Philadelphia Fed)** : prévisions de CPI, PCE, chômage, payrolls et PIB, avec un historique long. https://www.philadelphiafed.org/research-and-data/real-time-center/survey-of-professional-forecasters/data-files ⚠️ Trimestriel, donc trop grossier pour une surprise du mois.
- **Surprise statistique** : publié − prévision d'un modèle simple (AR, moyenne mobile) calculé sur ALFRED. C'est gratuit, sans biais d'anticipation et reproductible, mais ce n'est pas le consensus du marché.

**Recommandation :** en backtest, ne pas utiliser de feature « surprise vs consensus ». Utiliser plutôt (a) un **filtre de calendrier** (pas d'entrée X heures avant ou après CPI, NFP ou FOMC) et (b) la surprise vs nowcast Cleveland pour l'inflation. En live, on pourra enregistrer soi-même le consensus chaque semaine (saisie manuelle ou source à valider) pour se constituer un historique à partir de maintenant.

---

## 3. Macro point-in-time : FRED / ALFRED

Doc : https://fred.stlouisfed.org/docs/api/fred/realtime_period.html · https://fred.stlouisfed.org/docs/api/fred/series_vintagedates.html

- `realtime_start` / `realtime_end` (YYYY-MM-DD, période fermée des deux côtés) donnent la série **telle qu'elle était connue** à une date. ✅
- `GET https://api.stlouisfed.org/fred/series/vintagedates?series_id=...` liste toutes les dates de publication ou de révision d'une série (jusqu'à 10 000 par requête). ✅
- **Clé** : obligatoire, gratuite. **Quota** : 120 req/min (source tierce ⚠️).
- **Licence** : variable selon la série. Les séries d'agences US sont en général du domaine public, mais certaines sont sous copyright (ex. ICE BofA, §5.4). Vérifier les notes de chaque série. Conditions FRED : https://fred.stlouisfed.org/legal/ (non lue ⚠️).
- **Fiabilité** : très élevée (Fed de St. Louis). C'est la brique centrale pour la macro.

---

## 4. Fed : discours, décisions, anticipations

### 4.1 RSS federalreserve.gov ✅

Doc : https://www.federalreserve.gov/feeds/feeds.htm

- Politique monétaire (communiqués FOMC) : https://www.federalreserve.gov/feeds/press_monetary.xml
- Tous les communiqués : https://www.federalreserve.gov/feeds/press_all.xml
- Discours : https://www.federalreserve.gov/feeds/speeches.xml · témoignages : https://www.federalreserve.gov/feeds/testimony.xml
- Par gouverneur : ex. https://www.federalreserve.gov/feeds/s_t_powell.xml, https://www.federalreserve.gov/feeds/s_t_waller.xml
- Taux directeurs : https://www.federalreserve.gov/feeds/prates.xml · H.15 : https://www.federalreserve.gov/feeds/h15.xml
- **Clé** : non. **Limite** : le flux ne garde que les ~15 derniers items (vérifié sur speeches.xml). Il faut donc **interroger régulièrement et archiver localement**. Pas d'historique via RSS.
- Pas de flux FOMC dédié : utiliser `press_monetary.xml`.

### 4.2 Anticipations de taux : Atlanta Fed Market Probability Tracker ⚠️

Doc : https://www.atlantafed.org/research-and-data/data/market-probability-tracker · https://www.atlantafed.org/research-and-data/data/market-probability-tracker/what-to-know

- Probabilités implicites tirées des options sur futures SOFR du CME (« with permission from CME »), mises à jour chaque jour avec les données de la veille.
- La page cite « MPT Historical Data » et « MPT Source Code », mais l'URL exacte du fichier n'est pas ressortie. ⚠️ À récupérer à la main sur la page.
- **Clé** : non. **Fiabilité** : bonne (Fed d'Atlanta). Granularité quotidienne, suffisante pour du swing.
- CME FedWatch : pas d'API gratuite identifiée. ❌

---

## 5. Contexte de marché

### 5.1 VIX : Cboe ✅

Doc : https://www.cboe.com/tradable_products/vix/vix_historical_data

- CSV quotidien depuis 1990, « Updated Daily ». Lien indiqué par la page : `https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv` (⚠️ URL issue d'un résumé, à tester). Même schéma pour VIX9D, VVIX, etc.
- **Clé** : non. **Conditions** : données fournies « for the convenience of site visitors », sans garantie d'exactitude. ⚠️ Usage perso.
- **Limite** : quotidien uniquement. Pour du VIX intraday, voir si Capital.com cote un CFD VIX via `GET /markets` (⚠️ non vérifié).

### 5.2 Dollar : DXY ⚠️

- Le DXY est un indice ICE propriétaire. Pas de source gratuite officielle. ❌
- Proxy : **FRED `DTWEXBGS`** (Nominal Broad U.S. Dollar Index), quotidien depuis 2006-01-02, source Fed (H.10). https://fred.stlouisfed.org/series/DTWEXBGS ⚠️ La page affiche à la fois un tag « Public Domain » et un avertissement copyright. Publication avec environ une semaine de retard (dernier point au 13/11 consulté le 20/11), donc à utiliser comme régime et non comme signal intraday.
- En live : si Capital.com cote un CFD sur le dollar, le lire via l'API (⚠️ à vérifier avec `GET /markets`).

### 5.3 Taux US 2 et 10 ans : Trésor US ✅

Doc : https://home.treasury.gov/treasury-daily-interest-rate-xml-feed

- `https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml?data=daily_treasury_yield_curve&field_tdr_date_value=2026` (paramètres `field_tdr_date_value=yyyy`, `field_tdr_date_value_month=yyyymm`, ou `all&page=N`, 300 lignes par page).
- Historique : courbe par depuis 1990. Archives CSV : https://home.treasury.gov/interest-rates-data-csv-archive
- **Clé** : aucune mentionnée. **Fiabilité** : source officielle. Quotidien (fin de journée).
- Alternative : séries FRED des taux constant-maturity (domaine public, à confirmer dans les notes de série).

### 5.4 Spreads de crédit ⚠️

- FRED `BAMLH0A0HYM2` (ICE BofA US High Yield OAS) : « Starting in April 2026, this series will only include 3 years of observations ». Copyright ICE : « for your internal use only », aucune redistribution. https://fred.stlouisfed.org/series/BAMLH0A0HYM2
- Conséquence : utilisable en **live** comme filtre de régime (usage interne), mais **pas pour un backtest long** (seulement 3 ans glissants). ❌ Pas d'alternative gratuite avec un long historique identifiée.

### 5.5 Secteurs ⚠️

- Pas de source officielle gratuite pour les indices sectoriels S&P (propriétaires S&P DJI). ❌
- Proxy : ETF sectoriels SPDR (XLK, XLF…) en quotidien via yfinance, en **usage perso** seulement. Ou, si Capital.com cote ces ETF ou des CFD sectoriels, via son API (⚠️ à vérifier avec `GET /markets`).

---

## 6. Fondamentaux des entreprises du S&P 500

### 6.1 SEC EDGAR : résultats publiés ✅

Doc : https://www.sec.gov/search-filings/edgar-application-programming-interfaces · règles d'accès : https://www.sec.gov/os/accessing-edgar-data

- `https://data.sec.gov/submissions/CIK##########.json` (historique des dépôts)
- `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json` (tous les faits XBRL)
- `https://data.sec.gov/api/xbrl/companyconcept/CIK##########/us-gaap/<Concept>.json`
- `https://data.sec.gov/api/xbrl/frames/us-gaap/<Concept>/USD/CY2019Q1I.json` (une valeur par société pour une période)
- Bulk nocturne : https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip et https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip
- **Clé** : non (« do not require any authentication or API keys »). **Quota** : 10 requêtes/s. **User-Agent** à déclarer (nom + email de contact, à configurer par l'utilisateur, pas dans le code versionné).
- Mise à jour en temps réel dans la journée. Les champs `filed` donnent la date de dépôt, ce qui permet un usage point-in-time.
- **Fiabilité** : source primaire. Ne contient **pas** les estimations des analystes.

### 6.2 Estimations des analystes et calendrier de résultats ❌/⚠️

- Le consensus EPS historique n'est disponible gratuitement nulle part de façon fiable. ❌
- Calendrier des résultats : Alpha Vantage `EARNINGS_CALENDAR` (clé gratuite, 25 req/jour ; ⚠️ statut gratuit/premium de cette fonction non confirmé). yfinance (usage perso).
- Pour un bot sur l'indice, l'utile est surtout de **connaître les dates de résultats des 10 plus grosses pondérations** (filtre de risque), plus que leurs estimations.

### 6.3 Pondérations du S&P 500 ⚠️

- Fichier holdings quotidien du SPY : https://www.ssga.com/library-content/products/fund-data/etfs/us/holdings-daily-us-en-spy.xlsx (lien trouvé sur la page SPY de SSGA).
- **Clé** : non. **Conditions** : la page interdit toute reproduction ou transmission à des tiers sans accord écrit. Usage **interne** seulement.
- Pas d'historique : télécharger et archiver chaque jour.

---

## 7. Actualités financières ⚠️

- **RSS Fed** (§4.1) et **EDGAR** (8-K des grosses pondérations via `submissions`) : sources primaires, gratuites, sans clé.
- **GDELT DOC 2.0** : `https://api.gdeltproject.org/api/v2/doc/doc?query=...&mode=...`, sans clé mentionnée, recherche depuis le 1/1/2017, mais les listes d'articles ne couvrent que les 3 derniers mois de la fenêtre, 250 résultats max. Modes `TimelineTone` / `TimelineVol` utiles pour un indicateur de sentiment. https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/ · https://blog.gdeltproject.org/doc-2-0-updates-1-5-year-searching-and-updated-mobile-interface/ ⚠️ Docs datées de 2017-2018, comportement actuel à tester.
- **Alpha Vantage `NEWS_SENTIMENT`** : listé dans la doc, quota de 25 req/jour. ⚠️ Statut gratuit non confirmé.
- **Finnhub** : la page tarifaire n'a pas pu être lue (rendue en JavaScript). ⚠️ Plan gratuit à vérifier à la main avant de l'inclure.

---

## 8. Les trous (aucune source gratuite)

1. **Consensus des publications macro, surtout en historique** (CPI, NFP, PCE, ISM…). C'est le trou principal. Contournements : filtre de calendrier, nowcast Cleveland pour l'inflation, surprise vs modèle sur ALFRED, et archivage maison à partir d'aujourd'hui.
2. **ISM** (manufacturier et services) : données propriétaires, pas d'API gratuite.
3. **Consensus EPS et révisions d'estimations** des analystes.
4. **DXY officiel (ICE)** : seulement le proxy Fed Broad Dollar, publié avec du retard.
5. **Spreads de crédit avec long historique** : ICE BofA sur FRED limité à 3 ans et sous copyright.
6. **Indices sectoriels S&P officiels** : seulement des proxys ETF.
7. **Anticipations de taux en intraday** (FedWatch) : seulement le quotidien de l'Atlanta Fed.
8. **Historique long de l'US500 au prix Capital.com** : à mesurer. Sinon Dukascopy, avec un écart de prix à calibrer.

---

## 9. Points à vérifier avant de coder

- [ ] Capital.com `GET /markets/US500` : `minDealSize`, `minGuaranteedStopDistance`, prime GSL, `overnightFee`, spread médian par heure.
- [ ] Capital.com : plafond réel de `max` et profondeur d'historique par résolution pour l'epic US500.
- [ ] Capital.com `GET /markets` : CFD disponibles pour VIX, dollar et secteurs.
- [ ] Dukascopy : identifiant exact de l'US 500 et date de début.
- [ ] URL de l'historique de l'Atlanta Fed MPT.
- [ ] URL CSV du VIX (Cboe) : tester le téléchargement.
- [ ] Quotas FRED dans la doc officielle (https://fred.stlouisfed.org/docs/api/api_key.html) et conditions (https://fred.stlouisfed.org/legal/).
- [ ] Guide de l'API BEA (https://apps.bea.gov/API/docs/index.htm) : quotas.
- [ ] Finnhub et Alpha Vantage : ce qui est réellement gratuit (calendrier, news).

## 9 bis. Implémentation (Data Engine)

Connecteurs dans `src/bot_trading/marketdata/sources/`, testés avec des réponses
simulées. **Aucun n'a encore été exécuté contre la vraie API** : les points ⚠️
ci-dessus restent à vérifier au premier `python -m bot_trading.marketdata refresh`.
Hypothèses codées et paramétrables : symbole Dukascopy `USA500IDXUSD` et diviseur
de prix 1000 ; heures de publication FRED (`RELEASE_TIMES_ET`) ; VIX et Trésor
disponibles à 18:00 New York ; `acceptanceDateTime` EDGAR lu en heure de New York ;
noms des champs du WebSocket Capital.com ; heures de cotation US500 (modèle CME).

## 10. Règles d'usage pour le projet

- Aucune clé ni identifiant dans le dépôt : variables d'environnement ou fichier `.env` ignoré par git.
- Chaque source = un module d'ingestion avec cache local (SQLite ou Parquet) et horodatage de collecte, pour pouvoir rejouer en point-in-time.
- Archiver localement ce qui n'a pas d'historique : RSS Fed, holdings SPY, MPT Atlanta, nowcasts Cleveland, et tout consensus collecté.
- Respecter les quotas avec un limiteur par source (FRED 120/min, SEC 10/s, Capital.com 10/s, BLS 500/jour).
- Données sous copyright (ICE, SSGA, Yahoo) : usage interne uniquement, ne jamais les committer dans le dépôt.
- Le journal de trading personnel (`TJ_*.xlsx`) n'est jamais commité ; il est analysé par `research/journal_analysis.py`.
