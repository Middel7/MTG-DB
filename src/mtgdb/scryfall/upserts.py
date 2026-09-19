"""
Écritures Scryfall vers PostgreSQL : upserts, remplacements, propagations.

Chaque fonction prend une `Session` et ne dépend de rien d'autre — ni du CLI, ni
de l'environnement. C'est ce qui permet de les tester contre une base jetable,
sans lancer un import complet.

La règle qui gouverne ce module : **n'écrire que ce qui change**. Les clauses
`WHERE … IS DISTINCT FROM` ne sont pas une optimisation de confort ; sans elles,
les 520 000 lignes d'impressions étaient réécrites à chaque run, sur une instance
de production à 0,1 vCPU qui est déjà le goulot du pipeline.
"""
from __future__ import annotations

import logging

from sqlalchemy import delete, or_, select
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from mtgdb.db.models.card import Card
from mtgdb.db.models.card_face import CardFace
from mtgdb.db.models.card_price import CardPrice
from mtgdb.db.models.card_printing import CardPrinting

log = logging.getLogger("mtgdb.scryfall.upserts")

# ══════════════════════════════════════════════════════════════════════════════
# 2. IMPORT DES ÉDITIONS
# ══════════════════════════════════════════════════════════════════════════════

# Colonnes de `scryfall_cards` qui décrivent la CARTE, jamais l'impression. Le
# bulk les répète à l'identique sur chacune des impressions d'une même carte :
# c'est pourquoi ne retenir que la première occurrence du run est sans perte.
COLONNES_CARTE = (
    "name", "normalized_name", "mana_cost", "mana_value", "type_line",
    "oracle_text", "power", "toughness", "loyalty", "defense",
    "colors", "color_identity", "keywords", "legal_commander", "edhrec_rank",
)


def upsert_cards(session: Session, rows: list[dict]) -> dict[str, int]:
    stmt = pg_insert(Card).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["oracle_id"],
        # N'écrire que ce qui change réellement. Sans ce prédicat, chaque upsert
        # produit un UPDATE — donc un tuple mort, du WAL, et une valeur de
        # séquence consommée — même quand la ligne est rigoureusement identique.
        where=or_(*[
            getattr(Card, colonne).is_distinct_from(getattr(stmt.excluded, colonne))
            for colonne in COLONNES_CARTE
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


def replace_faces(session: Session, face_rows: list[dict], card_ids: list[int]) -> None:
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


def upsert_printings(session: Session, rows: list[dict]) -> dict[str, int]:
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


def insert_prices(session: Session, rows: list[dict]) -> None:
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
