# Sécurité

## Contexte

Capital.com ne fournit à ce projet qu'un **compte réel** : il n'y a pas de compte
démo. Une clé API de ce compte permet techniquement d'ouvrir des positions. Le
dépôt est donc conçu pour qu'**aucun ordre réel ne puisse partir**, même en cas
d'erreur de programmation ou d'agent mal orienté.

## Garde-fous en place

| Couche | Mécanisme | Test |
|---|---|---|
| Configuration | `TradingMode` ne contient que `backtest` et `paper` ; `BOT_MODE=live` lève une erreur. | `tests/test_safety.py` |
| Client | `CapitalReadOnlyClient` n'a aucune méthode d'ordre, de position, de confirmation ou de changement de compte. | `test_client_has_no_trading_methods` |
| Transport | `ReadOnlyGuardAdapter` est le **seul** adaptateur monté sur la session HTTP. Il vérifie méthode + chemin exact contre une liste blanche **avant** tout envoi, refuse les fragments `positions`, `workingorders`, `confirms`, `accounts`, `history`, `watchlists`, `topup`, impose HTTPS et les hôtes Capital.com connus, et désactive les redirections. | `test_direct_http_order_never_reaches_network`, `test_forbidden_requests` |
| Code source | Seul `capital_readonly.py` connaît les hôtes Capital.com ; aucun fichier ne contient d'appel HTTP vers un endpoint d'ordre. | `test_only_readonly_module_knows_capital_hosts`, `test_no_order_endpoint_called_in_source` |
| Flux temps réel | `CapitalStream` n'envoie que `marketData.*`, `OHLCMarketData.*` et `ping` (`ALLOWED_STREAM_DESTINATIONS`), sur la seule URL `STREAMING_URL`. | `test_stream_refuses_other_destinations` |
| Sources publiques | `PublicSourceClient` : `GET` seul, HTTPS, hôtes déclarés par source ; la clé FRED n'apparaît ni dans les URL stockées, ni dans la traçabilité, ni dans les erreurs. | `tests/test_data_sources.py`, `test_secrets_never_stored` |
| Contenu externe | Titres RSS, noms de dépôts SEC : nettoyés et marqués `untrusted_external`. Ce sont des données, jamais des instructions pour les agents. | `test_fed_rss_parse_marks_untrusted` |
| Exécution | Le seul broker est `SimulatedBroker`, sans dépendance réseau, et il passe chaque ordre par le Risk Engine. | `tests/test_simulated_broker.py` |
| Docker | Le service `tests` tourne avec `network_mode: none` et sans `.env`. | `docker-compose.yml` |

## Liste blanche Capital.com

`GET /api/v1/time`, `GET /api/v1/ping`, `GET /api/v1/session/encryptionKey`,
`GET|POST|DELETE /api/v1/session`, `GET /api/v1/markets`,
`GET /api/v1/markets/{epic}`, `GET /api/v1/marketnavigation[/{nodeId}]`,
`GET /api/v1/prices/{epic}`.

Tout le reste est refusé. Modifier cette liste exige une revue humaine.

## Secrets

- Variables : `CAPITAL_API_KEY`, `CAPITAL_IDENTIFIER`, `CAPITAL_API_PASSWORD`,
  clés FRED/BLS/BEA, `SEC_USER_AGENT`. Modèle : `.env.example`.
- `.env` et `*.env` sont ignorés par git. `CapitalCredentials` masque ses valeurs
  dans `repr`.
- Recommandations : clé API dédiée au bot, mot de passe de clé propre,
  révocation immédiate en cas de doute (Settings > API integrations sur Capital.com).
- Les tests n'utilisent aucun identifiant ni accès réseau.

## Signaler un problème

Ouvrir une issue privée ou contacter le propriétaire du dépôt. Ne jamais coller
de clé, de jeton `CST` / `X-SECURITY-TOKEN` ou de relevé de compte dans une issue.
