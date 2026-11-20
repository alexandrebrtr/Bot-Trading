# Analyse du journal de trading 2026

> Source : `TJ_2026.xlsx`, onglet « Trades », lu le 20/11/2026 (262 lignes, du 19/01 au 20/11/2026).
> Script reproductible : `python research/journal_analysis.py <chemin du fichier>`.
> Le journal est une donnée personnelle : il n'est pas commité dans le dépôt.

## 1. Segmentation

Les lignes sans taille ni prix (dividendes, dépôts, retraits, 13 lignes) sont exclues. Il reste 257 trades.

| Segment | Contenu | Trades | Période |
|---|---|---:|---|
| **A. Capital.com US500** | compte Live, symbole `SP500` | **13** | 03/08 → 10/11 |
| B. Live, autres actifs | UK100 (surtout), US100, Solana | 37 | 19/01 → 04/08 |
| C1. Prop firms, US500 | futures ES / MES (FTMO, FTUK, The5ers, Lucid), réel et évaluation | 125 | 04/08 → 20/11 |
| C2. Prop firms, autres | UK100, US100, MNQ | 74 | 13/05 → 06/10 |
| D. Paper et Demo | UK100, US100 | 8 | 11/02 → 12/05 |

Tous les trades `Live` sont chez Capital.com (confirmé par Alexandre). Le segment A ne garde que l'US500 ; les UK100, US100 et Solana du compte Live (segment B), y compris ceux d'avant le dépôt de 300 € du 20/07, servent aussi à calibrer le simulateur (valeur du point, frais, dividendes).

## 2. Statistiques par segment

Montants en devise du compte (le journal indique « $ », mais pour le segment A le P&L vaut 0,85 à 0,89 × les points × la taille, ce qui correspond à des euros sur un instrument coté en dollars).

| | A. Capital US500 | B. Live autres | C1. Prop US500 | C2. Prop autres | D. Paper/Demo |
|---|---:|---:|---:|---:|---:|
| Trades | 13 | 37 | 125 | 74 | 8 |
| Part de LONG | 77 % | 59 % | **41 %** | 76 % | 50 % |
| Win rate | **100 %** | 92 % | 92 % | 95 % | 75 % |
| P&L net | +35,46 | +19,77 | +2 595,50 | +329,82 | +164,35 |
| Gain moyen | 2,73 | 0,65 | 85,17 | 12,08 | 27,76 |
| Perte moyenne | aucune | −0,81 | **−719,90** | −128,99 | −1,10 |
| Pire perte | aucune | −2,31 | **−1 792,50** | −500,00 | −1,13 |
| Profit factor | ∞ | 9,2 | **1,36** | 1,64 | 76 |
| Gain médian (points) | 3,0 | 3,5 | 2,6 | 6,2 | 5,3 |
| Stop renseigné | 100 % | 70 % | **1 %** | 1 % | 88 % |
| Stop médian (% du prix) | **7,6 %** | 2,6 % | n.s. | n.s. | 0,1 % |
| Take profit médian (points) | 15,4 | 21,7 | 9,8 | 18,4 | 25,6 |
| Exécution en M5 | 100 % | 51 % | 69 % | 86 % | 88 % |

Sur tout l'US500 (137 trades, tous comptes) : win rate 94 %, gain moyen **+6,1 points** (médiane +3,0), perte moyenne **−37,2 points**, espérance +3,6 points par trade, tirée vers le haut par un trade de +206 points dont les prix semblent mal saisis (sans lui, environ +2,1 points). Durée médiane d'un trade : **16 minutes** (90 % sous 2 h 20). Un seul trade US500 a été gardé plus d'une journée.

Sur les prop firms US500, les 5 pires pertes (−6 536) effacent plus que les 5 meilleurs gains (+5 041). Sur les comptes prop firm réels (financés), le P&L US500 est de **−35 sur 62 trades** ; le résultat positif vient des comptes d'évaluation.

Toutes les pertes lourdes ont été prises **sans stop loss** et souvent avec une taille plus grande que d'habitude (5 MES). La plupart portent la mention « Respect Plan : OUI ».

## 3. Ce que le journal montre réellement

1. **Le style exécuté n'est pas du swing.** C'est du scalping intraday en M5 : take profit fixe d'environ 15 points, sortie en moins d'une heure, win rate très élevé.
2. **Le profil de gain est asymétrique dans le mauvais sens** (« ramasser des centimes devant un rouleau compresseur ») : beaucoup de petits gains, quelques pertes très lourdes. Le R:R prévu médian sur Capital.com est d'environ **0,03** (15 points visés pour 460 à 1 100 points de risque).
3. **Les stops Capital.com sont des stops catastrophe** (6 à 15 % sous l'entrée). Ils sont incompatibles avec la règle « perte maximale = 50 % de la marge engagée », qui impose un stop à environ **2,5 % du prix** avec un levier de 20:1 (voir `RISK_POLICY.md`).
4. **Long only non respecté** : 3 shorts sur 13 trades Capital.com, et 59 % de shorts sur les futures en prop firm.
5. **Le fondamental n'est pas tracé.** Le journal ne contient aucun champ macro (publication, surprise, biais Fed). Il est donc impossible de vérifier, à partir du journal, que l'analyse fondamentale améliore les résultats.
6. **Les meilleurs créneaux observés** (US500, tous comptes, heures de saisie) se situent entre 10 h et 13 h et entre 15 h et 16 h. Les pertes lourdes se concentrent à 8-9 h et 14 h et 17 h. L'échantillon est trop petit pour en tirer une règle : c'est une hypothèse à tester.
7. **Les notes répètent les mêmes erreurs** : sorties trop tôt par manque de confiance, trades pris pendant les cours ou un appel, clics erronés. Ce sont des problèmes d'exécution qu'un bot élimine.

## 4. Conséquences pour le bot

- Le bot **ne doit pas copier le profil statistique** du journal (petit TP, stop lointain ou absent). Avec un stop garanti obligatoire à environ 2,5 % et un capital de 20 €, ce profil ferait perdre environ 1,6 € à chaque échec pour gagner environ 0,13 € à chaque réussite : il faudrait plus de 92 % de réussite juste pour couvrir les frais.
- Le bot reprend ce qui relève de **l'intention** : biais haussier sur l'US500, entrée sur retracement en M5 après une analyse multi-timeframe, et patience pour garder un trade en swing quand l'entrée est mal placée (trade du 05/10).
- Les 13 trades Capital.com servent à **calibrer le simulateur** (spread, valeur du point en euros, frais overnight observés : 1,75 sur le trade du 05/10), pas à valider la stratégie.
- Il faut ajouter au journal les champs manquants (biais fondamental, publication du jour, timeframe du setup, zone FVG/OB visée) pour pouvoir comparer à l'avenir le bot et les trades manuels.

## 5. Limites

- 13 trades Capital.com, tous gagnants : aucune conclusion statistique n'est possible sur ce segment.
- Plusieurs champs calculés du fichier sont incohérents (R:R réel, `#DIV/0!`, un prix de sortie à 975,20 pour un UK100 à 10 972). Les statistiques ci-dessus sont recalculées à partir des prix, sans utiliser ces champs.
- Les heures sont celles saisies dans le journal, sans fuseau précisé (heure de Paris supposée).
