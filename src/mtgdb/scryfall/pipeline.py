"""
Le pipeline d'import Scryfall : lots, cache, comptage.

Tout ce qui décide de CE qui est écrit vit ici ; le CLI ne fait plus que lire des
options et appeler `import_cards()`.

Deux invariants portés par ce module, tous deux nés d'un défaut réel :

  - une déduplication par `oracle_id` ne s'applique qu'aux CARTES. L'appliquer aux
    impressions écartait 22 037 des 542 827 lignes du bulk à chaque run, sans
    trace ;
  - `SKIP_SCRYFALL_PRICES` n'est plus lu ici. C'est un paramètre, décidé par
    l'appelant : un pipeline qui interroge l'environnement au milieu de son
    travail ne se teste pas.
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from mtgdb.db.engine import engine
from mtgdb.db.retry import retry_transient
from mtgdb.scryfall.parsers import (
    iter_bulk_cards,
    parse_card_row,
    parse_face_rows,
    parse_part_rows,
    parse_price_rows,
    parse_printing_row,
)
from mtgdb.scryfall.upserts import (
    insert_prices,
    replace_faces,
    replace_parts,
    upsert_cards,
    upsert_printings,
)

log = logging.getLogger("mtgdb.scryfall.pipeline")

# Taille d'un lot, en lignes de bulk. 500 : assez pour amortir l'aller-retour
# SQL, assez peu pour qu'un échec ne fasse pas reperdre un travail long.
BATCH_SIZE = 500


def safe_rollback(session: Session) -> None:
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


# ══════════════════════════════════════════════════════════════════════════════
# 5. TRAITEMENT PAR BATCH
# ══════════════════════════════════════════════════════════════════════════════

def flush_batch(
    session: Session,
    card_rows: list[dict],
    raw_cards: list[dict[str, Any]],
    today: date,
    cache_oracle: dict[str, int] | None = None,
    ecrire_prix: bool = True,
    cartes_liees: set[int] | None = None,
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
        cache_oracle.update(upsert_cards(session, nouvelles))
    oracle_to_id = cache_oracle

    printing_rows: list[dict] = []
    faces_par_carte: dict[int, list[dict]] = {}
    # Les cartes liées suivent les faces : elles décrivent la CARTE, on ne les
    # relit donc qu'une fois par carte et par run. Deux structures et non une :
    # `parts_par_carte` porte ce qu'il faut écrire, `cartes_examinees` porte ce
    # qu'il faut purger — y compris les cartes qui n'ont plus aucune liaison,
    # dont les anciennes lignes resteraient sinon en base indéfiniment.
    parts_par_carte: dict[int, list[dict]] = {}
    cartes_examinees: dict[int, None] = {}
    # Cartes dont les liaisons ont deja ete relevees dans ce run. Partage par
    # tous les lots, comme `cache_oracle` : une carte peut livrer ses liaisons
    # au lot 12 apres avoir ete purgee au lot 3.
    if cartes_liees is None:
        cartes_liees = set()
    raw_prices: dict[str, dict] = {}

    for raw in raw_cards:
        oracle_id = raw.get("oracle_id")
        card_id = oracle_to_id.get(oracle_id)
        if card_id is None:
            continue
        # Les faces appartiennent à la CARTE, pas à l'impression, et `replace_faces`
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
            faces = parse_face_rows(raw, card_id)
            if faces:
                faces_par_carte[card_id] = faces
        # La PURGE se decide une fois par carte : elle doit couvrir jusqu'aux
        # cartes qui n'ont plus aucune liaison. Un dict et non une liste, car le
        # lot contient plusieurs impressions par carte et un card_id repete
        # gonflerait le IN du DELETE.
        if oracle_id not in deja_traitees and card_id not in cartes_examinees:
            cartes_examinees[card_id] = None

        # La COLLECTE, elle, suit l'impression qui porte le champ — et non la
        # premiere rencontree. Scryfall ne renseigne `all_parts` que sur une
        # partie des impressions d'une carte : mesure sur le bulk du 20/09,
        # 3 798 des 6 986 cartes liees ont une premiere ligne qui ne le porte
        # pas. Les lire la revenait a perdre 54 % des liaisons, silencieusement,
        # puisque la purge avait bien eu lieu.
        if raw.get("all_parts") and card_id not in cartes_liees:
            parts = parse_part_rows(raw, card_id)
            if parts:
                parts_par_carte[card_id] = parts
                cartes_liees.add(card_id)
        printing_rows.append(parse_printing_row(raw, card_id))
        raw_prices[raw["id"]] = raw.get("prices") or {}

    if faces_par_carte:
        face_rows = [ligne for faces in faces_par_carte.values() for ligne in faces]
        replace_faces(session, face_rows, list(faces_par_carte))

    # Le DELETE couvre les cartes purgees de ce lot ET celles dont on ecrit les
    # liaisons : une carte purgee dans un lot anterieur reviendrait sinon avec
    # les lignes des deux runs.
    ids_a_purger = list(dict.fromkeys([*cartes_examinees, *parts_par_carte]))
    if ids_a_purger:
        part_rows = [ligne for parts in parts_par_carte.values() for ligne in parts]
        replace_parts(session, part_rows, ids_a_purger)

    scryfall_to_printing_id = upsert_printings(session, printing_rows)

    if ecrire_prix:
        price_rows: list[dict] = []
        for scryfall_id, prices_dict in raw_prices.items():
            pid = scryfall_to_printing_id.get(scryfall_id)
            if pid is not None:
                price_rows.extend(parse_price_rows(prices_dict, pid, today))
        insert_prices(session, price_rows)

    session.commit()
    # `nouvelles` et non `card_rows` : le compteur doit refléter les cartes
    # réellement upsertées. Additionné sur le run, il converge vers le nombre de
    # cartes oracle distinctes (~38 900) au lieu du nombre de lignes du bulk.
    return len(nouvelles), len(printing_rows)


def import_cards(file_path: Path, session: Session,
                 *, ecrire_prix: bool = True) -> tuple[int, int, int]:
    today = date.today()
    # Partagé par tous les lots du run : c'est ce qui rend la déduplication des
    # cartes globale. ~38 900 entrées en fin de run, quelques mégaoctets.
    cache_oracle: dict[str, int] = {}
    # Meme role que `cache_oracle`, pour les liaisons : quelques milliers
    # d'entiers, partages par tous les lots du run.
    cartes_liees: set[int] = set()
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
    if ecrire_prix:
        log.info("scryfall_card_prices : écriture activée")
    else:
        log.warning("scryfall_card_prices : écriture DÉSACTIVÉE (SKIP_SCRYFALL_PRICES=1)")

    lignes_lues = 0
    lots = 0
    for raw_card in iter_bulk_cards(file_path):
        lignes_lues += 1
        try:
            card_row = parse_card_row(raw_card)
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
                    lambda: flush_batch(session, lot_cartes, lot_bruts, today,  # noqa: B023
                                        cache_oracle, ecrire_prix, cartes_liees),
                    description=f"[BATCH] cartes {cards_imported}–{cards_imported + BATCH_SIZE}",
                    on_retry=lambda: safe_rollback(session),
                )
                cards_imported += c
                printings_imported += p
            except Exception as exc:
                log.error(f"[BATCH] cards {cards_imported}–{cards_imported + BATCH_SIZE}: {exc}")
                safe_rollback(session)
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
                lambda: flush_batch(session, card_rows_buf, raw_cards_buf, today,
                                     cache_oracle, ecrire_prix, cartes_liees),
                description="[BATCH] dernier batch",
                on_retry=lambda: safe_rollback(session),
            )
            cards_imported += c
            printings_imported += p
        except Exception as exc:
            log.error(f"[BATCH] dernier batch : {exc}")
            safe_rollback(session)
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
