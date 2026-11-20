# STRATEGY.md — Stratégie US500 Long Only, Trend Following

> Version 0.1 (Phase 0), 20/11/2026. Document de référence pour le code : tout signal doit pouvoir être relié à une règle ci-dessous.
> Sources : mission V3 d'Alexandre, journal 2026 (`docs/JOURNAL_ANALYSIS.md`), doc Capital.com (`docs/CAPITAL_COM.md`).

Légende utilisée dans tous les documents :

| Statut | Sens |
|---|---|
| ✅ **Confirmé** | Règle demandée explicitement par Alexandre ou observée de façon constante dans le journal. Non négociable sans son accord. |
| 🧪 **Hypothèse** | Idée plausible, à valider par backtest hors échantillon avant usage en réel. |
| 🎛️ **Optimisable** | Paramètre numérique dont la valeur sera choisie par backtest (walk-forward), avec une plage autorisée. |
| 🏦 **Broker** | Contrainte imposée par Capital.com ou la réglementation. |
| ❓ **À valider** | Décision qu'Alexandre doit trancher. |

---

## 1. Principe

Le bot achète l'US500 (CFD Capital.com, epic `US500`) uniquement quand **le contexte fondamental est haussier** et qu'un **retracement technique** offre une entrée avec une invalidation proche. Sans ces deux conditions, la décision est `NO_TRADE`.

| # | Règle | Statut |
|---|---|---|
| S1 | Long only. Les shorts sont désactivés par défaut. | ✅ |
| S2 | Le fondamental décide de la direction et de l'autorisation de trader. La technique ne fait qu'optimiser l'entrée, le stop et la sortie. | ✅ |
| S3 | L'absence d'opportunité donne `NO_TRADE`. Aucun quota de trades. | ✅ |
| S4 | Objectif : performance nette ajustée au risque (Calmar, Sortino, espérance nette en R), pas le win rate. | ✅ |
| S5 | Horizon swing : quelques heures à plusieurs jours, overnight autorisé. | ✅ |
| S6 | Ne jamais reproduire le profil « petit take profit, stop lointain » du journal (R:R médian 0,03). | ✅ (déduit du journal, voir §6) |

## 2. Pipeline de décision (top-down)

```
Régime fondamental (quotidien + événements)
   └─ Biais D1 (tendance principale)
        └─ Structure H4 (BOS, OB, FVG majeurs, invalidation)
             └─ Setup H1 (zone d'entrée candidate)
                  └─ Confirmation M15 (réaction mesurable dans la zone)
                       └─ Exécution M5 (prix, stop, taille, spread)
                            └─ Risk Engine (veto final)
```

Chaque étage peut répondre `NO_TRADE` et doit enregistrer **pourquoi** (journal de décision structuré). Un étage inférieur ne peut jamais contredire un étage supérieur. ✅

## 3. Couche fondamentale

### 3.1 Score de régime (sortie : `RISK_ON`, `NEUTRE`, `RISK_OFF`)

| # | Composante | Données (gratuites) | Statut |
|---|---|---|---|
| F1 | Tendance de l'inflation : CPI, Core CPI, PCE, Core PCE (niveau, dynamique 3 et 6 mois, surprise vs nowcast Cleveland Fed) | ALFRED, Cleveland Fed | 🧪 |
| F2 | Emploi : NFP, chômage, jobless claims (dynamique, pas le chiffre isolé) | ALFRED, DOL | 🧪 |
| F3 | Activité : PIB, production industrielle, retail sales, consommation | ALFRED | 🧪 |
| F4 | Fed : ton hawkish/dovish des communiqués et discours (dont Kevin Warsh), comparé à la déclaration précédente du même intervenant | RSS Fed archivé + classifieur | 🧪 |
| F5 | Anticipations de taux : probabilités implicites (Atlanta Fed MPT), taux 2 ans | Atlanta Fed, Trésor | 🧪 |
| F6 | Intermarchés : VIX (niveau et pente), dollar large (proxy DXY), courbe 2-10 ans, spreads de crédit HY | Cboe, FRED, Trésor | 🧪 |
| F7 | Résultats des plus grosses pondérations (dates, surprise vs historique, guidance) et breadth | EDGAR, holdings SPY archivés | 🧪 |

Règles fixes :

| # | Règle | Statut |
|---|---|---|
| F8 | Une bonne statistique n'est jamais lue mécaniquement comme haussière : l'effet passe par les anticipations de taux et la réaction du marché (ex. emploi trop fort → taux en hausse → négatif pour les valorisations). | ✅ |
| F9 | Chaque donnée est utilisée **point-in-time** (vintage disponible à la date de décision). Aucune pondération actuelle n'est appliquée au passé. | ✅ |
| F10 | `RISK_OFF` interdit toute nouvelle entrée. `NEUTRE` n'autorise que les setups de qualité maximale avec une taille minimale. | 🧪 |
| F11 | Filtre d'événements : pas d'entrée dans la fenêtre [−X h, +Y h] autour de CPI, NFP, PCE, FOMC et des résultats des 5 plus grosses pondérations. | 🎛️ X ∈ [0 ; 24], Y ∈ [0 ; 4] |
| F12 | Le consensus des économistes n'est pas disponible gratuitement en historique : aucun backtest ne peut utiliser une « surprise vs consensus ». La surprise est mesurée vs nowcast (inflation) ou vs un modèle simple. | 🏦/données |
| F13 | Le score fondamental est d'abord **déterministe et explicable** (somme pondérée de sous-scores). Un LLM peut résumer les discours, mais sa sortie est un sous-score borné et journalisé, jamais un ordre. | ✅ (sécurité) |

### 3.2 Questions ouvertes

- ❓ **Q1.** Poids relatif fondamental / technique : le fondamental est-il un simple filtre (autorise ou non) ou module-t-il aussi la taille de position ? Proposition par défaut : filtre + modulation de la taille dans les limites de `RISK_POLICY.md`.
- ❓ **Q2.** Faut-il couper une position ouverte quand le régime passe à `RISK_OFF` ? Proposition par défaut : resserrer le stop au break-even ou au dernier plus bas H1, ne pas sortir au marché.

## 4. Couche technique

### 4.1 Tendance et structure

| # | Règle | Statut |
|---|---|---|
| T1 | D1 haussier = clôture au-dessus de la MM D1 (longueur 🎛️ 50 à 200) **et** structure de plus hauts / plus bas croissants. Sinon pas de long. | 🧪 |
| T2 | H4 : dernier Break of Structure haussier confirmé (clôture au-dessus d'un swing high). Le dernier swing low H4 est l'invalidation swing. | 🧪 |
| T3 | Éviter les entrées tardives : distance au-dessus de la MM D1 ≤ k × ATR D1. | 🎛️ k ∈ [1 ; 4] |
| T4 | Éviter les résistances : pas d'entrée si un FVG baissier H4/D1 non mitigé ou un plus haut majeur est à moins de m × le risque. | 🎛️ m ∈ [1 ; 3] |
| T5 | Swings détectés par fractales à n bougies, confirmés seulement après clôture de la n-ième bougie suivante (aucun regard vers le futur). | 🎛️ n ∈ [2 ; 5] |

### 4.2 Fair Value Gaps (moteur déterministe)

| # | Règle | Statut |
|---|---|---|
| G1 | FVG haussier : `low[i] > high[i−2]`, zone = [high[i−2] ; low[i]], confirmé à la **clôture** de la bougie i. | ✅ (définition fournie) |
| G2 | FVG baissier : `high[i] < low[i−2]`, zone = [high[i] ; low[i−2]]. Sert surtout à repérer les obstacles. | ✅ |
| G3 | Qualification : taille / ATR, force de l'impulsion, timeframe, alignement, proximité d'un OB, BOS associé, âge, mitigation (%), nombre de retests, distance à la résistance. Score borné [0 ; 1]. | 🧪 (pondérations 🎛️) |
| G4 | Taille minimale du FVG. | 🎛️ ∈ [0,1 ; 1,0] × ATR |
| G5 | Âge maximal du FVG. | 🎛️ H1 : 10 à 100 bougies ; H4 : 5 à 50 |
| G6 | Invalidation : clôture sous la borne basse du FVG haussier. | 🧪 |
| G7 | Pas d'entrée au premier contact quand la variante exige une confirmation. | ✅ |

Variantes d'entrée à tester (voir `EXPERIMENTS.md`) : retour dans le FVG, mitigation à 50 %, mitigation complète puis rejet, réintégration haussière, cassure d'un FVG baissier puis retest, FVG + BOS.

### 4.3 Order Blocks

| # | Règle | Statut |
|---|---|---|
| O1 | OB haussier : dernière bougie baissière avant une impulsion haussière ayant produit un BOS confirmé. Zone = [low ; high] de la bougie (variante : corps seul 🎛️). | 🧪 (définition candidate fournie) |
| O2 | Impulsion significative : déplacement ≥ d × ATR en ≤ b bougies. | 🎛️ d ∈ [1 ; 3], b ∈ [1 ; 5] |
| O3 | Un OB est invalidé par une clôture sous sa borne basse, et « consommé » après r retests. | 🎛️ r ∈ [1 ; 3] |
| O4 | Bonus de qualité si l'OB contient ou touche un FVG du même timeframe. | 🧪 |

### 4.4 Confirmation et exécution

| # | Règle | Statut |
|---|---|---|
| E1 | Setups identifiés en H1/H4, confirmés en M15 ou M5. | ✅ |
| E2 | Confirmation M15 : retour dans la zone + bougie de rejet (mèche basse ≥ p % du range) **ou** micro-BOS haussier M15. | 🧪 (p 🎛️) |
| E3 | Exécution M5 : ordre au marché à la clôture de la bougie de confirmation, ou ordre limite dans la zone. | 🧪 |
| E4 | Stop garanti sous l'invalidation technique (bas de la zone − buffer), jamais plus loin que la limite de `RISK_POLICY.md` (≈ 2,5 % du prix). Si l'invalidation est plus loin : `NO_TRADE`. | ✅ |
| E5 | Pas d'entrée si spread > s × spread médian ou hors des heures de cotation normales. | 🎛️ s ∈ [1,5 ; 3] |
| E6 | R:R potentiel minimal jusqu'à la prochaine résistance. | 🎛️ ∈ [1,5 ; 4] |

### 4.5 Gestion de position et sortie

| # | Règle | Statut |
|---|---|---|
| X1 | Take profit facultatif. Par défaut : sortie partielle ou totale à la résistance H4/D1 suivante. | 🧪 |
| X2 | Passage au break-even après +a R, puis stop suiveur sous les swing lows H1. | 🎛️ a ∈ [0,5 ; 2] |
| X3 | Sortie temporelle : si +b R n'est pas atteint après N jours, sortie (le financement overnight coûte environ 0,02 % du nominal par nuit). | 🎛️ N ∈ [2 ; 15] |
| X4 | Le stop est **déplacé uniquement dans le sens favorable**. Le remonter vers l'entrée ne doit jamais faire sauter le statut garanti (à vérifier avec l'API, voir `RISK_POLICY.md`). | ✅ / 🏦 |

## 5. Ce que le bot reprend du trading manuel

| Observation du journal | Traduction dans le bot | Statut |
|---|---|---|
| Biais haussier sur l'indice US, trend following | S1, T1, T2 | ✅ |
| Entrées en M5 après lecture de la tendance | E1 à E3 | ✅ |
| Garder en swing un trade mal entré quand le scénario reste valable (05/10) | X2/X3 plutôt que coupure au premier recul | 🧪 |
| Sorties anticipées par manque de confiance, trades pendant les cours, clics erronés | Exécution automatique, aucune intervention manuelle pendant un trade | ✅ |
| Pertes lourdes sans stop | Stop garanti obligatoire | ✅ |
| Shorts fréquents | Désactivés | ✅ |

## 6. Ce que le bot ne reprend pas

- Le TP fixe de ~15 points et les stops à 6–15 % : espérance non démontrée et profil de risque incompatible avec un capital de 20 € (détail dans `docs/JOURNAL_ANALYSIS.md` §4).
- Les heures « favorables » du journal : échantillon trop faible, à tester comme hypothèse (EXP-09) et non à coder en dur.

## 7. Décisions à valider par Alexandre

| # | Question | Proposition par défaut |
|---|---|---|
| Q1 | Rôle du fondamental : filtre seul ou filtre + taille ? | Filtre + taille |
| Q2 | Que faire d'une position ouverte quand le régime devient `RISK_OFF` ? | Stop au break-even, pas de sortie forcée |
| Q4 | Accepter que le bot trade nettement moins souvent que le journal (quelques trades par mois) ? | Oui, conséquence de S3 et E4 |

Implémentation (Decision Engine, en attente de validation) : Q1 et Q2 suivent les propositions par défaut ci-dessus (filtre + taille ; en `RISK_OFF`, stop resserré sans sortie forcée, option `ExitParams.on_risk_off`). Le passage en `NEUTRE` exige un setup de qualité ≥ 0,8 et divise le risque par deux.

Décidé : pas de compte démo possible chez Capital.com (confirmé par Alexandre), le paper trading est local. Tous les trades `Live` du journal sont chez Capital.com (confirmé par Alexandre).
