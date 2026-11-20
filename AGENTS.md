# Règles pour les agents

Ce fichier s'applique à tout agent (humain ou IA) qui modifie ce dépôt.

## Interdits absolus

1. **Aucun ordre réel.** Ne jamais ajouter de code capable d'appeler
   `/api/v1/positions`, `/api/v1/workingorders`, `/api/v1/confirms`,
   `PUT /api/v1/session` ou `/api/v1/accounts*` de Capital.com. Ne jamais élargir
   `ALLOWED_ENDPOINTS` ni retirer un élément de `FORBIDDEN_FRAGMENTS` sans
   décision humaine écrite.
2. **Aucun mode « live »** dans `TradingMode`. Le passage au réel fera l'objet
   d'un module séparé, revu par un humain, hors de ce socle.
3. **Aucun secret** dans le code, les tests, les journaux ou les commits.
   Les identifiants viennent de l'environnement (`.env` ignoré par git).
4. **Ne jamais contourner le Risk Engine.** Tout ordre passe par
   `RiskEngine.check_order`. Un agent ne peut pas modifier les limites de
   `RiskLimits` pour faire passer un trade.
5. **Aucun look-ahead.** Toute nouvelle feature doit porter un indice ou un
   horodatage de disponibilité et être couverte par un test de préfixe.
6. **Ne pas transformer la stratégie en stratégie purement technique.** La
   direction vient de l'analyse fondamentale ; la technique optimise l'entrée.

## Façon de travailler

- Une modification = un test. `python -m pytest` doit rester vert, sans réseau.
- Les seuils techniques (taille FVG/ATR, déplacement OB/ATR, mitigation d'entrée)
  sont des paramètres : les optimiser par backtest, puis valider hors échantillon,
  et consigner chaque essai dans `EXPERIMENTS.md`.
- Les données sous licence (ICE, SSGA, Yahoo…) et le journal de trading personnel
  ne sont jamais committés (`data/`, `TJ_*.xlsx` sont ignorés).
- Ne citer un endpoint ou un champ d'API qu'après lecture de la documentation
  officielle ; indiquer ce qui reste à vérifier.
- Le Risk Engine reste déterministe : pas d'aléa, pas d'I/O, pas de modèle.

## Rôle des futurs agents

| Agent | Peut | Ne peut pas |
|---|---|---|
| Analyste macro / Fed | Produire un biais (haussier, neutre, risque) daté et sourcé | Émettre un ordre |
| Analyste technique | Proposer zone d'entrée, stop, invalidation | Ignorer le biais fondamental |
| Décideur | Construire un `OrderRequest` ou `NO_TRADE` | Modifier les limites de risque |
| Risk Engine | Approuver ou opposer un veto | Être désactivé par un autre agent |
| Agent d'amélioration | Proposer des paramètres et lancer des backtests | Déployer sans validation hors échantillon et revue humaine |
