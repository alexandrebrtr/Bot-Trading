"""Connecteurs de sources gratuites. Chaque module expose des fonctions de
parsing pures (testables sans réseau) et une classe de collecte qui renvoie
``(records, Provenance)``, prêts pour ``ParquetStore.write``."""
