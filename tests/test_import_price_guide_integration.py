"""
Import du Price Guide Cardmarket, contre une vraie base PostgreSQL.

Deux propriétés que seul PostgreSQL peut arbitrer, et qu'aucun test ne couvrait :

  1. un produit inconnu du catalogue ne doit plus faire perdre son prix.
     L'ancien code mettait `id_product` à NULL pour esquiver la clé étrangère —
     252 414 lignes (4 % de la table) étaient dans cet état au 19/09/2026,
     rattachables à rien ;

  2. réimporter le même fichier ne doit rien dupliquer. Ce n'est pas acquis : la
     contrainte `UNIQUE (import_file_id, id_product)` est INOPÉRANTE sur les
     lignes à `id_product` NULL, puisqu'en SQL un NULL n'entre jamais en conflit
     avec un autre NULL. Le correctif du point 1 est donc aussi ce qui rend la
     déduplication possible.

Ces tests écrivent en base : ils refusent de tourner ailleurs qu'en local, et
nettoient systématiquement derrière eux.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from mtgdb.cardmarket.import_price_guide import import_price_guide
from mtgdb.db.models.cardmarket_import_file import CardmarketImportFile

# Plage d'identifiants réservée aux tests. Très au-dessus des identifiants
# Cardmarket réels (~1,2 million au 19/09/2026) : aucune collision possible avec
# une donnée de production, et le nettoyage peut cibler la plage entière.
ID_TEST_MIN = 900_000_000
ID_TEST_MAX = 900_000_999


@pytest.fixture
def session_locale(database_url):
    """Session sur la base locale. Saute si DATABASE_URL vise autre chose."""
    from mtgdb.db.engine import SessionLocal
    from mtgdb.db.urls import is_local_database_url

    if not is_local_database_url(database_url):
        pytest.skip("Ce test écrit en base : réservé à une base locale.")

    with SessionLocal() as session:
        yield session
        _nettoyer(session)


def _nettoyer(session) -> None:
    """Efface tout ce que les tests ont pu créer, dans l'ordre des dépendances."""
    session.rollback()
    session.execute(text("""
        DELETE FROM cardmarket_price_guide_entries
         WHERE id_product BETWEEN :mini AND :maxi
            OR import_file_id IN (SELECT id FROM cardmarket_import_files
                                   WHERE file_type = 'test_price_guide')
    """), {"mini": ID_TEST_MIN, "maxi": ID_TEST_MAX})
    session.execute(
        text("DELETE FROM cardmarket_products WHERE id_product BETWEEN :mini AND :maxi"),
        {"mini": ID_TEST_MIN, "maxi": ID_TEST_MAX})
    session.execute(
        text("DELETE FROM cardmarket_import_files WHERE file_type = 'test_price_guide'"))
    session.commit()


def _fichier_price_guide(tmp_path, identifiants: list[int]):
    chemin = tmp_path / "price_guide_test.json"
    chemin.write_text(json.dumps([
        {"idProduct": pid, "avg": "10.50", "trend": "11.00", "low": "9.00"}
        for pid in identifiants
    ]), encoding="utf-8")
    return chemin


def _nouvel_import(session) -> CardmarketImportFile:
    ligne = CardmarketImportFile(
        file_type="test_price_guide",
        file_url="test://price_guide",
        status="started",
        started_at=datetime.now(timezone.utc),
    )
    session.add(ligne)
    session.commit()
    session.refresh(ligne)
    return ligne


@pytest.mark.integration
def test_un_produit_inconnu_ne_fait_plus_perdre_son_prix(session_locale, tmp_path):
    """
    Le cas le plus fréquent : le Price Guide connaît un produit avant le catalogue.

    Les deux fichiers sont téléchargés séparément, et le catalogue est souvent
    `skipped_not_modified` alors que le Price Guide du jour contient déjà les
    nouveautés. ~4 950 produits par capture étaient concernés.
    """
    session = session_locale
    _nettoyer(session)
    inconnus = [ID_TEST_MIN + 1, ID_TEST_MIN + 2]
    fichier = _fichier_price_guide(tmp_path, inconnus)

    import_price_guide(fichier, session, _nouvel_import(session))

    lignes = session.execute(text("""
        SELECT id_product, avg_price FROM cardmarket_price_guide_entries
         WHERE id_product BETWEEN :mini AND :maxi ORDER BY id_product
    """), {"mini": ID_TEST_MIN, "maxi": ID_TEST_MAX}).all()

    assert [ligne[0] for ligne in lignes] == inconnus, (
        "les prix doivent rester rattachés à leur produit, pas être orphelinés"
    )
    assert all(ligne[1] is not None for ligne in lignes)

    produits = session.execute(text("""
        SELECT id_product, en_name FROM cardmarket_products
         WHERE id_product BETWEEN :mini AND :maxi ORDER BY id_product
    """), {"mini": ID_TEST_MIN, "maxi": ID_TEST_MAX}).all()
    assert [p[0] for p in produits] == inconnus, (
        "le produit manquant doit avoir été créé, en attendant que le Product "
        "Catalog le renseigne"
    )


@pytest.mark.integration
def test_reimporter_le_meme_fichier_ne_cree_aucun_doublon(session_locale, tmp_path):
    """
    L'idempotence, propriété centrale d'un pipeline qui tourne deux fois par jour.

    Le même `import_file_id` rejoué doit buter sur la contrainte unique — ce qui
    n'était possible qu'une fois `id_product` cessé d'être NULL.
    """
    session = session_locale
    _nettoyer(session)
    identifiants = [ID_TEST_MIN + 10, ID_TEST_MIN + 11, ID_TEST_MIN + 12]
    fichier = _fichier_price_guide(tmp_path, identifiants)
    ligne_import = _nouvel_import(session)

    premier = import_price_guide(fichier, session, ligne_import)
    second = import_price_guide(fichier, session, ligne_import)

    total = session.execute(text("""
        SELECT count(*) FROM cardmarket_price_guide_entries
         WHERE import_file_id = :fid
    """), {"fid": ligne_import.id}).scalar()

    assert total == len(identifiants), f"{total} lignes pour {len(identifiants)} produits"
    assert premier == len(identifiants)
    assert second == 0, (
        "le second import ne doit RIEN écrire, et le compteur doit le dire : "
        "il comptait auparavant les lignes proposées, pas les lignes insérées"
    )


@pytest.mark.integration
def test_deux_captures_distinctes_coexistent(session_locale, tmp_path):
    """
    L'historisation reste possible.

    Deux `import_file_id` différents sont deux relevés : la déduplication ne doit
    pas les confondre, sans quoi l'historique des prix cesserait d'exister.
    """
    session = session_locale
    _nettoyer(session)
    identifiants = [ID_TEST_MIN + 20]
    fichier = _fichier_price_guide(tmp_path, identifiants)

    import_price_guide(fichier, session, _nouvel_import(session))
    import_price_guide(fichier, session, _nouvel_import(session))

    total = session.execute(text("""
        SELECT count(*) FROM cardmarket_price_guide_entries
         WHERE id_product = :pid
    """), {"pid": identifiants[0]}).scalar()

    assert total == 2, "deux captures = deux lignes, c'est tout l'objet de la table"


@pytest.mark.integration
def test_aucune_ligne_orpheline_n_est_produite(session_locale, tmp_path):
    """
    Garde-fou direct contre la récidive.

    Aucun import ne doit plus produire de ligne à `id_product` NULL : c'est le
    signe exact du défaut corrigé.
    """
    session = session_locale
    _nettoyer(session)
    fichier = _fichier_price_guide(tmp_path, [ID_TEST_MIN + 30, ID_TEST_MIN + 31])
    ligne_import = _nouvel_import(session)

    import_price_guide(fichier, session, ligne_import)

    orphelines = session.execute(text("""
        SELECT count(*) FROM cardmarket_price_guide_entries
         WHERE import_file_id = :fid AND id_product IS NULL
    """), {"fid": ligne_import.id}).scalar()

    assert orphelines == 0
