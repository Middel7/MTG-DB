"""
Gestion des fichiers bruts téléchargés dans data/raw/.

Les imports téléchargent des fichiers volumineux (bulk Scryfall : ~2,6 Go ;
exports Cardmarket : ~46 Mo par run) qui saturent le disque s'ils s'accumulent.
Ce module centralise leur purge, utilisée par les deux pipelines.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("mtgdb.rawfiles")


def purge_old_files(
    directory: Path,
    keep: int = 1,
    current: str | None = None,
    logger: logging.Logger | None = None,
) -> int:
    """
    Supprime les fichiers .json* les plus anciens de `directory`, retourne les octets libérés.

    Le motif couvre `.json` (exports Cardmarket) comme `.jsonl.gz` (bulk Scryfall,
    depuis le passage de Scryfall au JSONL compressé) : sans cela, les bulks ne
    seraient plus jamais purgés et s'accumuleraient à 374 Mo pièce.

    `keep` est le nombre TOTAL de fichiers conservés, `current` inclus.

    `current` est réservé dans ce budget **même s'il n'existe pas encore sur le disque**.
    C'est ce qui permet d'appeler cette fonction *avant* un téléchargement : avec keep=1,
    tous les anciens fichiers partent et le nouveau arrive sur un répertoire vide, au lieu
    de cohabiter avec son prédécesseur. Sur le bulk Scryfall, cela fait la différence entre
    un pic disque de 5,1 Go et de 2,6 Go.

    Le fichier `current` n'est jamais supprimé : un téléchargement partiel interrompu reste
    donc réutilisable au run suivant.
    """
    out = logger or log
    if not directory.exists():
        return 0

    files = sorted(directory.glob("*.json*"), key=lambda p: p.stat().st_mtime, reverse=True)

    keepers: set[Path] = set()
    if current:
        keepers.add(directory / current)
    for path in files:
        if len(keepers) >= max(keep, 1):
            break
        keepers.add(path)

    freed = 0
    for path in files:
        if path in keepers:
            continue
        try:
            size = path.stat().st_size
            path.unlink()
            freed += size
            out.info(f"  Purge : {path.name} ({size / 1_048_576:.0f} Mo)")
        except OSError as exc:
            out.warning(f"  Purge impossible pour {path.name} : {exc}")

    if freed >= 1_073_741_824:
        out.info(f"  Espace libéré : {freed / 1_073_741_824:.2f} Go")
    elif freed:
        out.info(f"  Espace libéré : {freed / 1_048_576:.0f} Mo")
    return freed
