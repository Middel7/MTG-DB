"""
Traduction du bulk Scryfall en lignes de base.

Ces fonctions sont PURES : elles prennent un dictionnaire issu du JSON et
rendent un dictionnaire prêt pour un `INSERT`. Aucune base, aucun réseau, aucune
variable d'environnement — elles se testent avec trois lignes de fixture.

Elles vivaient dans `scripts/import_scryfall.py`, hors du wheel, ce qui obligeait
les tests à charger un script de 1 000 lignes par son chemin de fichier
(`importlib.spec_from_file_location`) pour atteindre une fonction de trente
lignes. Leur place est ici.
"""
from __future__ import annotations

import gzip
import json
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from mtgdb.db.models.card import normalize_card_name


def iter_bulk_cards(file_path: Path):
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


# ══════════════════════════════════════════════════════════════════════════════
# 3. PARSEURS
# ══════════════════════════════════════════════════════════════════════════════

def parse_card_row(raw: dict[str, Any]) -> dict[str, Any] | None:
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


def parse_face_rows(raw: dict[str, Any], card_id: int) -> list[dict[str, Any]]:
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


def parse_part_rows(raw: dict[str, Any], card_id: int) -> list[dict[str, Any]]:
    """
    Traduit `all_parts` en lignes de liaison carte → carte liée.

    Scryfall fait figurer la carte elle-même dans sa propre liste — une carte qui
    engendre un jeton apparaît en `combo_piece` à côté du jeton. Cette
    auto-référence est écartée : elle n'apprend rien et ferait compter la carte
    parmi les objets qu'elle met en jeu.

    La déduplication sur (identifiant, composant) est nécessaire et non
    défensive : un même jeton peut être cité deux fois par des impressions
    voisines, et la contrainte d'unicité de la table ferait échouer le lot entier.
    """
    parts = raw.get("all_parts") or []
    soi = raw.get("id")
    rows: list[dict[str, Any]] = []
    vus: set[tuple[str, str]] = set()
    for part in parts:
        part_id = part.get("id")
        component = part.get("component")
        if not part_id or not component or part_id == soi:
            continue
        cle = (part_id, component)
        if cle in vus:
            continue
        vus.add(cle)
        rows.append({
            "card_id": card_id,
            "component": component,
            "part_scryfall_id": part_id,
            "part_name": part.get("name"),
            "part_type_line": part.get("type_line"),
        })
    return rows


def extract_printed_name(raw: dict[str, Any]) -> str | None:
    printed = raw.get("printed_name")
    if not printed:
        faces = raw.get("card_faces") or []
        face_names = [f.get("printed_name") for f in faces if f.get("printed_name")]
        if face_names:
            printed = " // ".join(face_names)
    return printed or None


def parse_printing_row(raw: dict[str, Any], card_id: int) -> dict[str, Any]:
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
        # Lu sur la CARTE, jamais sur la face : Scryfall qualifie le visuel de
        # l'impression entière, et `image_uris` peut venir d'une face (ci-dessus).
        "image_status": raw.get("image_status"),
        "scryfall_uri": raw.get("scryfall_uri"),
        "cardmarket_id": raw.get("cardmarket_id"),
        "tcgplayer_id": raw.get("tcgplayer_id"),
        "printed_name": extract_printed_name(raw),
    }


def parse_price_rows(prices: dict[str, Any], printing_id: int,
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
