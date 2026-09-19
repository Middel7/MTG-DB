#!/usr/bin/env python3
"""
Import des cartes Magic: The Gathering depuis Scryfall bulk data vers PostgreSQL.

Flux :
  1. GET https://api.scryfall.com/bulk-data  → jsonl_download_uri du fichier all_cards
  2. Purge des anciens bulks, puis téléchargement → data/raw/scryfall/<filename>
  3. GET https://api.scryfall.com/sets       → upsert dans mtg_sets (FK obligatoire)
  4. Parsing streaming JSONL gzippé → batches de 500 cartes :
       cards / card_faces / card_printings / card_prices
  5. Mise à jour import_runs (début, fin, compteurs, erreurs)

Usage :
  python scripts/import_scryfall.py
  python scripts/import_scryfall.py --force      # retélécharge même si fichier existant
  python scripts/import_scryfall.py --dry-run    # parse et compte sans toucher la base
"""
from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import sys
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql import func
from tqdm import tqdm

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mtgdb.db.engine import SessionLocal, check_connection, engine
from mtgdb.db.models.card import Card, normalize_card_name
from mtgdb.db.models.card_face import CardFace
from mtgdb.db.models.card_price import CardPrice
from mtgdb.db.models.card_printing import CardPrinting
from mtgdb.db.models.import_run import ImportRun
from mtgdb.db.models.mtg_set import MtgSet
from mtgdb.db.retry import retry_transient
from mtgdb.rawfiles import purge_old_files

BULK_DATA_URL = "https://api.scryfall.com/bulk-data"
SETS_URL = "https://api.scryfall.com/sets"
RAW_DIR = ROOT / "data" / "raw" / "scryfall"
BATCH_SIZE = 500
HTTP_HEADERS = {"User-Agent": "MTG-DB/1.0 (educational project)"}

# ──────────────────────────────────────────────────────────────────────────────
# SKIP_SCRYFALL_PRICES : ne pas alimenter scryfall_card_prices.
#
# À activer sur les bases où PERSONNE ne lit cette table — la production
# RELIC-Trade, typiquement. Elle y représente 61,6 Mo/jour, soit 46 % de la
# croissance, pour des lignes que rien ne consulte.
#
# À laisser DÉSACTIVÉE en local : ManaMind_AI lit bien cette table
# (routers/collection.py, join sur CardPrice pour un MIN(price) en euros).
# La couper en local casserait son affichage de prix.
#
# Pourquoi une variable d'environnement et non une détection d'URL : un
# basculement automatique sur la forme de DATABASE_URL est un piège. L'URL de
# l'hébergeur change, ou quelqu'un pointe le local vers le cloud pour un test,
# et le comportement bascule sans que personne ne l'ait demandé. Une variable
# explicite se lit dans la config et se cherche au grep.
#
# ⚠️ Ne supprime RIEN : les lignes déjà présentes restent. On cesse d'écrire,
# on ne nettoie pas.
SKIP_SCRYFALL_PRICES = os.getenv("SKIP_SCRYFALL_PRICES", "").strip().lower() in (
    "1", "true", "yes", "on"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("import_scryfall")


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


def _iter_bulk_cards(file_path: Path):
    """
    Itère les cartes du bulk, une par ligne.

    Le bulk est désormais du JSONL gzippé : un objet JSON complet par ligne,
    et non plus un unique tableau JSON. `ijson.items(f, "item")` ne sait pas le
    lire — mais on n'en a plus besoin, la lecture ligne par ligne est déjà
    naturellement en streaming, et plus rapide.

    `gzip.open` décompresse à la volée : le fichier n'est jamais développé sur
    disque (374 Mo compressés contre ~2,4 Go décompressés).
    """
    with gzip.open(file_path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip().rstrip(",")
            # Tolère d'éventuels crochets si Scryfall revenait à un tableau.
            if not line or line in ("[", "]"):
                continue
            yield json.loads(line)


def _safe_rollback(session: Session) -> None:
    """
    Remet la session en état, sans jamais lever.

    Un rollback ouvre lui-même une connexion quand la précédente est morte : sur
    une base indisponible il échoue à son tour. C'est ce qui a tué le run du
    19/09 — l'erreur de nettoyage, levée depuis un bloc `except`, a remplacé
    l'erreur d'origine et est remontée hors de tout rattrapage.

    `engine.dispose()` force le pool à jeter ses connexions : sans lui, la
    tentative suivante réutiliserait le même socket fermé et échouerait pour une
    raison qui n'a plus rien à voir avec l'état réel de la base.
    """
    try:
        session.rollback()
    except Exception as exc:  # noqa: BLE001
        log.debug(f"Rollback impossible (connexion morte ?) : {exc}")
    if engine is not None:
        try:
            engine.dispose()
        except Exception as exc:  # noqa: BLE001
            log.debug(f"Recyclage du pool impossible : {exc}")


def fail_orphan_runs(session: Session, source: str = "scryfall",
                     older_than_hours: int = 6) -> int:
    """
    Marque `failed` les runs restés `running` d'un processus qui n'existe plus.

    Un run tué brutalement (conteneur arrêté, base injoignable au moment
    d'écrire son statut) laisse une ligne `running` éternelle. Trois dégâts :
    la supervision croit un import en cours, `bulk_already_imported` ne voit
    jamais de succès pour ce bulk, et l'historique devient illisible.

    Le seuil de 6 h est une sécurité, pas la garantie principale : c'est le
    verrou advisory qui assure qu'aucun autre run ne tourne. Il couvre le cas
    d'un run lancé avec `--no-lock`, où cette garantie n'existe plus.
    """
    from sqlalchemy import text as sa_text

    result = session.execute(sa_text("""
        UPDATE import_runs
        SET status = 'failed',
            finished_at = now(),
            error_message = COALESCE(
                error_message,
                'Run orphelin : processus interrompu avant d''avoir pu écrire son statut. '
                'Marqué par un run ultérieur.')
        WHERE source = :source
          AND status = 'running'
          AND started_at < now() - make_interval(hours => :heures)
    """), {"source": source, "heures": older_than_hours})
    session.commit()
    return result.rowcount


def finalize_run(run_id: int, status: str, *, cards: int = 0, printings: int = 0,
                 errors: int = 0, error_message: str | None = None) -> bool:
    """
    Écrit le statut final d'un run dans une session NEUVE. Retourne False si
    même cela a échoué.

    Pourquoi ne pas réutiliser la session du run : quand le statut à écrire est
    `failed`, la cause la plus probable est une base injoignable. La session en
    cours est alors dans un état indéterminé, et un `rollback` y expire les
    objets ORM — les attributs qu'on vient d'affecter seraient rechargés depuis
    la base, donc perdus, et le commit n'écrirait rien. Un `UPDATE` explicite
    sur une connexion neuve ne dépend d'aucun état accumulé.
    """
    from sqlalchemy import text as sa_text

    def _ecrire() -> bool:
        with SessionLocal() as session_finale:
            session_finale.execute(sa_text("""
                UPDATE import_runs
                SET status = :status,
                    finished_at = now(),
                    cards_imported = :cards,
                    printings_imported = :printings,
                    errors_count = :errors,
                    error_message = :message
                WHERE id = :id
            """), {"status": status, "cards": cards, "printings": printings,
                   "errors": errors, "message": error_message, "id": run_id})
            session_finale.commit()
        return True

    try:
        return retry_transient(
            _ecrire,
            description=f"enregistrement du statut '{status}' du run #{run_id}",
            on_retry=lambda: engine.dispose() if engine is not None else None,
        )
    except Exception as exc:  # noqa: BLE001
        log.error(
            f"Statut '{status}' non enregistre pour le run #{run_id} ({exc}). "
            f"Il restera 'running' jusqu'a ce qu'un run ulterieur le marque orphelin."
        )
        return False


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


# ══════════════════════════════════════════════════════════════════════════════
# 2. IMPORT DES ÉDITIONS
# ══════════════════════════════════════════════════════════════════════════════

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
# 3. PARSEURS
# ══════════════════════════════════════════════════════════════════════════════

def _parse_card_row(raw: dict[str, Any]) -> dict[str, Any] | None:
    oracle_id = raw.get("oracle_id")
    if not oracle_id:
        return None
    name = raw.get("name", "")
    legalities = raw.get("legalities") or {}
    mana_cost = raw.get("mana_cost") or None
    if not mana_cost:
        faces = raw.get("card_faces") or []
        if faces:
            mana_cost = faces[0].get("mana_cost") or None
    return {
        "oracle_id": oracle_id,
        "name": name,
        "normalized_name": normalize_card_name(name),
        "mana_cost": mana_cost,
        "mana_value": raw.get("cmc"),
        "type_line": raw.get("type_line"),
        "oracle_text": raw.get("oracle_text"),
        "power": raw.get("power"),
        "toughness": raw.get("toughness"),
        "loyalty": raw.get("loyalty"),
        "defense": raw.get("defense"),
        "colors": raw.get("colors") or [],
        "color_identity": raw.get("color_identity") or [],
        "keywords": raw.get("keywords") or [],
        "legal_commander": legalities.get("commander") == "legal",
        "edhrec_rank": raw.get("edhrec_rank"),
    }


def _parse_face_rows(raw: dict[str, Any], card_id: int) -> list[dict[str, Any]]:
    faces_data = raw.get("card_faces") or []
    rows = []
    for face in faces_data:
        img = face.get("image_uris") or {}
        rows.append({
            "card_id": card_id,
            "face_name": face.get("name", ""),
            "mana_cost": face.get("mana_cost") or None,
            "type_line": face.get("type_line"),
            "oracle_text": face.get("oracle_text"),
            "power": face.get("power"),
            "toughness": face.get("toughness"),
            "loyalty": face.get("loyalty"),
            "defense": face.get("defense"),
            "colors": face.get("colors") or [],
            "image_small": img.get("small"),
            "image_normal": img.get("normal"),
            "image_large": img.get("large"),
        })
    return rows


def _extract_printed_name(raw: dict[str, Any]) -> str | None:
    printed = raw.get("printed_name")
    if not printed:
        faces = raw.get("card_faces") or []
        face_names = [f.get("printed_name") for f in faces if f.get("printed_name")]
        if face_names:
            printed = " // ".join(face_names)
    return printed or None


def _parse_printing_row(raw: dict[str, Any], card_id: int) -> dict[str, Any]:
    img = raw.get("image_uris") or {}
    if not img:
        faces = raw.get("card_faces") or []
        if faces:
            img = faces[0].get("image_uris") or {}
    released_at = None
    if raw_date := raw.get("released_at"):
        try:
            released_at = date.fromisoformat(raw_date)
        except ValueError:
            pass
    return {
        "scryfall_id": raw["id"],
        "oracle_id": raw.get("oracle_id", ""),
        "card_id": card_id,
        "set_code": raw.get("set"),
        "collector_number": raw.get("collector_number"),
        "lang": raw.get("lang"),
        "rarity": raw.get("rarity"),
        "released_at": released_at,
        "artist": raw.get("artist"),
        "border_color": raw.get("border_color"),
        "frame": raw.get("frame"),
        "full_art": bool(raw.get("full_art")),
        "promo": bool(raw.get("promo")),
        "reprint": bool(raw.get("reprint")),
        "digital": bool(raw.get("digital")),
        "image_small": img.get("small"),
        "image_normal": img.get("normal"),
        "image_large": img.get("large"),
        "scryfall_uri": raw.get("scryfall_uri"),
        "cardmarket_id": raw.get("cardmarket_id"),
        "tcgplayer_id": raw.get("tcgplayer_id"),
        "printed_name": _extract_printed_name(raw),
    }



def _parse_price_rows(prices: dict[str, Any], printing_id: int,
                      today: date) -> list[dict[str, Any]]:
    rows = []
    candidates = [
        ("eur", "regular", prices.get("eur")),
        ("eur", "foil",    prices.get("eur_foil")),
        ("usd", "regular", prices.get("usd")),
        ("usd", "foil",    prices.get("usd_foil")),
        ("tix", "regular", prices.get("tix")),
    ]
    for currency, price_type, price_str in candidates:
        if not price_str:
            continue
        try:
            rows.append({
                "printing_id": printing_id,
                "source": "scryfall",
                "currency": currency,
                "price_type": price_type,
                # Decimal, jamais float : la colonne est Numeric(10, 2) et il
                # s'agit de monnaie. L'arrondi absorbait l'imprécision du
                # flottant, mais le pipeline Cardmarket, lui, fait déjà
                # `Decimal(s)` — l'asymétrie n'avait aucune raison d'être sur le
                # seul sujet où elle ne se justifie jamais.
                "price": Decimal(str(price_str)),
                "date": today,
            })
        except (InvalidOperation, ValueError, TypeError):
            pass
    return rows


# ══════════════════════════════════════════════════════════════════════════════
# 4. UPSERTS
# ══════════════════════════════════════════════════════════════════════════════

# Colonnes de `scryfall_cards` qui décrivent la CARTE, jamais l'impression. Le
# bulk les répète à l'identique sur chacune des impressions d'une même carte :
# c'est pourquoi ne retenir que la première occurrence du run est sans perte.
_COLONNES_CARTE = (
    "name", "normalized_name", "mana_cost", "mana_value", "type_line",
    "oracle_text", "power", "toughness", "loyalty", "defense",
    "colors", "color_identity", "keywords", "legal_commander", "edhrec_rank",
)


def _upsert_cards(session: Session, rows: list[dict]) -> dict[str, int]:
    stmt = pg_insert(Card).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["oracle_id"],
        # N'écrire que ce qui change réellement. Sans ce prédicat, chaque upsert
        # produit un UPDATE — donc un tuple mort, du WAL, et une valeur de
        # séquence consommée — même quand la ligne est rigoureusement identique.
        where=or_(*[
            getattr(Card, colonne).is_distinct_from(getattr(stmt.excluded, colonne))
            for colonne in _COLONNES_CARTE
        ]),
        set_={
            "name":             stmt.excluded.name,
            "normalized_name":  stmt.excluded.normalized_name,
            "mana_cost":        stmt.excluded.mana_cost,
            "mana_value":       stmt.excluded.mana_value,
            "type_line":        stmt.excluded.type_line,
            "oracle_text":      stmt.excluded.oracle_text,
            "power":            stmt.excluded.power,
            "toughness":        stmt.excluded.toughness,
            "loyalty":          stmt.excluded.loyalty,
            "defense":          stmt.excluded.defense,
            "colors":           stmt.excluded.colors,
            "color_identity":   stmt.excluded.color_identity,
            "keywords":         stmt.excluded.keywords,
            "legal_commander":  stmt.excluded.legal_commander,
            "edhrec_rank":      stmt.excluded.edhrec_rank,
            "updated_at":       func.now(),
        },
    )
    session.execute(stmt)
    oracle_ids = [r["oracle_id"] for r in rows]
    result = session.execute(
        select(Card.id, Card.oracle_id).where(Card.oracle_id.in_(oracle_ids))
    )
    return {row.oracle_id: row.id for row in result}


def _replace_faces(session: Session, face_rows: list[dict], card_ids: list[int]) -> None:
    session.execute(delete(CardFace).where(CardFace.card_id.in_(card_ids)))
    if face_rows:
        session.execute(pg_insert(CardFace).values(face_rows))


# Colonnes que le bulk Scryfall ne renseigne que pour UNE PARTIE des impressions,
# et dont une valeur absente ne signifie donc pas « cette valeur a disparu ».
#
# cardmarket_id : Scryfall ne le fournit que sur l'impression anglaise. Écrasé
# tel quel, il repassait à NULL sur les 401 230 impressions non anglaises À
# CHAQUE RUN — que `propagate_cardmarket_ids()` repeuplait juste après, en
# recopiant la valeur depuis l'impression anglaise de la même carte. Soit 77 %
# de la table réécrite deux fois par run pour revenir au point de départ :
# 24 minutes sur la base de production, mesurées le 19/09.
#
# Conséquence assumée : un cardmarket_id ne peut plus être EFFACÉ par le bulk.
# Si Scryfall retire l'identifiant d'un produit délisté, l'ancienne valeur
# subsiste. C'était déjà largement le cas — la propagation la recopiait depuis
# une impression voisine — et le rapport de liaison Cardmarket surveille cet
# écart (« Sans correspondance CM », 10 lignes au 19/09).
PRESERVE_IF_NULL = frozenset({"cardmarket_id"})


def _upsert_printings(session: Session, rows: list[dict]) -> dict[str, int]:
    update_cols = [
        "oracle_id", "card_id", "set_code", "collector_number", "lang",
        "rarity", "released_at", "artist", "border_color", "frame",
        "full_art", "promo", "reprint", "digital",
        "image_small", "image_normal", "image_large", "scryfall_uri",
        "cardmarket_id", "tcgplayer_id", "printed_name",
    ]
    stmt = pg_insert(CardPrinting).values(rows)

    def valeur_cible(col: str):
        """Ce que la colonne vaudra après l'upsert."""
        if col in PRESERVE_IF_NULL:
            return func.coalesce(getattr(stmt.excluded, col), getattr(CardPrinting, col))
        return getattr(stmt.excluded, col)

    stmt = stmt.on_conflict_do_update(
        index_elements=["scryfall_id"],
        # Ne réécrire que les impressions réellement modifiées.
        #
        # Sans ce prédicat, les 520 000 lignes de la table étaient réécrites à
        # chaque run, que le bulk ait changé quelque chose ou non : 43 785 680
        # UPDATE cumulés pour 11 449 INSERT, mesurés dans pg_stat_user_tables.
        # C'est le « dernier gros gisement » que le CHANGELOG du 19/09 identifiait
        # sans le traiter.
        #
        # `IS DISTINCT FROM` et non `!=` : la table est pleine de NULL
        # (cardmarket_id, printed_name, tcgplayer_id…), et `NULL != NULL` vaut
        # NULL, donc faux — la moitié des colonnes ne serait jamais comparée.
        #
        # La comparaison porte sur la valeur CIBLE, coalesce comprise : sinon une
        # impression dont le bulk ne fournit pas le cardmarket_id serait vue comme
        # modifiée à chaque run, et on retomberait sur le problème d'origine.
        where=or_(*[
            getattr(CardPrinting, col).is_distinct_from(valeur_cible(col))
            for col in update_cols
        ]),
        set_={col: valeur_cible(col) for col in update_cols},
    )
    session.execute(stmt)
    scryfall_ids = [r["scryfall_id"] for r in rows]
    result = session.execute(
        select(CardPrinting.id, CardPrinting.scryfall_id)
        .where(CardPrinting.scryfall_id.in_(scryfall_ids))
    )
    return {row.scryfall_id: row.id for row in result}


def _insert_prices(session: Session, rows: list[dict]) -> None:
    if not rows:
        return
    stmt = pg_insert(CardPrice).values(rows)
    stmt = stmt.on_conflict_do_nothing()
    session.execute(stmt)


def propagate_cardmarket_ids(session: Session) -> int:
    """
    Propage cardmarket_id aux impressions qui n'en ont pas,
    en copiant depuis une autre impression de la même carte dans la même édition
    (même card_id + set_code + collector_number).
    """
    from sqlalchemy import text as sa_text
    result = session.execute(sa_text("""
        UPDATE scryfall_card_printings AS target
        SET cardmarket_id = source.cardmarket_id
        FROM scryfall_card_printings AS source
        WHERE target.cardmarket_id IS NULL
          AND source.cardmarket_id IS NOT NULL
          AND target.card_id = source.card_id
          AND target.set_code = source.set_code
          AND target.collector_number = source.collector_number
    """))
    session.commit()
    return result.rowcount


def propagate_tcgplayer_id_en(session: Session) -> int:
    """
    Renseigne tcgplayer_id_en sur toutes les impressions en copiant le tcgplayer_id
    de l'impression anglaise (lang='en') ayant le même set_code + collector_number.
    Les impressions sans équivalent anglais restent NULL.
    """
    from sqlalchemy import text as sa_text
    result = session.execute(sa_text("""
        UPDATE scryfall_card_printings AS target
        SET tcgplayer_id_en = source.tcgplayer_id
        FROM scryfall_card_printings AS source
        WHERE source.lang = 'en'
          AND source.tcgplayer_id IS NOT NULL
          AND target.set_code = source.set_code
          AND target.collector_number = source.collector_number
          AND (target.tcgplayer_id_en IS NULL
               OR target.tcgplayer_id_en != source.tcgplayer_id)
    """))
    session.commit()
    return result.rowcount


# ══════════════════════════════════════════════════════════════════════════════
# 5. TRAITEMENT PAR BATCH
# ══════════════════════════════════════════════════════════════════════════════

def _flush_batch(
    session: Session,
    card_rows: list[dict],
    raw_cards: list[dict[str, Any]],
    today: date,
    cache_oracle: dict[str, int] | None = None,
) -> tuple[int, int]:
    """
    Écrit un lot du bulk. Retourne (cartes upsertées, impressions upsertées).

    `cache_oracle` mémorise, pour tout le run, la correspondance
    `oracle_id → scryfall_cards.id`. Il rend la déduplication des cartes GLOBALE
    et non plus locale au lot : sans lui, un terrain de base présent dans 800 lots
    est upserté 800 fois. Mesuré sur la base locale : 27 073 993 UPDATE cumulés
    sur une table de 38 907 lignes, soit un facteur 13,4 de travail inutile à
    chaque run, sur l'instance PostgreSQL qui est déjà le goulot de la production.

    Omettre l'argument conserve l'ancien comportement, autonome et sans état —
    ce qui garde la fonction testable lot par lot.
    """
    # Deux déduplications de natures DIFFÉRENTES. Les confondre a coûté 4 % du
    # catalogue à chaque run.
    #
    # `card_rows` porte des CARTES (niveau oracle). `ON CONFLICT DO UPDATE` ne
    # peut pas affecter deux fois la même ligne dans un seul INSERT : il faut
    # donc un seul enregistrement par `oracle_id`.
    #
    # `raw_cards` porte des IMPRESSIONS, et le bulk en compte plusieurs par carte
    # — une par langue, par variante. Leur appliquer la déduplication des cartes
    # revenait à jeter toutes les impressions d'un même `oracle_id` sauf une :
    # 22 037 lignes écartées sur les 542 827 du bulk du 19/09, sans aucune trace,
    # puisque le compteur affiché était celui des survivantes. Le symptôme visible
    # était `cards_imported == printings_imported` dans chaque run.
    seen_oracle: dict[str, int] = {}
    for i, row in enumerate(card_rows):
        seen_oracle[row["oracle_id"]] = i
    card_rows = [card_rows[i] for i in sorted(seen_oracle.values())]

    # Une impression ne se déduplique que sur sa propre identité.
    seen_sid: dict[str, int] = {}
    for i, raw in enumerate(raw_cards):
        seen_sid[raw["id"]] = i
    raw_cards = [raw_cards[i] for i in sorted(seen_sid.values())]

    # Ne proposer à l'upsert que les cartes jamais vues dans ce run. Les colonnes
    # de `scryfall_cards` décrivent la carte, pas l'impression : le bulk les
    # répète à l'identique sur chaque impression, et la première occurrence fait
    # donc aussi bien autorité que la dernière.
    if cache_oracle is None:
        cache_oracle = {}
    deja_traitees = set(cache_oracle)
    nouvelles = [ligne for ligne in card_rows if ligne["oracle_id"] not in deja_traitees]
    if nouvelles:
        cache_oracle.update(_upsert_cards(session, nouvelles))
    oracle_to_id = cache_oracle

    printing_rows: list[dict] = []
    faces_par_carte: dict[int, list[dict]] = {}
    raw_prices: dict[str, dict] = {}

    for raw in raw_cards:
        oracle_id = raw.get("oracle_id")
        card_id = oracle_to_id.get(oracle_id)
        if card_id is None:
            continue
        # Les faces appartiennent à la CARTE, pas à l'impression, et `_replace_faces`
        # procède par DELETE puis INSERT. Deux raisons de ne les traiter qu'une
        # fois par carte et par run :
        #
        #   - dans un même lot, plusieurs impressions d'une même carte
        #     insèreraient les mêmes faces autant de fois, et
        #     `scryfall_card_faces` n'a aucune contrainte d'unicité pour l'empêcher ;
        #   - d'un lot à l'autre, refaire le DELETE+INSERT ne change rien à la
        #     donnée et ne produit que des tuples morts. La table en cumulait
        #     1 152 858 insertions pour 1 152 808 suppressions, soit un
        #     remplacement intégral à chaque run.
        if oracle_id not in deja_traitees and card_id not in faces_par_carte:
            faces = _parse_face_rows(raw, card_id)
            if faces:
                faces_par_carte[card_id] = faces
        printing_rows.append(_parse_printing_row(raw, card_id))
        raw_prices[raw["id"]] = raw.get("prices") or {}

    if faces_par_carte:
        face_rows = [ligne for faces in faces_par_carte.values() for ligne in faces]
        _replace_faces(session, face_rows, list(faces_par_carte))

    scryfall_to_printing_id = _upsert_printings(session, printing_rows)

    if not SKIP_SCRYFALL_PRICES:
        price_rows: list[dict] = []
        for scryfall_id, prices_dict in raw_prices.items():
            pid = scryfall_to_printing_id.get(scryfall_id)
            if pid is not None:
                price_rows.extend(_parse_price_rows(prices_dict, pid, today))
        _insert_prices(session, price_rows)

    session.commit()
    # `nouvelles` et non `card_rows` : le compteur doit refléter les cartes
    # réellement upsertées. Additionné sur le run, il converge vers le nombre de
    # cartes oracle distinctes (~38 900) au lieu du nombre de lignes du bulk.
    return len(nouvelles), len(printing_rows)


def import_cards(file_path: Path, session: Session) -> tuple[int, int, int]:
    today = date.today()
    # Partagé par tous les lots du run : c'est ce qui rend la déduplication des
    # cartes globale. ~38 900 entrées en fin de run, quelques mégaoctets.
    cache_oracle: dict[str, int] = {}
    cards_imported = 0
    printings_imported = 0
    errors_count = 0
    card_rows_buf: list[dict] = []
    raw_cards_buf: list[dict] = []

    file_size_mb = file_path.stat().st_size / 1_048_576
    log.info(f"Fichier : {file_path.name} ({file_size_mb:.0f} Mo)")

    # Tracé à CHAQUE run, dans les deux sens : sans cette ligne, quelqu'un qui
    # découvre scryfall_card_prices vide conclurait à une panne d'import et
    # « réparerait » un pipeline qui fonctionne comme prévu.
    if SKIP_SCRYFALL_PRICES:
        log.warning("scryfall_card_prices : écriture DÉSACTIVÉE (SKIP_SCRYFALL_PRICES=1)")
    else:
        log.info("scryfall_card_prices : écriture activée")

    lignes_lues = 0
    lots = 0
    for raw_card in _iter_bulk_cards(file_path):
        lignes_lues += 1
        try:
            card_row = _parse_card_row(raw_card)
            if card_row is None:
                continue
            card_rows_buf.append(card_row)
            raw_cards_buf.append(raw_card)
        except Exception as exc:
            errors_count += 1
            log.warning(f"[PARSE] '{raw_card.get('name', '?')}': {exc}")
            continue

        if len(card_rows_buf) >= BATCH_SIZE:
            lots += 1
            lot_cartes, lot_bruts = card_rows_buf, raw_cards_buf
            try:
                c, p = retry_transient(
                    # noqa B023 : `retry_transient` appelle cette lambda tout de
                    # suite, dans l'itération courante. Les deux variables ne sont
                    # réaffectées qu'au tour suivant, une fois l'appel terminé —
                    # la capture tardive que la règle signale ne peut pas se
                    # produire ici.
                    lambda: _flush_batch(session, lot_cartes, lot_bruts, today,  # noqa: B023
                                        cache_oracle),
                    description=f"[BATCH] cartes {cards_imported}–{cards_imported + BATCH_SIZE}",
                    on_retry=lambda: _safe_rollback(session),
                )
                cards_imported += c
                printings_imported += p
            except Exception as exc:
                log.error(f"[BATCH] cards {cards_imported}–{cards_imported + BATCH_SIZE}: {exc}")
                _safe_rollback(session)
                errors_count += len(lot_cartes)
            finally:
                card_rows_buf = []
                raw_cards_buf = []

            # Progression comptée en LOTS, pas en cartes. Le seuil précédent
            # (`cards_imported % 2_000 == 0`) supposait que le compteur avançait
            # par pas de 500 ; il avance en réalité du nombre de cartes distinctes
            # du lot, une valeur variable. Tomber pile sur un multiple de 2 000
            # relevait donc du hasard : mesuré sur 29 journaux consécutifs, cette
            # ligne s'affichait 0 ou 1 fois par run. Un import de deux heures en
            # production était muet entre son début et sa fin.
            if lots % 40 == 0:  # ~20 000 lignes de bulk
                log.info(
                    f"  -> {lignes_lues:>7,} lignes lues  |  "
                    f"{cards_imported:>7,} cartes  |  "
                    f"{printings_imported:>7,} impressions  |  "
                    f"{errors_count} erreurs"
                )

    if card_rows_buf:
        try:
            c, p = retry_transient(
                lambda: _flush_batch(session, card_rows_buf, raw_cards_buf, today,
                                     cache_oracle),
                description="[BATCH] dernier batch",
                on_retry=lambda: _safe_rollback(session),
            )
            cards_imported += c
            printings_imported += p
        except Exception as exc:
            log.error(f"[BATCH] dernier batch : {exc}")
            _safe_rollback(session)
            errors_count += len(card_rows_buf)

    # Écart entre ce que le bulk contient et ce qui a été upserté. C'est
    # exactement ce chiffre qui manquait : la déduplication écartait 22 037 des
    # 542 827 lignes sans que rien ne l'affiche, le compteur publié étant celui
    # des survivantes. Le tracer à chaque run rend la récidive immédiatement
    # visible, quelle qu'en soit la cause.
    ecart = lignes_lues - printings_imported - errors_count
    log.info(f"Lignes lues dans le bulk : {lignes_lues:,}")
    if ecart:
        log.warning(
            f"{ecart:,} ligne(s) du bulk n'ont produit aucune impression "
            f"({ecart / lignes_lues:.2%} du fichier). Attendu : uniquement les "
            f"lignes sans oracle_id (jetons, cartes d'art)."
        )

    return cards_imported, printings_imported, errors_count


# ══════════════════════════════════════════════════════════════════════════════
# 6. POINT D'ENTRÉE
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Importe les cartes MTG depuis Scryfall bulk data vers PostgreSQL."
    )
    parser.add_argument("--force", action="store_true",
                        help="Retélécharge et réimporte même si ce bulk a déjà été importé.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse sans insérer en base.")
    parser.add_argument("--keep-bulks", type=int, default=1, metavar="N",
                        help="Nombre de fichiers bulk à conserver sur disque (défaut : 1).")
    parser.add_argument("--no-purge", action="store_true",
                        help="Ne supprime pas les anciens fichiers bulk.")
    args = parser.parse_args()

    if not args.dry_run and not check_connection():
        log.error("Connexion PostgreSQL impossible. Vérifier DATABASE_URL dans .env.")
        sys.exit(1)

    with httpx.Client(
        timeout=httpx.Timeout(30.0, read=300.0),
        headers=HTTP_HEADERS,
        follow_redirects=True,
    ) as client:

        log.info("Récupération des métadonnées Scryfall...")
        try:
            download_uri, filename, source_updated_at = fetch_bulk_metadata(client)
        except Exception as exc:
            log.error(f"Impossible de contacter Scryfall : {exc}")
            sys.exit(1)

        log.info(f"  Source   : {filename}")
        log.info(f"  Scryfall : mis à jour le {source_updated_at.strftime('%Y-%m-%d %H:%M UTC')}")

        # Ce bulk est-il déjà en base ? Si oui, inutile de le retélécharger ni de le
        # re-parser : on sort en succès. C'est ce qui rend les runs planifiés répétés
        # (2×/jour) quasi gratuits quand Scryfall n'a rien republié.
        if not args.dry_run and not args.force:
            with SessionLocal() as session:
                if bulk_already_imported(session, download_uri):
                    log.info("Ce bulk a déjà été importé avec succès — rien à faire.")
                    log.info("  (--force pour réimporter malgré tout)")
                    if not args.no_purge:
                        purge_old_files(RAW_DIR, keep=args.keep_bulks, current=filename, logger=log)
                    return

        dest = RAW_DIR / filename

        # Purge AVANT le téléchargement : les anciens bulks sont déjà importés, les garder
        # pendant le parsing (5 à 18 min) ferait cohabiter 2×2,6 Go sur le disque pour rien.
        # `filename` est réservé dans le budget `keep`, donc un téléchargement partiel déjà
        # présent survit et reste réutilisable.
        if not args.no_purge and not args.dry_run:
            log.info("Purge des anciens fichiers bulk...")
            purge_old_files(RAW_DIR, keep=args.keep_bulks, current=filename, logger=log)

        if dest.exists() and not args.force:
            log.info(f"Fichier déjà présent ({dest.stat().st_size / 1_048_576:.0f} Mo). "
                     f"Utilise --force pour retélécharger.")
        else:
            log.info(f"Téléchargement vers {dest} ...")
            download_bulk_file(client, download_uri, dest)

        if args.dry_run:
            log.info("[DRY-RUN] Comptage sans insertion...")
            count = 0
            for _ in _iter_bulk_cards(dest):
                count += 1
                if count % 5_000 == 0:
                    log.info(f"  {count:,} objets parsés...")
            log.info(f"[DRY-RUN] Total : {count:,} objets.")
            return

        with SessionLocal() as session:
            orphelins = fail_orphan_runs(session)
            if orphelins:
                log.warning(
                    f"{orphelins} run(s) precedent(s) restes 'running' marques 'failed' "
                    f"— processus interrompu sans finalisation."
                )

            run = ImportRun(
                source="scryfall",
                source_file=download_uri,
                source_updated_at=source_updated_at,
                status="running",
            )
            session.add(run)
            session.commit()
            session.refresh(run)
            run_id = run.id  # conserve hors ORM : `run` est inutilisable si la session casse
            log.info(f"Import run #{run_id} démarré.")

            started_at = datetime.now(timezone.utc)
            try:
                log.info("Import des éditions...")
                n_sets = import_sets(client, session)
                log.info(f"  {n_sets} éditions importées/mises à jour.")

                log.info("Import des cartes (streaming, batches de 500)...")
                cards_n, printings_n, errors_n = import_cards(dest, session)

                log.info("Propagation des cardmarket_id aux impressions non-anglaises...")
                propagated = retry_transient(
                    lambda: propagate_cardmarket_ids(session),
                    description="propagation cardmarket_id",
                    on_retry=lambda: _safe_rollback(session),
                )
                log.info(f"  {propagated:,} impression(s) mise(s) à jour.")

                log.info("Propagation des tcgplayer_id_en (ID anglais vers toutes les langues)...")
                propagated_tcg = retry_transient(
                    lambda: propagate_tcgplayer_id_en(session),
                    description="propagation tcgplayer_id_en",
                    on_retry=lambda: _safe_rollback(session),
                )
                log.info(f"  {propagated_tcg:,} impression(s) mise(s) à jour.")

                elapsed = int((datetime.now(timezone.utc) - started_at).total_seconds())
                # 'partial' et non 'success' des qu'une carte a ete perdue : un run
                # ou 300 000 cartes ont echoue n'est pas un succes. Consequences
                # voulues : bulk_already_imported() ne le voit pas, donc le prochain
                # run reprend ce bulk ; et la supervision cote RELIC-Trade, qui
                # compte les 'success', signale le decrochage.
                statut_final = "success" if errors_n == 0 else "partial"
                finalize_run(run_id, statut_final, cards=cards_n,
                             printings=printings_n, errors=errors_n)
                if errors_n:
                    log.warning(
                        f"Run marque 'partial' : {errors_n} carte(s) perdue(s). "
                        f"Le prochain run reprendra ce bulk."
                    )

                log.info("")
                log.info("=" * 47)
                log.info(f"  Cartes          : {cards_n:>10,}")
                log.info(f"  Impressions     : {printings_n:>10,}")
                log.info(f"  CM id propagés  : {propagated:>10,}")
                log.info(f"  TCG id_en prop. : {propagated_tcg:>10,}")
                log.info(f"  Éditions        : {n_sets:>10,}")
                log.info(f"  Erreurs         : {errors_n:>10}")
                log.info(f"  Durée           : {elapsed:>9}s")
                log.info("=" * 47)

                # Un run 'partial' doit SORTIR en echec. Le marquer en base ne
                # suffisait pas : le processus rendait 0, `update_all.py` affichait
                # « OK » et la tache planifiee remontait LastTaskResult=0. Un run
                # ayant perdu 300 000 cartes produisait donc exactement le meme
                # signal d'exploitation qu'un run parfait, et la seule trace etait
                # une ligne en base que rien n'interrogeait automatiquement.
                if errors_n:
                    sys.exit(1)

            except Exception as exc:
                elapsed = int((datetime.now(timezone.utc) - started_at).total_seconds())
                log.error(f"Erreur fatale après {elapsed}s : {exc}", exc_info=True)
                _safe_rollback(session)
                finalize_run(run_id, "failed", error_message=str(exc)[:2000])
                sys.exit(1)


if __name__ == "__main__":
    main()
