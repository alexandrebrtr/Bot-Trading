# ROADMAP.md — Feuille de route

> Version 0.1, 20/11/2026. Chaque phase a un critère de sortie mesurable. On ne passe pas à la suivante sans lui.
> Légende : ✅ confirmé · 🧪 hypothèse · 🎛️ optimisable · 🏦 broker · ❓ à valider.

## Phase 0 — Cadrage (en cours)

- [x] Analyse du journal 2026 (`docs/JOURNAL_ANALYSIS.md`)
- [x] Vérification de l'API, du stop garanti et des frais Capital.com (`docs/CAPITAL_COM.md`)
- [x] Faisabilité à 0,01 lot (`research/feasibility.py`)
- [x] STRATEGY, RISK_POLICY, ROADMAP, EXPERIMENTS, DATA_SOURCES
- [x] Réponses d'Alexandre : risque max 20 % par trade (après la règle des 50 %), trades Live tous chez Capital.com
- [ ] Questions restantes : Q1, Q2, Q4, Q-R3, Q-R4

**Sortie :** documents validés par Alexandre.

## Phase 1 — Données (lecture seule)

- Client Capital.com limité aux `GET` et au WebSocket (test qui échoue si une autre méthode est possible).
- Mesures : profondeur d'historique par résolution, spread médian par heure, valeurs de `dealingRules` et `overnightFee` pour US500.
- Historique long : Dukascopy US 500, calibré sur la période commune avec Capital.com.
- Ingestion point-in-time : ALFRED, calendrier FRED, Trésor, Cboe VIX, RSS Fed (archivage), Atlanta Fed MPT, EDGAR.
- Stockage Parquet/SQLite horodaté (date de collecte + date de disponibilité).

**Sortie :** jeu de données D1→M5 sur ≥ 5 ans, contrôles qualité (trous, doublons, sessions) passés, écart Dukascopy/Capital.com chiffré.

## Phase 2 — Moteurs déterministes (fil « code », en parallèle)

- Risk Engine (`RISK_POLICY.md`), broker simulé reproduisant Capital.com (spread bid/ask, GSL et prime, financement, conversion 0,7 %, marge, arrondis de taille).
- Moteurs FVG, Order Blocks, swings/BOS, sans regard vers le futur (tests unitaires dédiés).
- Score fondamental v0 explicable.

**Sortie :** tests unitaires verts, aucun regard vers le futur détecté par un test « données tronquées = mêmes signaux ».

## Phase 3 — Backtest et recherche

- Moteur événementiel multi-timeframe, frais réels.
- Expériences de `EXPERIMENTS.md`, walk-forward : optimisation sur fenêtre glissante, test sur la fenêtre suivante, réserve finale jamais touchée (🎛️ 12 derniers mois).
- Comparaison systématique au benchmark « buy & hold US500 avec la même marge » et à la stratégie sans couche fondamentale.

**Sortie :** une configuration dont l'espérance nette est > 0 hors échantillon, profit factor ≥ 1,3, drawdown simulé < 35 %, et ≥ 30 trades hors échantillon. Sinon retour en recherche, pas de passage en Phase 4.

## Phase 4 — Paper trading local en temps réel

- Même code qu'en réel, ordres envoyés au broker simulé, prix live Capital.com.
- Journal de décision complet (y compris les `NO_TRADE` et leurs raisons), tableau de bord.
- Comparaison avec les trades manuels d'Alexandre sur la même période.

**Sortie :** ≥ 8 semaines et ≥ 15 trades, résultats dans l'intervalle attendu par le backtest, zéro incident de sécurité.

## Phase 5 — Boucles d'auto-amélioration (encadrées)

- Agent « chercheur » : propose des variantes (paramètres 🎛️ dans leur plage, nouvelles features), les teste en walk-forward, rédige un rapport.
- Agent « critique » : vérifie le surapprentissage (nombre d'essais, déflation du Sharpe, stabilité par régime).
- Promotion d'une variante uniquement après paper trading et validation d'Alexandre. Registre versionné des modèles et paramètres.

**Sortie :** processus documenté, reproductible, avec retour arrière en une commande.

## Phase 6 — Réel à taille minimale

- ❓ Feu vert explicite d'Alexandre, clé API dédiée, 0,01 lot, kill switch testé.
- Tests préalables sur une position minimale : stop garanti posé et confirmé, déplacement du stop, prélèvement du financement.
- Montée en taille seulement selon les règles de `RISK_POLICY.md`.

**Sortie :** 3 mois de réel conformes au paper trading.

## Risques du projet

| Risque | Mitigation |
|---|---|
| Pas de consensus macro gratuit en historique | Surprise vs nowcast ou modèle ; archivage maison dès maintenant |
| Historique Capital.com court | Dukascopy calibré |
| Surapprentissage (beaucoup de paramètres, peu de trades) | Walk-forward, réserve finale, budget d'essais déclaré dans EXPERIMENTS |
| Capital de 20 € : 6 pertes max avant l'arrêt à 0,01 lot (3 si le risque atteint 20 %) | R8 puis R9, paliers R17, marge ≤ 50 % (R19) |
| Le journal ne prouve pas l'avantage fondamental | Mesurer l'apport du fondamental en A/B (EXP-01) |
