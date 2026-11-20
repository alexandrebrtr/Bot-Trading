"""Bot de trading swing Long Only sur le CFD US500 de Capital.com.

Le paquet ne contient aucun chemin capable d'envoyer un ordre réel :
- l'exécution passe uniquement par le broker simulé local (``broker.simulated``) ;
- l'accès à Capital.com passe par ``broker.capital_readonly``, qui bloque au niveau
  du transport HTTP toute requête hors d'une liste blanche de lecture.
"""

__version__ = "0.1.0"
