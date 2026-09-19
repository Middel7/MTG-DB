"""
Traitement de DATABASE_URL : normalisation et garde-fous.

Ces règles vivaient jusqu'ici dans `update-prod.ps1`, donc sur le poste Windows
uniquement. Depuis que les runs de production partent d'un Cron Job Render, plus
aucun script PowerShell n'est sur le chemin : elles doivent être appliquées par
le code Python lui-même, sans quoi le premier run en conteneur échoue.

Aucune fonction de ce module ne lit l'environnement ni ne quitte le processus :
elles sont pures, donc testables sans base ni variables d'environnement.
"""
from __future__ import annotations

import re

# Render affiche encore l'URL de ses bases avec le préfixe historique postgres://,
# que SQLAlchemy 2 refuse ("Can't load plugin: sqlalchemy.dialects:postgres").
# La substitution est purement syntaxique : même dialecte, même pilote.
_LEGACY_PREFIX = "postgres://"
_MODERN_PREFIX = "postgresql://"

# Hôtes qui désignent la machine courante. ::1 apparaît sous ses deux écritures
# usuelles dans une URL : [::1] (forme canonique) et ::1 (forme tolérée).
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"})

_HOST_RE = re.compile(
    r"^[^:]+://"          # schéma
    r"(?:[^@/]*@)?"       # identifiants, optionnels
    r"(\[[^\]]+\]|[^:/?#]*)"  # hôte : [ipv6] ou nom/ipv4
)


def normalize_database_url(url: str | None) -> str | None:
    """
    Remplace le préfixe postgres:// par postgresql://. Tout le reste est inchangé.

    Retourne `url` tel quel (y compris None ou "") si le préfixe n'est pas concerné :
    cette fonction ne valide pas l'URL, elle ne corrige que ce seul préfixe.
    """
    if url and url.startswith(_LEGACY_PREFIX):
        return _MODERN_PREFIX + url[len(_LEGACY_PREFIX):]
    return url


def database_host(url: str | None) -> str:
    """Extrait l'hôte d'une URL de connexion, ou "" si elle est illisible."""
    if not url:
        return ""
    match = _HOST_RE.match(url)
    return match.group(1) if match else ""


def is_local_database_url(url: str | None) -> bool:
    """
    True si l'URL désigne une base sur la machine courante.

    Sert de garde-fou : un run de production qui tombe sur localhost ne produit
    aucune erreur visible — il met à jour la mauvaise base et rend un rapport
    final tout vert. C'est précisément le scénario contre lequel
    `update-prod.ps1` protégeait.
    """
    return database_host(url).lower() in _LOCAL_HOSTS


def redact_database_url(url: str | None) -> str:
    """
    Masque le mot de passe pour l'affichage dans les journaux.

    Les journaux d'un Cron Job Render sont consultables dans le dashboard : une
    URL de connexion complète ne doit jamais y apparaître.
    """
    if not url:
        return "(absent)"
    return re.sub(r"(://[^:/@]+:)[^@]*(@)", r"\1***\2", url)
