"""
Import du catalogue Scryfall.

Le pipeline vivait dans `scripts/import_scryfall.py`, hors du wheel : les tests
devaient charger 1 000 lignes par chemin de fichier pour atteindre une fonction,
et aucun consommateur ne pouvait réutiliser quoi que ce soit. Il est ici
désormais, découpé selon ce que chaque partie touche :

    parsers.py    le JSON, et rien d'autre — fonctions pures
    upserts.py    PostgreSQL, une Session en paramètre
    bulk.py       le réseau : métadonnées, téléchargement, idempotence
    pipeline.py   l'enchaînement : lots, cache, comptage

`scripts/import_scryfall.py` n'est plus qu'un point d'entrée en ligne de commande.
"""
