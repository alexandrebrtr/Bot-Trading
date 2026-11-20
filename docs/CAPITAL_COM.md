# Capital.com : API, stop garanti, frais et faisabilité à 0,01 lot

> Vérifié le 20/11/2026 sur la documentation officielle. ✅ = lu dans la doc · ⚠️ = résumé partiel ou valeur non publiée, à lire dans `GET /api/v1/markets/US500` au démarrage · ❌ = la doc est muette.
> Sources : https://open-api.capital.com/ · https://help.capital.com/hc/en-us/articles/360016602919 · https://capital.com/en-eu/ways-to-trade/fees-and-charges/cfd-and-ko · https://capital.com/en-eu/ways-to-trade/pricing · https://help.capital.com/hc/en-us/articles/360016641939

## 1. API REST et WebSocket

| Élément | Détail | Statut |
|---|---|---|
| Base réelle | `https://api-capital.backend-capital.com/` | ✅ |
| Base démo | `https://demo-api-capital.backend-capital.com/`. La doc indique « a demo account can be used ». Alexandre n'a pas de compte démo : à revérifier dans l'app (voir STRATEGY Q5). | ✅ |
| Authentification | `POST /api/v1/session` avec le header `X-CAP-API-KEY` et `identifier` + `password` dans le corps (variante chiffrée RSA via `GET /session/encryptionKey`). Les headers de réponse `CST` et `X-SECURITY-TOKEN` sont valables 10 min après le dernier usage. | ✅ |
| Keep-alive | `GET /api/v1/ping` (authentifié), `GET /api/v1/time` (sans authentification) | ✅ |
| Quotas | 10 requêtes/s par utilisateur ; 1 ordre / 0,1 s ; `POST /session` : 1/s par clé ; 1 000 ordres/h sur le démo | ✅ |
| Lecture seule | `GET /markets`, `GET /markets/{epic}`, `GET /prices/{epic}`, `GET /positions`, `GET /accounts`, historique d'activité et de transactions (plage max 1 jour sur `/history/activity`), WebSocket | ✅ |
| Écriture (**interdite pendant le développement**) | `POST/PUT/DELETE /positions`, `POST/PUT/DELETE /workingorders`, `PUT /session`, `PUT /accounts/preferences`, `POST /accounts/topUp` | ✅ |
| Confirmation d'ordre | `POST /positions` renvoie un `dealReference` ; le statut réel se lit avec `GET /confirms/{dealReference}` (« a successful response does not always mean that the position has been successfully opened »). | ✅ |
| Historique de prix | `GET /prices/{epic}?resolution=MINUTE_5|MINUTE_15|HOUR|HOUR_4|DAY&max=...&from=...&to=...`, bid et ask séparés. `max` ≤ 1 000 d'après le résumé. Profondeur d'historique non documentée. | ⚠️ |
| WebSocket | `wss://api-streaming-capital.backend-capital.com/connect`, `marketData.subscribe` et `OHLCMarketData.subscribe` (40 epics max), ping au moins toutes les 10 min | ✅ |
| Champs `POST /positions` | `epic`, `direction` (BUY/SELL), `size`, `guaranteedStop`, `stopLevel` / `stopDistance` / `stopAmount`, `profitLevel` / `profitDistance` / `profitAmount`, `trailingStop`. `guaranteedStop=true` exige un stop et est incompatible avec `trailingStop` (et avec le mode hedging). | ⚠️ (résumé) |
| Règles de l'instrument | `GET /markets/US500` → `dealingRules` : `minDealSize`, `minSizeIncrement`, `minGuaranteedStopDistance`, `minStopOrProfitDistance`, `maxStopOrProfitDistance` ; `instrument` : `guaranteedStopAllowed`, `marginFactor`, `overnightFee`, `openingHours`, `currency`. | ⚠️ (noms à confirmer sur une réponse réelle) |

**Conséquence pour le code :** le service de données instancie un client qui ne connaît que des chemins `GET` et le WebSocket. Toute autre méthode HTTP lève une exception, testée en CI. L'exécution réelle sera un module séparé, désactivé par défaut, activé seulement en Phase 6 (voir `ROADMAP.md`).

## 2. Stop loss garanti (GSL)

| Point | Détail | Statut |
|---|---|---|
| Obligation | « In accordance with French regulations, all CFD positions must include a mandatory Guaranteed Stop Loss » pour les clients résidant en France. La règle « stop garanti obligatoire » est donc aussi une contrainte réglementaire. | ✅ |
| Prime | « A GSLO fee is only charged if the GSLO is triggered. » Rien n'est payé si le stop n'est pas touché. | ✅ |
| Calcul | Frais GSL = prime (%) × prix d'ouverture × taille. Exemple officiel : or, prime 0,03 %. | ✅ |
| Prime US500 | Non publiée dans l'extrait lu (US100 : 0,02 %). Hypothèse de travail : 0,03 %. À lire sur le ticket ou via l'API. | ⚠️ |
| Distance minimale | Non publiée sur les pages d'aide. Valeur par instrument : `dealingRules.minGuaranteedStopDistance`. | ⚠️ |
| Déplacer ou retirer un GSL | Non documenté. À tester avant la Phase 6 (sur une position réelle minimale, avec accord d'Alexandre). | ❌ |
| Effet sur la marge | Non documenté. | ❌ |

## 3. Frais

| Frais | Détail | Statut |
|---|---|---|
| Commission | Aucune (« Trading commission: NO FEE ») | ✅ |
| Spread | Dynamique, non publié pour l'US500. Hypothèse 0,6 point, **à mesurer** sur les bid/ask de l'API. | ⚠️ |
| Financement overnight | Frais Capital.com 4 %/an ± taux de référence (SOFR pour les indices US), divisé par 360 pour l'USD, appliqué au **nominal** et non à la marge. Un long paie frais + SOFR. Exemple officiel US100 : long 0,025 %/jour. | ✅ |
| Heure de prélèvement, triple jour pour les indices | Non précisés pour les indices. À lire dans `overnightFee` (`swapChargeTimestamp`, `swapChargeInterval`). | ❌ |
| Conversion EUR ↔ USD | 0,7 % du cours spot, inclus dans le taux appliqué au P&L, au financement et aux frais GSL | ✅ |
| Dépôt, retrait, compte | Gratuit ; pas de frais d'inactivité mentionnés | ✅ |
| Levier 20:1, seuil de liquidation, protection contre le solde négatif | Non couverts par les pages lues. Le 20:1 (marge 5 %) est la norme ESMA pour les indices majeurs. | ⚠️ |

Observé dans le journal : un short US500 de taille 1 gardé en swing (05/10) a coûté 1,75 € de frais, cohérent avec un financement de l'ordre de 0,02 à 0,03 % du nominal par nuit.

## 4. Faisabilité à 0,01 lot (capital 20 €)

Calcul : `python research/feasibility.py` (hypothèses : US500 = 7 520, EUR/USD = 1,16, spread 0,6 pt, SOFR 3,75 %, prime GSL 0,03 %). « 1 lot » = taille 1 = 1 USD par point, cohérent avec la marge d'environ 340 € indiquée par Alexandre (7 520 / 1,16 × 5 % = 324 €).

| Grandeur | Valeur à 0,01 lot |
|---|---|
| Valeur du point | 0,0086 € |
| Nominal | 64,83 € |
| Marge engagée | 3,24 € (16 % du capital) |
| Perte max = 50 % de la marge | **1,62 €** |
| → distance de stop max | **188 points = 2,50 % du prix** |
| Perte visée de 1,70 € | 197 points (2,62 %) : légèrement au-delà de la règle des 50 % |
| Prime GSL si le stop est touché | 0,02 € |
| Spread aller-retour | 0,005 € |
| Financement long | 0,014 € par nuit, 0,42 € pour 30 nuits |
| Perte totale si le stop à 188 pts est touché | 1,66 € = **8,3 % du capital** |
| Nombre de pertes max avant le drawdown de 50 % | **6** |
| Taille max permise par la marge | 0,06 lot |

Risque selon la distance du stop (0,01 lot) :

| Stop | 25 pts (0,33 %) | 50 pts (0,66 %) | 75 pts (1 %) | 100 pts (1,33 %) | 150 pts (2 %) | 188 pts (2,5 %) |
|---|---|---|---|---|---|---|
| Perte | 0,24 € | 0,46 € | 0,67 € | 0,89 € | 1,32 € | 1,65 € |
| % du capital | 1,2 % | 2,3 % | 3,4 % | 4,4 % | 6,6 % | 8,2 % |

**Conclusions :**

1. **C'est faisable** techniquement : 0,01 lot accepté, frais négligeables par rapport au risque (le spread et la prime GSL représentent environ 1,5 % de la perte max).
2. **La règle « 50 % de la marge » équivaut à un stop maximal de 2,5 % du prix** à 20:1, quelle que soit la taille. Une perte visée de 1,70 € la dépasse de 5 % : retenir 1,62 € (ou recalculer à chaque trade).
3. **La taille minimale impose un risque élevé** : au stop maximal, un seul trade risque 8 % du capital et 6 pertes suffisent à atteindre le drawdown de 50 %. Pour ramener le risque à environ 2–3 % du capital, le stop doit être à 50–75 points (invalidation H1 plutôt que H4). C'est pourquoi `RISK_POLICY.md` plafonne aussi le risque en % du capital.
4. **Le financement pèse sur le swing long** : 30 nuits coûtent 0,42 €, soit 26 % d'une perte max. D'où la sortie temporelle X3 de `STRATEGY.md`.
5. **L'exposition max de 1 lot est hors de portée** : la marge limite à 0,06 lot au départ. La limite de 1 lot ne deviendra active qu'au-delà d'environ 340 € de capital.
6. **Les vraies valeurs** (`minDealSize`, `minGuaranteedStopDistance`, prime GSL, spread, `overnightFee`) doivent être lues au démarrage via `GET /markets/US500`. Le Risk Engine refuse de trader si elles sont absentes.
