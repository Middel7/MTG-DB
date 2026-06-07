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


def extract_products_list(data: Any) -> list[dict]:
    if isinstance(data, list):
        return data
    for key in ("product", "products", "data", "singles"):
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
    "foil_sell":       ("foilSell", "foil_sell", "FoilSell"),
    "foil_low":        ("foilLow", "foil_low", "FoilLow", "Foil Low"),
    "foil_trend":      ("foilTrend", "foil_trend", "FoilTrend", "Foil Trend"),
    "low_price_ex_plus": ("lowEx", "lowPriceExPlus", "low_price_ex_plus", "Low Price Ex+", "lowExPlus"),
    "avg1":            ("avg1", "Avg1", "AVG1"),
    "avg7":            ("avg7", "Avg7", "AVG7"),
    "avg30":           ("avg30", "Avg30", "AVG30"),
    "foil_avg1":       ("foilAvg1", "foil_avg1", "FoilAvg1"),
    "foil_avg7":       ("foilAvg7", "foil_avg7", "FoilAvg7"),
    "foil_avg30":      ("foilAvg30", "foil_avg30", "FoilAvg30"),
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


def extract_price_guide_list(data: Any) -> list[dict]:
    if isinstance(data, list):
        return data
    for key in ("priceGuides", "priceGuide", "price_guide", "product", "products", "data"):
        if isinstance(data, dict) and key in data:
            val = data[key]
            if isinstance(val, list):
                return val
    return []


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
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None
