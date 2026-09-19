"""
Le fichier bulk Scryfall : métadonnées, téléchargement, idempotence.

Séparé du pipeline parce que ces trois gestes sont les seuls à parler au réseau.
Les isoler permet de tester le reste sans sortir de la machine.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from tqdm import tqdm

from mtgdb.db.models.import_run import ImportRun
from mtgdb.db.models.mtg_set import MtgSet

log = logging.getLogger("mtgdb.scryfall.bulk")

BULK_DATA_URL = "https://api.scryfall.com/bulk-data"
SETS_URL = "https://api.scryfall.com/sets"
HTTP_HEADERS = {"User-Agent": "MTG-DB/1.0 (educational project)"}


# ══════════════════════════════════════════════════════════════════════════════
# 1. TÉLÉCHARGEMENT
# ══════════════════════════════════════════════════════════════════════════════

def fetch_bulk_metadata(client: httpx.Client) -> tuple[str, str, datetime]:
    """
    URI, nom de fichier et date de publication du bulk `all_cards`.

    Scryfall a remplacé `download_uri` (tableau JSON de 2,4 Go) par
    `jsonl_download_uri` (JSONL gzippé, ~374 Mo). L'ancien champ a purement
    disparu de la réponse : le lire produisait un KeyError, ce qui a bloqué
    l'import du 28/07 au 25/08/2026 sans que rien ne le signale.

    On ne retombe volontairement PAS sur `download_uri` : ce champ n'existe plus,
    et un repli silencieux masquerait un nouveau changement d'API. Mieux vaut
    échouer bruyamment avec un message qui nomme le champ manquant.
    """
    resp = client.get(BULK_DATA_URL)
    resp.raise_for_status()
    for item in resp.json().get("data", []):
        if item.get("type") == "all_cards":
            updated_at = datetime.fromisoformat(item["updated_at"].replace("Z", "+00:00"))
            uri = item.get("jsonl_download_uri")
            if not uri:
                raise ValueError(
                    "Le bulk 'all_cards' n'expose pas 'jsonl_download_uri' — l'API "
                    f"Scryfall a probablement encore changé. Champs reçus : "
                    f"{sorted(item.keys())}"
                )
            filename = uri.rsplit("/", 1)[-1]
            return uri, filename, updated_at
    raise ValueError("Type 'all_cards' introuvable dans l'API bulk-data Scryfall.")


def bulk_already_imported(session: Session, download_uri: str) -> bool:
    """
    True si ce bulk exact a déjà été importé avec succès.

    Scryfall ne publie qu'un bulk par jour et son nom porte un horodatage unique :
    comparer source_file suffit à savoir si l'import a déjà été fait. Permet aux
    runs planifiés répétés de ne pas re-parser 2,4 Go pour rien.
    """
    stmt = (
        select(ImportRun.id)
        .where(
            ImportRun.source == "scryfall",
            ImportRun.status == "success",
            ImportRun.source_file == download_uri,
        )
        .limit(1)
    )
    return session.execute(stmt).first() is not None


def download_bulk_file(client: httpx.Client, url: str, dest: Path) -> None:
    """
    Télécharge le bulk, en ne publiant `dest` que si le transfert est complet.

    Le fichier était écrit directement sous son nom définitif. Une coupure en
    cours de route laissait donc un `.jsonl.gz` tronqué que le run suivant
    considérait comme valide — `main()` se contente de `dest.exists()` pour
    décider de ne pas retélécharger. `gzip` finissait par lever, mais des minutes
    plus tard et sur un message qui ne désignait pas la vraie cause.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    partiel = dest.with_suffix(dest.suffix + ".part")
    with client.stream("GET", url, follow_redirects=True) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0)) or None
        recus = 0
        try:
            with (
                open(partiel, "wb") as f,
                tqdm(total=total, unit="B", unit_scale=True, desc=dest.name) as bar,
            ):
                for chunk in resp.iter_bytes(chunk_size=65_536):
                    f.write(chunk)
                    recus += len(chunk)
                    bar.update(len(chunk))
        except BaseException:
            # BaseException : un Ctrl-C ou un SIGTERM doit lui aussi emporter le
            # fichier partiel, sinon il survit au run qu'il a fait échouer.
            partiel.unlink(missing_ok=True)
            raise

    if total and recus != total:
        partiel.unlink(missing_ok=True)
        raise OSError(
            f"Téléchargement incomplet : {recus:,} octets reçus sur {total:,} annoncés.")

    partiel.replace(dest)


def import_sets(client: httpx.Client, session: Session) -> int:
    resp = client.get(SETS_URL)
    resp.raise_for_status()
    sets_data = resp.json().get("data", [])
    rows: list[dict] = []
    for s in sets_data:
        released = None
        if raw_date := s.get("released_at"):
            try:
                released = date.fromisoformat(raw_date)
            except ValueError:
                pass
        rows.append({
            "code": s["code"],
            "name": s["name"],
            "set_type": s.get("set_type"),
            "released_at": released,
            "block": s.get("block"),
            "parent_set_code": s.get("parent_set_code"),
            "card_count": s.get("card_count"),
            "icon_svg_uri": s.get("icon_svg_uri"),
        })
    if not rows:
        return 0
    stmt = pg_insert(MtgSet).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["code"],
        set_={
            "name": stmt.excluded.name,
            "set_type": stmt.excluded.set_type,
            "released_at": stmt.excluded.released_at,
            "block": stmt.excluded.block,
            "parent_set_code": stmt.excluded.parent_set_code,
            "card_count": stmt.excluded.card_count,
            "icon_svg_uri": stmt.excluded.icon_svg_uri,
        },
    )
    session.execute(stmt)
    session.commit()
    return len(rows)


# ══════════════════════════════════════════════════════════════════════════════
# 4. UPSERTS
# ══════════════════════════════════════════════════════════════════════════════
