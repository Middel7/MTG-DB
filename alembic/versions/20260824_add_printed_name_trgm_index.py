"""Index GIN trigram sur scryfall_card_printings.printed_name

RELIC-Trade (dépôt MTG-TRADE-FAB) lit cette base et cherche les cartes par nom
traduit avec des ILIKE à joker : `printed_name ILIKE '%feuervogel%'`. Sans index
trigram, PostgreSQL n'a pas d'autre choix que le Seq Scan — 82 491 buffers, soit
la table entière (645 Mo), lue à chaque frappe de l'utilisateur, pour ramener
4 lignes. Mesuré à 289 ms en local, 200 ms à 1,5 s côté API.

Un btree ne peut rien pour ces requêtes : ILIKE est insensible à la casse et le
joker de tête interdit la recherche par préfixe. Seul pg_trgm sait indexer un
LIKE '%…%' — il découpe chaque valeur en trigrammes et indexe ceux-ci.

INDEX COMPLET, PAS PARTIEL
Un `WHERE printed_name IS NOT NULL` avait été envisagé : 23,9 % des lignes ont
printed_name à NULL (126 439 sur 528 180), et les écarter semblait alléger
l'index et l'import. La mesure a invalidé le raisonnement — à données égales,
complet 19 890 176 octets contre partiel 19 709 952, soit 0,9 % d'écart. GIN
n'indexe pas les valeurs NULL : le prédicat ne lui retirait rien qu'il n'ait
déjà écarté.

Le prédicat a donc été abandonné. Un index partiel n'est pas gratuit : le planner
ne peut s'en servir que s'il prouve que le WHERE de la requête implique le
prédicat. C'est acquis pour ILIKE (opérateur strict), mais toute forme qui
brouille cette preuve perdrait l'index. Pour 0,9 % de gain, ce n'était pas un
échange raisonnable.

CONCURRENTLY, HORS TRANSACTION
La table fait 850 Mo et sert deux applications en lecture ; un CREATE INDEX
bloquant gèlerait les écritures de l'import. CONCURRENTLY l'évite, au prix de
deux passes complètes sur la table et d'une attente de la fin de toutes les
transactions ouvertes. Il exige d'être hors transaction, d'où l'autocommit_block.

REJOUABLE SANS ÊTRE DESTRUCTEUR
CONCURRENTLY n'est pas transactionnel : un échec (deadlock, session tuée, disque
plein) laisse derrière lui un index INVALID, qui occupe l'espace et pèse sur les
écritures sans servir aucune requête. Cette migration se garde donc sur
pg_index.indisvalid — elle ne reconstruit que ce qui est absent ou cassé, et ne
touche pas à un index déjà valide (le détruire pour le refaire coûterait deux
passes de plus sur 645 Mo).

Détecter un index INVALID resté d'un run interrompu :

    SELECT c.relname
      FROM pg_index i
      JOIN pg_class c ON c.oid = i.indexrelid
     WHERE NOT i.indisvalid;

Revision ID: 20260824_printed_name_trgm
Revises: 20260713_rename_legacy_indexes
Create Date: 2026-08-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260824_printed_name_trgm"
down_revision: Union[str, Sequence[str], None] = "20260713_rename_legacy_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLE_NAME = "scryfall_card_printings"
INDEX_NAME = "ix_scryfall_card_printings_printed_name_trgm"

# Construire un GIN avec les 64 Mo par défaut multiplie les passes de tri sur
# disque. Réglé en SET de session : on est hors transaction, SET LOCAL n'aurait
# aucun effet. La valeur ne survit pas à la fin de la migration.
MAINTENANCE_WORK_MEM = "512MB"


def _index_state(conn: sa.engine.Connection) -> str | None:
    """'valid', 'invalid', ou None si l'index n'existe pas."""
    indisvalid = conn.execute(
        sa.text(
            """
            SELECT i.indisvalid
              FROM pg_class c
              JOIN pg_index i ON i.indexrelid = c.oid
             WHERE c.relname = :name
            """
        ),
        {"name": INDEX_NAME},
    ).scalar()
    if indisvalid is None:
        return None
    return "valid" if indisvalid else "invalid"


def upgrade() -> None:
    # Hors du bloc autocommit : l'extension doit être commitée avant que le
    # CREATE INDEX puisse résoudre l'opclass gin_trgm_ops. autocommit_block()
    # valide la transaction courante en entrant, ce qui garantit cet ordre.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    with op.get_context().autocommit_block():
        conn = op.get_bind()
        state = _index_state(conn)

        if state == "valid":
            print(f"  {INDEX_NAME} déjà présent et valide — rien à faire.")
            return

        if state == "invalid":
            # Résidu d'une création interrompue : inutilisable, il faut le
            # reprendre à zéro.
            print(f"  {INDEX_NAME} présent mais INVALID — reconstruction.")
            conn.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}"))

        conn.execute(sa.text(f"SET maintenance_work_mem = '{MAINTENANCE_WORK_MEM}'"))
        conn.execute(
            sa.text(
                f"""
                CREATE INDEX CONCURRENTLY {INDEX_NAME}
                    ON {TABLE_NAME} USING gin (printed_name gin_trgm_ops)
                """
            )
        )


def downgrade() -> None:
    # ┌──────────────────────────────────────────────────────────────────────┐
    # │ NE JAMAIS AJOUTER `DROP EXTENSION pg_trgm` ICI.                      │
    # │                                                                      │
    # │ La base `manamind` est partagée avec ManaMind_AI et RELIC-Trade. Si  │
    # │ l'un d'eux crée un jour ses propres index trigram, un DROP EXTENSION │
    # │ … CASCADE les détruirait en silence — et sans CASCADE, ce downgrade  │
    # │ échouerait au lieu de faire son travail. Une extension orpheline ne  │
    # │ coûte rien ; c'est le risque le plus grave de cette migration.       │
    # └──────────────────────────────────────────────────────────────────────┘
    with op.get_context().autocommit_block():
        op.get_bind().execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}"))
