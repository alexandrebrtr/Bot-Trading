# RISK_POLICY.md — Politique de risque

> Version 0.1 (Phase 0), 20/11/2026. Le Risk Engine applique ce document et a un **droit de veto** sur toute décision de la stratégie ou d'un agent. Aucune boucle d'auto-amélioration ne peut modifier les règles ✅ ou 🏦.
> Légende : ✅ confirmé · 🧪 hypothèse · 🎛️ optimisable (dans la plage indiquée, jamais au-delà) · 🏦 broker/réglementation · ❓ à valider.
> Chiffres détaillés : `docs/CAPITAL_COM.md` §4.

## 1. Paramètres du compte

| # | Règle | Valeur | Statut |
|---|---|---|---|
| R1 | Broker, instrument | Capital.com, CFD `US500` | ✅ |
| R2 | Capital initial, devise | 20 €, EUR | ✅ |
| R3 | Levier max et par défaut | 20:1 (marge 5 %) | ✅ / 🏦 |
| R4 | Taille minimale et pas de taille | 0,01 lot, lus dans `dealingRules` au démarrage | 🏦 |
| R5 | Exposition maximale cumulée | 1 lot (non atteignable sous ~340 € de capital) | ✅ |
| R6 | Direction | Long uniquement ; un ordre SELL d'ouverture est rejeté | ✅ |

## 2. Règles par position

| # | Règle | Valeur | Statut |
|---|---|---|---|
| R7 | Stop loss garanti obligatoire, posé **dans le même ordre** que l'ouverture (`guaranteedStop=true`) | — | ✅ / 🏦 (obligation française) |
| R8 | Perte max par position (stop + spread + prime GSL + conversion) | ≤ 50 % de la marge engagée ⇒ stop ≤ 2,5 % du prix à 20:1 | ✅ |
| R9 | Perte max par position en % du capital courant | 🎛️ 2 % à 8 %, défaut **5 %** | ❓ (voir Q-R1) |
| R10 | Distance de stop ≥ `minGuaranteedStopDistance` ; si l'invalidation technique est plus proche, le stop est placé au minimum autorisé et le risque recalculé | — | 🏦 |
| R11 | Si R8, R9 et la taille minimale sont incompatibles (stop trop loin) : `NO_TRADE`, jamais réduction du stop sous l'invalidation | — | ✅ |
| R12 | Take profit facultatif | — | ✅ |
| R13 | Le stop ne se déplace que dans le sens favorable | — | ✅ |
| R14 | Spread au moment de l'entrée ≤ s × spread médian 20 jours | 🎛️ s ∈ [1,5 ; 3] | 🧪 |
| R15 | Taille = plancher((risque autorisé) / (distance × valeur du point), pas de taille), bornée par R5 et la marge disponible | — | ✅ |

## 3. Règles de compte

| # | Règle | Valeur | Statut |
|---|---|---|---|
| R16 | Drawdown max du compte (depuis le plus haut d'equity) | 50 % ⇒ **arrêt total** (kill switch), redémarrage manuel uniquement | ✅ |
| R17 | Paliers de réduction avant l'arrêt | DD ≥ 20 % : risque/trade divisé par 2 ; DD ≥ 35 % : seuls les setups de score max | 🧪 (paliers 🎛️) |
| R18 | Positions simultanées | 🎛️ 1 à 3, défaut 1 tant que le capital < 100 € | 🧪 |
| R19 | Marge utilisée totale | ≤ 50 % de l'equity (éviter la liquidation automatique du broker) | 🧪 |
| R20 | Pertes consécutives | Après 3 pertes : pause de 🎛️ 1 à 5 jours ouvrés | 🧪 |
| R21 | Événements majeurs (CPI, NFP, PCE, FOMC) | Pas de nouvelle entrée dans la fenêtre définie en STRATEGY F11 | 🧪 |
| R22 | Financement | Coût overnight cumulé estimé ≤ 🎛️ 25 % du risque initial, sinon sortie temporelle | 🧪 |

## 4. Règles de sécurité logicielle

| # | Règle | Statut |
|---|---|---|
| R23 | **Aucun ordre réel pendant le développement ou les tests.** Le client de données n'expose que des `GET` et le WebSocket ; toute autre méthode HTTP lève une exception (test en CI). | ✅ |
| R24 | Le module d'exécution réelle est séparé, désactivé par défaut, et exige un drapeau explicite + une confirmation d'Alexandre pour passer en réel. | ✅ |
| R25 | Aucun identifiant dans le dépôt : variables d'environnement ou `.env` ignoré par git ; clé API dédiée au bot, révocable. | ✅ |
| R26 | Après chaque ordre : vérification par `GET /confirms/{dealReference}` puis `GET /positions` ; si le stop garanti n'est pas confirmé, fermeture immédiate. | ✅ / 🏦 |
| R27 | Rapprochement périodique entre l'état local et `GET /positions` ; toute divergence fige le bot et alerte. | ✅ |
| R28 | Perte de flux de prix > 🎛️ 2 à 10 min ou session invalide : aucune nouvelle entrée (les stops garantis protègent les positions ouvertes). | ✅ |
| R29 | Un agent IA ou un LLM ne peut ni passer d'ordre, ni modifier ce document, ni changer un paramètre hors de sa plage 🎛️. Il propose ; le Risk Engine dispose. | ✅ |
| R30 | Les règles de la Phase 6 (passage en réel) exigent la validation hors échantillon et le paper trading décrits dans `ROADMAP.md`. | ✅ |

## 5. Contraintes du broker à revérifier au démarrage

Lues dans `GET /api/v1/markets/US500` ; si l'une manque, le bot refuse de trader :
`minDealSize`, `minSizeIncrement`, `minGuaranteedStopDistance`, `guaranteedStopAllowed`, `marginFactor`, `overnightFee`, `openingHours`, ainsi que le spread courant.

À tester une fois en réel, avant la Phase 6, avec l'accord d'Alexandre : déplacement d'un stop garanti, effet du GSL sur la marge, heure et jour de prélèvement du financement des indices.

## 6. Décisions à valider

| # | Question | Proposition |
|---|---|---|
| Q-R1 | Risque max par trade en % du capital (R9). La taille minimale rend 8 % possible au stop maximal. | 5 % du capital (stop ≤ ~115 points au début) |
| Q-R2 | « Perte max 1,70 € » ou « 50 % de la marge » ? Les deux diffèrent de 5 %. | Garder la règle des 50 % (1,62 € à 7 520 points), recalculée à chaque trade |
| Q-R3 | Le drawdown de 50 % se mesure-t-il depuis le capital initial (20 €) ou depuis le plus haut d'equity ? | Depuis le plus haut (plus prudent) |
| Q-R4 | Après l'arrêt à −50 %, redémarrage avec un nouveau dépôt autorisé ? | Non sans revue complète |
