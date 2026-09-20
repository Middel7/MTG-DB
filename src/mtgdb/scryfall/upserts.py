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

from sqlalchemy import cast, column, delete, or_, select, update, values
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from mtgdb.db.models.card import Card
from mtgdb.db.models.card_face import CardFace
from mtgdb.db.models.card_price import CardPrice
from mtgdb.db.models.card_printing import CardPrinting
from mtgdb.db.models.mtg_set import MtgSet

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


def _table_de_valeurs(modele, noms: tuple[str, ...], lignes: list[dict], alias: str):
    """
    Construit un `VALUES (…), (…)` à partir des lignes à écrire.

    Déclarer le type des colonnes ne suffit PAS à typer le SQL émis : SQLAlchemy
    n'ajoute un `::type` que pour certains types, les tableaux notamment. Quand
    toutes les valeurs d'une colonne du lot valent NULL — cas courant sur
    `edhrec_rank`, `printed_name` ou `cardmarket_id` — PostgreSQL la type en
    `text` par défaut, et la requête échoue sur

        operator does not exist: integer = text

    C'est `_colonne()` qui règle cela, en castant à l'usage.
    """
    colonnes = [column(nom, modele.__table__.c[nom].type) for nom in noms]
    return values(*colonnes, name=alias).data(
        [tuple(ligne.get(nom) for nom in noms) for ligne in lignes]
    )


def _colonne(table_valeurs, modele, nom: str):
    """
    Référence une colonne du `VALUES`, castée dans le type de la table cible.

    Le cast n'est pas une précaution de style : sans lui, une colonne entièrement
    NULL dans le lot arrive en `text` et fait échouer la comparaison comme
    l'affectation. Il est sans coût — PostgreSQL le résout à la planification.
    """
    return cast(table_valeurs.c[nom], modele.__table__.c[nom].type)


def _separer(session: Session, modele, cle: str, rows: list[dict]) -> tuple[dict, list, list]:
    """
    Partage les lignes entre celles déjà en base et les nouvelles.

    POURQUOI CE DÉTOUR PLUTÔT QU'UN SIMPLE `ON CONFLICT`
    `INSERT … ON CONFLICT` consomme une valeur de séquence pour chaque ligne
    PROPOSÉE, y compris celles qui finissent en `UPDATE` ou qui ne font rien :
    `nextval()` est évalué à la construction de la ligne candidate, bien avant
    que le conflit ne soit détecté. Rien ne la rend au moment du conflit.

    Mesuré sur la base locale au 19/09/2026 :

        cards_id_seq            39 840 157   pour      544 insertions réelles
        card_printings_id_seq   40 830 218   pour   14 696 insertions réelles

    Soit 1 024 fois et 75 fois le nombre de lignes des tables. Les colonnes `id`
    étant des `integer`, ce gaspillage — et non la croissance des données —
    fixait l'échéance d'épuisement du plafond 2 147 483 647.

    Séparer coûte un `SELECT` de la clé métier par lot, et fait tomber la
    consommation au nombre d'insertions véritables.
    """
    cles = [ligne[cle] for ligne in rows]
    colonne_cle = getattr(modele, cle)
    connus = {
        valeur: identifiant
        for valeur, identifiant in session.execute(
            select(colonne_cle, modele.id).where(colonne_cle.in_(cles))
        )
    }
    nouvelles = [ligne for ligne in rows if ligne[cle] not in connus]
    existantes = [ligne for ligne in rows if ligne[cle] in connus]
    return connus, nouvelles, existantes


def upsert_cards(session: Session, rows: list[dict]) -> dict[str, int]:
    """Écrit les cartes du lot et retourne la correspondance oracle_id → id."""
    if not rows:
        return {}

    connus, nouvelles, existantes = _separer(session, Card, "oracle_id", rows)

    # Seules les vraies insertions consomment la séquence.
    if nouvelles:
        resultat = session.execute(
            pg_insert(Card)
            .values(nouvelles)
            .on_conflict_do_nothing(index_elements=["oracle_id"])
            .returning(Card.oracle_id, Card.id)
        )
        connus.update(dict(resultat.all()))

    if existantes:
        v = _table_de_valeurs(Card, ("oracle_id", *COLONNES_CARTE), existantes, "cartes")
        session.execute(
            update(Card)
            .where(Card.oracle_id == v.c.oracle_id)
            # N'écrire que ce qui change réellement : sans ce prédicat, chaque
            # ligne identique produit tout de même un tuple mort et du WAL.
            .where(or_(*[
                getattr(Card, colonne).is_distinct_from(_colonne(v, Card, colonne))
                for colonne in COLONNES_CARTE
            ]))
            .values({
                **{colonne: _colonne(v, Card, colonne) for colonne in COLONNES_CARTE},
                "updated_at": func.now(),
            })
        )

    return connus


COLONNES_SET = (
    "name", "set_type", "released_at", "block",
    "parent_set_code", "card_count", "icon_svg_uri",
)


def ecrire_sets(session: Session, rows: list[dict]) -> None:
    """
    Écrit les éditions, sans brûler d'identifiant pour celles qui existent déjà.

    1 051 éditions sont proposées à chaque run pour ~1 nouvelle par mois :
    `mtg_sets_id_seq` était à 85 887 pour 1 051 lignes, soit 82 fois la taille de
    la table. L'enjeu absolu est faible — 0,004 % du plafond `int4` — mais la
    cause est exactement la même que sur les cartes et les impressions, et la
    corriger ici évite qu'on se demande un jour pourquoi cette table-là fait
    exception.
    """
    if not rows:
        return
    _, nouvelles, existantes = _separer(session, MtgSet, "code", rows)

    if nouvelles:
        session.execute(
            pg_insert(MtgSet).values(nouvelles).on_conflict_do_nothing(index_elements=["code"])
        )

    if existantes:
        v = _table_de_valeurs(MtgSet, ("code", *COLONNES_SET), existantes, "editions")
        session.execute(
            update(MtgSet)
            .where(MtgSet.code == v.c.code)
            .where(or_(*[
                getattr(MtgSet, colonne).is_distinct_from(_colonne(v, MtgSet, colonne))
                for colonne in COLONNES_SET
            ]))
            .values({colonne: _colonne(v, MtgSet, colonne) for colonne in COLONNES_SET})
        )


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


COLONNES_IMPRESSION = (
    "oracle_id", "card_id", "set_code", "collector_number", "lang",
    "rarity", "released_at", "artist", "border_color", "frame",
    "full_art", "promo", "reprint", "digital",
    "image_small", "image_normal", "image_large", "image_status", "scryfall_uri",
    "cardmarket_id", "tcgplayer_id", "printed_name",
)


def upsert_printings(session: Session, rows: list[dict]) -> dict[str, int]:
    """Écrit les impressions du lot et retourne la correspondance scryfall_id → id."""
    if not rows:
        return {}

    connus, nouvelles, existantes = _separer(session, CardPrinting, "scryfall_id", rows)

    if nouvelles:
        resultat = session.execute(
            pg_insert(CardPrinting)
            .values(nouvelles)
            .on_conflict_do_nothing(index_elements=["scryfall_id"])
            .returning(CardPrinting.scryfall_id, CardPrinting.id)
        )
        connus.update(dict(resultat.all()))

    if existantes:
        v = _table_de_valeurs(
            CardPrinting, ("scryfall_id", *COLONNES_IMPRESSION), existantes, "impressions")

        def valeur_cible(col: str):
            """Ce que la colonne vaudra après l'écriture."""
            valeur = _colonne(v, CardPrinting, col)
            if col in PRESERVE_IF_NULL:
                return func.coalesce(valeur, getattr(CardPrinting, col))
            return valeur

        session.execute(
            update(CardPrinting)
            .where(CardPrinting.scryfall_id == v.c.scryfall_id)
            # Ne réécrire que les impressions réellement modifiées.
            #
            # `IS DISTINCT FROM` et non `!=` : la table est pleine de NULL
            # (cardmarket_id, printed_name, tcgplayer_id…), et `NULL != NULL` vaut
            # NULL, donc faux — la moitié des colonnes ne serait jamais comparée.
            #
            # La comparaison porte sur la valeur CIBLE, coalesce comprise : sinon
            # une impression dont le bulk ne fournit pas le cardmarket_id serait
            # vue comme modifiée à chaque run, et on retomberait sur le problème
            # que `PRESERVE_IF_NULL` a résolu.
            .where(or_(*[
                getattr(CardPrinting, col).is_distinct_from(valeur_cible(col))
                for col in COLONNES_IMPRESSION
            ]))
            .values({col: valeur_cible(col) for col in COLONNES_IMPRESSION})
        )

    return connus


# Colonnes qui identifient un relevé de prix : la contrainte
# `uq_card_prices_printing_date_type` porte exactement sur celles-ci.
CLE_PRIX = ("printing_id", "date", "source", "currency", "price_type")


def insert_prices(session: Session, rows: list[dict]) -> None:
    """
    Insère les relevés de prix absents, et ne propose que ceux-là.

    `scryfall_card_prices` est append-only : un relevé par impression, par jour et
    par type. Le deuxième run d'une même journée retrouve donc exactement les
    mêmes clés, et son `ON CONFLICT DO NOTHING` n'insérait rien — tout en brûlant
    une valeur de séquence par ligne proposée, soit ~218 000 par jour pour un
    résultat nul.

    Le `SELECT` préalable coûte une requête par lot et supprime entièrement cette
    consommation.
    """
    if not rows:
        return

    deja = {
        tuple(ligne)
        for ligne in session.execute(
            select(*[getattr(CardPrice, col) for col in CLE_PRIX]).where(
                CardPrice.printing_id.in_({r["printing_id"] for r in rows}),
                CardPrice.date == rows[0]["date"],
                CardPrice.source == rows[0]["source"],
            )
        )
    }
    a_inserer = [r for r in rows if tuple(r[col] for col in CLE_PRIX) not in deja]
    if not a_inserer:
        return

    # `DO NOTHING` conservé comme filet : deux runs concurrents pourraient avoir
    # lu le même état avant que l'un des deux n'écrive.
    session.execute(
        pg_insert(CardPrice).values(a_inserer).on_conflict_do_nothing()
    )


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
