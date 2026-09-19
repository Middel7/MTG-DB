"""
Détection de l'environnement d'exécution.

Un même code tourne sur le poste de développement et dans un conteneur (Cron Job
Render). Deux comportements doivent en dépendre :

  - la journalisation : sur un disque éphémère, écrire logs/update_<date>.log
    revient à écrire dans le vide — le conteneur meurt avec son disque. Render
    capture stdout, c'est donc la seule sortie qui subsiste.
  - le garde-fou sur DATABASE_URL : une base localhost vue depuis un conteneur
    Render ne peut pas être la bonne.
"""
from __future__ import annotations

import os
from pathlib import Path

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def env_flag(name: str, default: bool = False) -> bool:
    """Lit une variable d'environnement booléenne (1/true/yes/on)."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUTHY


def in_container() -> bool:
    """
    True si le processus tourne dans un conteneur.

    Trois signaux, du plus explicite au plus incident :

      MTGDB_CONTAINER  posé par notre propre Dockerfile — c'est le signal
                       autoritaire, et le seul qui permette de simuler le mode
                       conteneur depuis le poste pour reproduire un incident ;
      RENDER           posé par Render sur tous ses services ;
      /.dockerenv      créé par le moteur Docker, filet pour une image tierce
                       qui n'aurait ni l'une ni l'autre.
    """
    if os.getenv("MTGDB_CONTAINER") is not None:
        return env_flag("MTGDB_CONTAINER")
    if env_flag("RENDER"):
        return True
    return Path("/.dockerenv").exists()
