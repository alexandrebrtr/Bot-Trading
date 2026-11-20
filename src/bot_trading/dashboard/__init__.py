"""Dashboard de suivi en lecture seule (Streamlit).

Il lit l'état d'exécution (``runtime``), les décisions publiées
(``decision_feed``) et le store Parquet (``market``). Il ne peut envoyer aucun
ordre ; le seul état qu'il modifie est le kill switch, via ``risk.kill_switch``.
"""
