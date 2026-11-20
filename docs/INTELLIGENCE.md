# Analyse fondamentale : agents macro, entreprises et régime

> Phase 2, 20/11/2026. Code : `src/bot_trading/intelligence/`. Tests :
> `tests/test_intelligence.py`, `tests/test_llm_guard.py`.

## Rôle dans le pipeline

```
PointInTimeData (connecteurs)          ← seule porte d'entrée des données
   ├─ MacroIntelligenceAgent   → MacroAssessment   (F1 à F5, F8, F11, F12)
   ├─ EquityIntelligenceAgent  → EquityAssessment  (F7)
   └─ MarketRegimeEngine       → RegimeAssessment  (F6 + synthèse, F10)
                                     │ regime, allow_new_entries, size_multiplier,
                                     │ min_setup_quality, event_blackout
                                     ▼
                         décideur (à venir) → OrderRequest → Risk Engine (veto)
```

Aucun module de `intelligence/` n'importe `risk`, `broker` ou `core` (vérifié par
un test sur les imports). Les sorties ne contiennent aucun champ d'ordre.

## Interface de données

`intelligence/data.py` définit le protocole `PointInTimeData`, à implémenter par
les connecteurs :

| Méthode | Renvoie |
|---|---|
| `series(series_id, asof)` | dernière vintage connue à `asof` pour chaque période |
| `fed_communications(asof, since)` | communiqués et discours publiés dans [since ; asof] |
| `scheduled_events(asof, start, end)` | CPI, NFP, PCE, FOMC, résultats… dont la date était connue à `asof` |
| `earnings(asof, ticker)` | rapports EDGAR déposés avant `asof` |
| `index_weights(asof)` | dernier instantané de pondérations archivé avant `asof` |

Tout horodatage est en UTC avec fuseau (un horodatage naïf est refusé).
`available_at` est l'heure de **publication**, pas la période observée.
`InMemoryPITStore` est l'implémentation de référence. Les identifiants de
séries sont dans `intelligence/series.py` (codes FRED/ALFRED quand ils existent).

## Sorties (Pydantic)

Chaque évaluation contient : `score` ∈ [-1 ; 1], `bias` (BULLISH / NEUTRAL /
BEARISH), `uncertainty` ∈ [0 ; 1], `subscores` (composante, score, poids,
justification, métriques, sources), trois `scenarios` (central, haussier,
baissier, probabilités sommant à 1), des `invalidations` vérifiables par machine
(`metric`, `operator`, `threshold`) et les `sources` datées.

Le modèle refuse à la construction toute source dont `available_at` > `asof`.

`RegimeAssessment` ajoute :

| Champ | Règle |
|---|---|
| `regime` | `RISK_ON` si score ≥ 0,20, `RISK_OFF` si ≤ −0,20, sinon `NEUTRE` ; hystérésis 0,05 avec `previous` |
| `allow_new_entries` | faux en `RISK_OFF` ou dans une fenêtre d'annonce (contrôlé par le schéma) |
| `size_multiplier` | 1 / 0,5 / 0 ; toujours ≤ 1 (le régime réduit le risque, ne l'augmente jamais). Avec 0,01 lot, `NEUTRE` revient à la taille minimale |
| `min_setup_quality` | 0,5 / 0,8 / 1,0 (F10 : `NEUTRE` n'accepte que les meilleurs setups) |
| `event_blackout` | à passer tel quel à `MarketState.event_blackout` du Risk Engine |

Forçages : VIX ≥ 35 ou spreads HY +100 pb en 20 séances → `RISK_OFF` ; règle de
Sahm déclenchée, biais macro baissier ou incertitude > 0,75 → au mieux `NEUTRE`.
`check_invalidations(assessment, metrics)` liste les invalidations déclenchées par
des données plus récentes.

Tous les seuils et poids sont dans `MacroConfig`, `EquityConfig` et
`RegimeConfig` (🎛️, à optimiser en walk-forward, voir `EXPERIMENTS.md`).

## Composantes

| Composante | Calcul | Sources |
|---|---|---|
| F1 inflation | Core CPI et Core PCE annualisés 3 et 6 mois vs 2,5 % ; accélération ; surprise | ALFRED, nowcast Cleveland |
| F2 emploi | Règle de Sahm, moyenne 3 mois des NFP, claims 4 sem. vs 26 sem. | ALFRED |
| F3 activité | Production industrielle 6 mois, ventes au détail réelles, PIB réel | ALFRED |
| F4 Fed | Lexique hawkish/dovish avec négations, **variation** vs intervention précédente du même orateur, poids par orateur (FOMC, Warsh, président…) | RSS Fed archivé |
| F5 taux | Variation 20 séances du 2 ans et du 10 ans, baisses attendues (Atlanta Fed MPT) | Trésor, FRED, Atlanta Fed |
| F6 intermarchés | VIX (niveau, vs moyenne 50 j, structure VIX/VIX3M), spreads HY, dollar large, pente 10−2 ans | Cboe, FRED, Trésor |
| F7 entreprises | CA, BPA, marge et guidance des 10 plus grosses pondérations (a/a), accélération du BPA ; breadth ; cycliques vs défensives ; prime de risque | EDGAR, holdings SPY archivés |

Poids : macro 45 %, entreprises 25 %, intermarchés 30 % (le fondamental domine).
Une composante sans donnée fraîche est marquée `available = false`, exclue du
score et augmente l'incertitude : rien n'est inventé.

**Lecture non mécanique (F8).** Quand l'inflation sous-jacente dépasse 3 % et que
l'emploi crée plus de 200 k postes par mois, la composante emploi est pénalisée
(risque de taux plus hauts). Les composantes F4 et F5 traduisent la réaction
effective des anticipations.

**Pas de consensus (F12).** La surprise d'inflation est mesurée contre le nowcast
Cleveland **publié avant** la donnée ; à défaut, contre la moyenne des
12 variations précédentes (z-score). La surprise des résultats est l'accélération
du BPA vs les 4 trimestres précédents. ISM : non intégré (propriétaire),
signalé dans `warnings`.

**Pondérations (F9).** L'agent entreprises n'utilise que l'instantané de
pondérations archivé avant `asof`. Sans instantané, il passe en poids égaux sur
un univers fourni et le signale. Il n'applique jamais les pondérations actuelles
au passé.

**Fenêtres d'annonce (F11).** CPI, NFP, PCE, FOMC, PIB, ventes au détail :
[−12 h ; +2 h]. Résultats des 5 plus grosses pondérations : [−6 h ; +18 h].

## LLM optionnel

- Interface `LLMAnalyst` ; par défaut `NullAnalyst`. `OpenAIResponsesAnalyst`
  appelle `POST https://api.openai.com/v1/responses` avec une sortie JSON
  stricte, `store: false` et aucun outil.
- Activation : `LLM_ENABLED=1`, `OPENAI_API_KEY`, `OPENAI_MODEL` dans `.env`.
- **Jamais en backtest** : le modèle connaît la suite de l'histoire. Les agents
  sont en mode backtest par défaut ; il faut passer `mode=TradingMode.PAPER`.
- Rôle limité à la lecture du ton Fed. Sa sortie est validée (bornes, champs
  interdits, identifiants cités existants) et ne pèse au plus que 30 % de la
  composante F4, soit un poids d’environ 2,7 % dans le score de régime. En cas d'erreur, retour
  au calcul déterministe.
- Contenu externe : normalisé, caractères invisibles retirés, motifs
  d'injection neutralisés et signalés dans `warnings`, tronqué, encodé en JSON
  dans un bloc « données non fiables » séparé des instructions.
- Secrets : valeurs des variables sensibles et motifs de clés masqués ; un
  envoi qui en contiendrait encore est refusé. La clé n'est que dans l'en-tête
  `Authorization` et n'apparaît pas dans `repr`.

## Limites connues

- Les probabilités des scénarios ne sont pas calibrées : elles ordonnent les
  scénarios. À recalibrer sur les fréquences observées en backtest.
- Le lexique Fed est volontairement simple ; il sera évalué contre les
  réactions du 2 ans le jour des interventions avant d'en augmenter le poids.
- Breadth, secteurs et rendement des bénéfices de l'indice n'ont pas encore de
  connecteur : ces composantes restent `available = false`.
