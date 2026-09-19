"""
Non-régression sur la consommation des séquences.

`INSERT … ON CONFLICT` consomme une valeur de séquence pour chaque ligne
PROPOSÉE, y compris celles qui finissent en `UPDATE` ou qui ne font rien :
`nextval()` est évalué à la construction de la ligne candidate, bien avant que le
conflit ne soit détecté, et rien ne la rend ensuite.

Mesuré le 19/09/2026 :

    cards_id_seq            39 840 157   pour      544 insertions réelles  1024×
    card_printings_id_seq   40 830 218   pour   14 696 insertions réelles    75×

Les colonnes `id` sont des `integer`. C'était donc ce gaspillage — et non la
croissance des données — qui fixait l'échéance d'épuisement du plafond
2 147 483 647.

Ces tests sont le filet contre une récidive. Elle serait autrement invisible :
tout continuerait de fonctionner, et le problème ne se manifesterait que des
années plus tard, le jour où une séquence bute et bloque toute insertion.

Base jetable : ces tests écrivent, et il leur faut une séquence dont ils
maîtrisent la valeur de départ.
"""
from __future__ import annotations

import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def base_jetable(database_url):
    """Base neuve, migrée, détruite à la fin."""
    from mtgdb.db.urls import is_local_database_url

    if not is_local_database_url(database_url):
        pytest.skip("Ce test crée une base : réservé à un PostgreSQL local.")

    nom = f"mtgdb_seq_{uuid.uuid4().hex[:12]}"
    racine = database_url.rsplit("/", 1)[0]
    url_test = f"{racine}/{nom}"

    admin = create_engine(f"{racine}/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{nom}"'))
    admin.dispose()

    try:
        import os

        env = {**os.environ, "DATABASE_URL": url_test}
        resultat = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=300,
        )
        if resultat.returncode != 0:
            pytest.fail(f"migrations impossibles :\n{resultat.stderr[-2000:]}")
        yield url_test
    finally:
        admin = create_engine(f"{racine}/postgres", isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            conn.execute(text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :nom AND pid <> pg_backend_pid()"), {"nom": nom})
            conn.execute(text(f'DROP DATABASE IF EXISTS "{nom}"'))
        admin.dispose()


@pytest.fixture
def session(base_jetable):
    from sqlalchemy.orm import sessionmaker

    moteur = create_engine(base_jetable)
    Session = sessionmaker(bind=moteur)
    with Session() as s:
        s.execute(text("TRUNCATE scryfall_cards, scryfall_card_printings, "
                       "scryfall_mtg_sets RESTART IDENTITY CASCADE"))
        s.commit()
        yield s
    moteur.dispose()


def _valeur(session, sequence: str) -> int:
    return session.execute(text(f"SELECT last_value FROM {sequence}")).scalar()


def _carte(oracle_id: str, nom: str = "Forest") -> dict:
    return {
        "oracle_id": oracle_id, "name": nom, "normalized_name": nom.lower(),
        "mana_cost": None, "mana_value": None, "type_line": "Land",
        "oracle_text": None, "power": None, "toughness": None, "loyalty": None,
        "defense": None, "colors": [], "color_identity": [], "keywords": [],
        "legal_commander": True, "edhrec_rank": None,
    }


def _impression(scryfall_id: str, card_id: int, oracle_id: str, artiste: str = "X") -> dict:
    return {
        "scryfall_id": scryfall_id, "oracle_id": oracle_id, "card_id": card_id,
        "set_code": None, "collector_number": "1", "lang": "en", "rarity": "common",
        "released_at": None, "artist": artiste, "border_color": "black",
        "frame": "2015", "full_art": False, "promo": False, "reprint": False,
        "digital": False, "image_small": None, "image_normal": None,
        "image_large": None, "scryfall_uri": None, "cardmarket_id": None,
        "tcgplayer_id": None, "printed_name": None,
    }


@pytest.mark.integration
def test_reecrire_les_memes_cartes_ne_consomme_aucun_identifiant(session):
    """
    Le cœur du correctif : un run qui ne trouve rien de nouveau ne doit rien
    consommer. C'est le cas ordinaire — 0 insertion pour 598 710 updates sur la
    production.
    """
    from mtgdb.scryfall.upserts import upsert_cards

    cartes = [_carte(f"oracle-{i:04d}") for i in range(50)]
    upsert_cards(session, cartes)
    session.commit()
    apres_creation = _valeur(session, "cards_id_seq")

    for _ in range(3):
        upsert_cards(session, cartes)
        session.commit()

    assert _valeur(session, "cards_id_seq") == apres_creation, (
        "trois passages sur des cartes inchangées ont consommé des identifiants"
    )


@pytest.mark.integration
def test_seules_les_nouvelles_cartes_consomment(session):
    """La séquence doit avancer exactement du nombre d'insertions réelles."""
    from mtgdb.scryfall.upserts import upsert_cards

    upsert_cards(session, [_carte(f"oracle-{i:04d}") for i in range(10)])
    session.commit()
    avant = _valeur(session, "cards_id_seq")

    # 10 connues + 3 nouvelles
    upsert_cards(session, [_carte(f"oracle-{i:04d}") for i in range(13)])
    session.commit()

    assert _valeur(session, "cards_id_seq") - avant == 3


@pytest.mark.integration
def test_une_modification_reelle_est_ecrite_sans_consommer(session):
    """
    Ne rien consommer ne doit pas vouloir dire ne rien écrire.

    C'est le risque exact d'une optimisation de ce genre : rendre l'import muet
    en croyant le rendre économe.
    """
    from mtgdb.scryfall.upserts import upsert_cards

    upsert_cards(session, [_carte("oracle-0001", "Forest")])
    session.commit()
    avant = _valeur(session, "cards_id_seq")

    upsert_cards(session, [_carte("oracle-0001", "Island")])
    session.commit()

    assert session.execute(text(
        "SELECT name FROM scryfall_cards WHERE oracle_id = 'oracle-0001'")).scalar() == "Island"
    assert _valeur(session, "cards_id_seq") == avant


@pytest.mark.integration
def test_les_impressions_suivent_la_meme_regle(session):
    """520 000 lignes proposées par run, pour quelques milliers de nouvelles."""
    from mtgdb.scryfall.upserts import upsert_cards, upsert_printings

    oracle_to_id = upsert_cards(session, [_carte("oracle-0001")])
    card_id = oracle_to_id["oracle-0001"]
    impressions = [
        _impression(f"sid-{i:04d}", card_id, "oracle-0001") for i in range(20)
    ]
    upsert_printings(session, impressions)
    session.commit()
    avant = _valeur(session, "card_printings_id_seq")

    resultat = upsert_printings(session, impressions)
    session.commit()

    assert _valeur(session, "card_printings_id_seq") == avant
    assert len(resultat) == 20, "les identifiants doivent tout de même être rendus"


@pytest.mark.integration
def test_une_colonne_entierement_nulle_ne_fait_pas_echouer_l_ecriture(session):
    """
    Le piège du `UPDATE … FROM (VALUES …)`, et il ne se voit qu'en base.

    Déclarer le type d'une colonne ne suffit pas à typer le SQL émis. Quand
    toutes les valeurs d'une colonne du lot valent NULL — `edhrec_rank`,
    `printed_name`, `cardmarket_id` le sont couramment — PostgreSQL la type en
    `text` et la requête échoue sur « operator does not exist: integer = text ».
    C'est ce qui a fait perdre 117 327 cartes à un run de contrôle.
    """
    from mtgdb.scryfall.upserts import upsert_cards, upsert_printings

    cartes = [_carte(f"oracle-{i:04d}") for i in range(5)]  # edhrec_rank tous NULL
    oracle_to_id = upsert_cards(session, cartes)
    session.commit()

    impressions = [
        _impression(f"sid-{i:04d}", oracle_to_id[f"oracle-{i:04d}"], f"oracle-{i:04d}")
        for i in range(5)
    ]  # cardmarket_id, tcgplayer_id, printed_name tous NULL
    upsert_printings(session, impressions)
    session.commit()

    # Deuxième passage : c'est lui qui emprunte le chemin UPDATE … FROM (VALUES …)
    for ligne in cartes:
        ligne["name"] = "Modifié"
    upsert_cards(session, cartes)
    for ligne in impressions:
        ligne["artist"] = "Autre"
    upsert_printings(session, impressions)
    session.commit()

    assert session.execute(text(
        "SELECT count(*) FROM scryfall_cards WHERE name = 'Modifié'")).scalar() == 5
    assert session.execute(text(
        "SELECT count(*) FROM scryfall_card_printings WHERE artist = 'Autre'")).scalar() == 5


@pytest.mark.integration
def test_les_editions_aussi(session):
    """1 051 éditions proposées à chaque run pour environ une nouvelle par mois."""
    from mtgdb.scryfall.upserts import ecrire_sets

    editions = [
        {"code": f"e{i:02d}", "name": f"Edition {i}", "set_type": "expansion",
         "released_at": None, "block": None, "parent_set_code": None,
         "card_count": 100, "icon_svg_uri": None}
        for i in range(10)
    ]
    ecrire_sets(session, editions)
    session.commit()
    avant = _valeur(session, "mtg_sets_id_seq")

    ecrire_sets(session, editions)
    session.commit()

    assert _valeur(session, "mtg_sets_id_seq") == avant


@pytest.mark.integration
def test_la_surveillance_ne_signale_que_les_colonnes_integer(session):
    """
    Un `bigint` n'a pas de plafond atteignable et n'a rien à faire dans l'alerte.

    `deck_cards_id_seq` était à 124 289 334 — la plus avancée de la base — mais
    sa colonne est un `bigint` : la signaler aurait fait passer pour urgent ce
    qui ne l'est pas, et décrédibilisé l'alerte.
    """
    from mtgdb.db.sequences import etat_des_sequences

    sequences = etat_des_sequences(session)
    noms = [s.nom for s in sequences]

    assert len(noms) == len(set(noms)), "une séquence ne doit apparaître qu'une fois"
    assert "cards_id_seq" in noms
    for sequence in sequences:
        typ = session.execute(text("""
            SELECT col.data_type
              FROM pg_class sequence
              JOIN pg_depend lien ON lien.objid = sequence.oid AND lien.deptype = 'a'
              JOIN pg_class porteuse ON porteuse.oid = lien.refobjid
              JOIN pg_attribute colonne ON colonne.attrelid = porteuse.oid
                                       AND colonne.attnum = lien.refobjsubid
              JOIN information_schema.columns col
                ON col.table_name = porteuse.relname
               AND col.column_name = colonne.attname
             WHERE sequence.relname = :nom
        """), {"nom": sequence.nom}).scalar()
        assert typ == "integer", f"{sequence.nom} porte une colonne {typ}"
