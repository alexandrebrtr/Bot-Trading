# EXPERIMENTS.md — Registre des expériences

> Version 0.1, 20/11/2026. Toute expérience est déclarée ici **avant** d'être lancée (hypothèse, métrique, critère de succès), puis complétée avec son résultat. Le nombre total d'essais sert à corriger le surapprentissage (Sharpe déflaté).
> Légende : ✅ confirmé · 🧪 hypothèse · 🎛️ optimisable · 🏦 broker · ❓ à valider.

## Protocole commun

| Élément | Règle | Statut |
|---|---|---|
| Données | US500 Dukascopy calibré + Capital.com, D1→M5 ; macro point-in-time (ALFRED) | ✅ |
| Coûts | Spread bid/ask réel ou médian par heure, prime GSL si le stop est touché, financement 4 % + SOFR / 360 sur le nominal, conversion 0,7 % | 🏦 |
| Découpage | Walk-forward : optimisation 🎛️ 24 mois, test 6 mois, pas de 6 mois. Réserve finale : 12 derniers mois, ouverte une seule fois. | ✅ |
| Métriques principales | Espérance nette en R, profit factor, Calmar, Sortino, drawdown max, nombre de trades | ✅ |
| Métriques secondaires | Win rate, durée moyenne, exposition, coût du financement en % du P&L, part des `NO_TRADE` | ✅ |
| Critère minimal | ≥ 30 trades hors échantillon, PF ≥ 1,3, espérance > 0 après frais, drawdown < 35 % | 🧪 |
| Benchmarks | Buy & hold US500 même marge ; même stratégie sans couche fondamentale ; entrées aléatoires avec les mêmes stops | ✅ |
| Interdits | Regard vers le futur, pondérations actuelles appliquées au passé, consensus historique, sélection a posteriori de la période | ✅ |

## Expériences prévues

| ID | Hypothèse | Variable testée | Succès si | Statut |
|---|---|---|---|---|
| EXP-01 | Le filtre de régime fondamental améliore l'espérance nette | Avec / sans F1–F7 (même technique) | Calmar +20 % et DD plus faible hors échantillon | 🧪 |
| EXP-02 | Le filtre d'événements macro réduit les pertes extrêmes | Fenêtre F11 (X, Y) | Pire perte et queue à 5 % réduites sans baisse du PF | 🧪 |
| EXP-03 | Meilleure règle d'entrée FVG | Retour dans le FVG / mitigation 50 % / mitigation complète + rejet / réintégration / cassure FVG baissier + retest / FVG + BOS | Espérance la plus haute, stable sur ≥ 3 fenêtres | 🧪 |
| EXP-04 | Timeframe du setup | H1 vs H4 (confirmation M15 vs M5) | Idem | 🧪 |
| EXP-05 | Seuils de qualification FVG | Taille / ATR (G4), âge (G5), score G3 | Plateau de performance (pas un pic isolé) | 🎛️ |
| EXP-06 | Définition des Order Blocks | Mèche vs corps, impulsion d × ATR, retests r | Idem | 🎛️ |
| EXP-07 | Gestion de sortie | TP à la résistance vs stop suiveur vs sortie partielle ; break-even a ; sortie temporelle N | Espérance nette après financement | 🎛️ |
| EXP-08 | Distance de stop vs risque/trade | Invalidation H1 vs H4, plafonnée à 2,5 % ; risque 2–20 % (après R8) | Calmar maximal avec DD < 35 % | 🎛️ |
| EXP-09 | Les heures d'entrée du journal (10–13 h, 15–16 h, heure de Paris) sont meilleures | Filtre horaire on/off | Gain significatif hors échantillon (sinon abandonné) | 🧪 (journal, n faible) |
| EXP-10 | Le profil du journal (TP ~15 pts, stop lointain) est perdant une fois le stop garanti à 2,5 % imposé | Réplique du style manuel | Confirmer ou infirmer l'analyse de JOURNAL_ANALYSIS §4 | 🧪 |
| EXP-11 | Le ton de la Fed (classifieur) ajoute de l'information au-delà des taux 2 ans | Score F4 on/off | Gain hors échantillon | 🧪 |
| EXP-12 | Écart Dukascopy / Capital.com acceptable | Signaux sur la période commune | ≥ 90 % de signaux identiques | 🏦 |

Exécution : EXP-01, EXP-03, EXP-04, EXP-07 et EXP-08 se lancent désormais sur la stratégie complète (`bot_trading.strategy.DecisionStrategy`) en faisant varier `TechnicalParams` (variante d'entrée, timeframes du setup), `ExitParams` (méthodes de sortie) et `DecisionParams` (risque, TP). `python -m bot_trading backtest --walk-forward` lance déjà la grille EXP-03 (4 variantes d'entrée = 4 essais à compter).

## Journal des résultats

| ID | Date | Version du code | Essais cumulés | Résultat hors échantillon | Décision |
|---|---|---|---|---|---|
| — | — | — | 0 | — | — |
