"""
Parseurs robustes pour les fichiers JSON Cardmarket.
Gère les variantes de noms de clés (camelCase, snake_case, espaces).
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Optional

# ── Product Catalog ───────────────────────────────────────────────────────────

def _get(obj: dict, *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in obj:
            return obj[k]
    return default


def parse_product(raw: dict[str, Any]) -> Optional[dict[str, Any]]:
    id_product = _get(raw, "idProduct", "id_product", "IdProduct")
    if not id_product:
        return None
    try:
        id_product = int(id_product)
    except (ValueError, TypeError):
        return None

    return {
        "id_product": id_product,
        "id_metaproduct": _int_or_none(_get(raw, "idMetaproduct", "id_metaproduct")),
        # Seul champ du Product Catalog qui distingue deux produits portant le même
        # nom : `expansion_name` n'est pas fourni par Cardmarket dans ce fichier, et
        # `en_name` est identique d'une édition à l'autre. Sans `id_expansion`, deux
        # « Jace Reawakened » — l'un de l'édition normale, l'autre d'un promo coté
        # 20× plus cher — sont indiscernables en SQL.
        "id_expansion": _int_or_none(_get(raw, "idExpansion", "id_expansion")),
        "count_reprints": _int_or_none(_get(raw, "countReprints", "count_reprints")),
        "en_name": str(_get(raw, "enName", "en_name", "name", default="")),
        "website": _str_or_none(_get(raw, "website")),
        "image": _str_or_none(_get(raw, "image")),
        "game_name": _str_or_none(_get(raw, "gameName", "game_name")),
        "category_name": _str_or_none(_get(raw, "categoryName", "category_name")),
        "number": _str_or_none(_get(raw, "number")),
        "rarity": _str_or_none(_get(raw, "rarity")),
        "expansion_name": _str_or_none(_get(raw, "expansionName", "expansion_name")),
        "raw_json": raw,
    }


def parse_localizations(raw: dict[str, Any], id_product: int) -> list[dict[str, Any]]:
    rows = []
    for loc in (_get(raw, "localization", "localizations") or []):
        id_lang = _int_or_none(_get(loc, "idLanguage", "id_language"))
        name = _str_or_none(_get(loc, "name", "productName", "product_name"))
        if id_lang is None or not name:
            continue
        rows.append({
            "id_product": id_product,
            "id_language": id_lang,
            "language_name": _str_or_none(_get(loc, "languageName", "language_name")),
            "product_name": name,
        })
    return rows


# Clés sous lesquelles Cardmarket a déjà livré sa liste de produits. L'ordre
# compte : la première trouvée gagne.
CLES_RACINE_PRODUITS = ("product", "products", "data", "singles")


def extract_products_list(data: Any) -> list[dict]:
    if isinstance(data, list):
        return data
    for key in CLES_RACINE_PRODUITS:
        if isinstance(data, dict) and key in data:
            val = data[key]
            if isinstance(val, list):
                return val
    return []


# ── Price Guide ───────────────────────────────────────────────────────────────

_PRICE_KEY_MAP = {
    "avg_price":       ("avg", "Avg", "avgPrice", "avg_price", "AVG"),
    "low_price":       ("low", "Low", "lowPrice", "low_price", "LOW"),
    "trend_price":     ("trend", "Trend", "trendPrice", "trend_price", "TREND"),
    "german_pro_low":  ("germanProLow", "german_pro_low", "GermanProLow"),
    "suggested_price": ("suggestedPrice", "suggested_price", "SuggestedPrice", "sell", "Sell"),
    "foil_sell":       ("avg-foil", "foilSell", "foil_sell", "FoilSell"),
    "foil_low":        ("low-foil", "foilLow", "foil_low", "FoilLow", "Foil Low"),
    "foil_trend":      ("trend-foil", "foilTrend", "foil_trend", "FoilTrend", "Foil Trend"),
    "low_price_ex_plus": ("lowEx", "lowPriceExPlus", "low_price_ex_plus",
                          "Low Price Ex+", "lowExPlus"),
    "avg1":            ("avg1", "Avg1", "AVG1"),
    "avg7":            ("avg7", "Avg7", "AVG7"),
    "avg30":           ("avg30", "Avg30", "AVG30"),
    "foil_avg1":       ("avg1-foil", "foilAvg1", "foil_avg1", "FoilAvg1"),
    "foil_avg7":       ("avg7-foil", "foilAvg7", "foil_avg7", "FoilAvg7"),
    "foil_avg30":      ("avg30-foil", "foilAvg30", "foil_avg30", "FoilAvg30"),
}


def parse_price_guide_entry(raw: dict[str, Any]) -> Optional[dict[str, Any]]:
    id_product = _get(raw, "idProduct", "id_product", "IdProduct")
    if not id_product:
        return None
    try:
        id_product = int(id_product)
    except (ValueError, TypeError):
        return None

    row: dict[str, Any] = {"id_product": id_product, "raw_json": raw}
    for field, keys in _PRICE_KEY_MAP.items():
        row[field] = _decimal_or_none(_get(raw, *keys))
    return row


CLES_RACINE_PRICE_GUIDE = (
    "priceGuides", "priceGuide", "price_guide", "product", "products", "data",
)


def extract_price_guide_list(data: Any) -> list[dict]:
    if isinstance(data, list):
        return data
    for key in CLES_RACINE_PRICE_GUIDE:
        if isinstance(data, dict) and key in data:
            val = data[key]
            if isinstance(val, list):
                return val
    return []


def iter_json_array(chemin, cles_racine: tuple[str, ...]):
    """
    Itère les objets d'un tableau JSON sans charger le fichier en mémoire.

    Les deux exports Cardmarket étaient désérialisés d'un bloc par `json.load()` :
    203 Mo des 350 Mo de pic mémoire du pipeline, pour 46 Mo de fichier. Le coût
    est linéaire en la taille de la source, et celle-ci ne fait que croître.

    `ijson` figure dans les dépendances depuis l'origine sans avoir jamais été
    utilisé : le bulk Scryfall est passé au JSONL gzippé, qui se lit ligne à
    ligne, et personne n'est revenu sur les fichiers Cardmarket.

    La racine n'est pas connue d'avance — Cardmarket a livré tantôt un tableau
    nu, tantôt un objet enveloppant (aujourd'hui `{"version":…, "priceGuides":[…]}`).
    On sonde donc le premier caractère utile pour choisir le préfixe, plutôt que
    de supposer.

    `use_float=True` n'est pas un détail. Par défaut, ijson rend les nombres en
    `Decimal` là où `json.load()` rendait des `float`, et les prix Cardmarket sont
    des nombres JSON (`"avg":0.09`). Or l'objet brut est stocké tel quel dans la
    colonne JSONB `raw_json`, sérialisée par `json.dumps()` — qui ne sait pas
    écrire un `Decimal` et lève `TypeError`. Le passage au streaming aurait donc
    fait échouer tous les imports de Price Guide, sur une ligne que rien ne
    désignait. La précision monétaire, elle, est préservée ailleurs :
    `_decimal_or_none()` reconstruit un `Decimal` depuis la représentation
    textuelle, exactement comme avant.
    """
    import ijson

    with open(chemin, "rb") as f:
        premier = f.read(1)
        while premier and premier.isspace():
            premier = f.read(1)
        f.seek(0)

        if premier == b"[":
            yield from ijson.items(f, "item", use_float=True)
            return

        # Objet enveloppant : on tente chaque clé connue, en relisant le fichier
        # depuis le début. Le coût est négligeable — on s'arrête au premier
        # élément trouvé, sans parcourir tout le fichier pour les clés absentes.
        for cle in cles_racine:
            f.seek(0)
            elements = ijson.items(f, f"{cle}.item", use_float=True)
            try:
                premier_element = next(elements)
            except StopIteration:
                continue
            yield premier_element
            yield from elements
            return


# ── Helpers ───────────────────────────────────────────────────────────────────

def _int_or_none(val: Any) -> Optional[int]:
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _str_or_none(val: Any) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def _decimal_or_none(val: Any) -> Optional[Decimal]:
    if val is None:
        return None
    try:
        s = str(val).strip().replace(",", ".")
        if not s:
            return None
        d = Decimal(s)
        # Cardmarket renvoie 0 pour "pas de données", pas un prix réel
        return None if d == 0 else d
    except (InvalidOperation, ValueError):
        return None
